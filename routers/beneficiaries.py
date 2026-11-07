import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from mysql.connector import Error
from fastapi_cache.decorator import cache

from config import get_settings
from database import get_app_connection, get_ods_connection, query_failed
from errors import ApiError
from observability import audit
from schemas import UserInput, ChedResponsePayload, MothersMaiden, Degree, VerifyResult, Verify4psResult, TesdaResponsePayload
from scopes import LEGACY_BENEFICIARY_READ, LEGACY_CHED_READ, LEGACY_CHED_WRITE, LEGACY_MEMBER_VERIFY
from security import Principal, recheck_grant, require_scope

router = APIRouter(tags=["Beneficiaries"])

CACHE_EXPIRE_SECONDS = get_settings().cache_expire_seconds

ELIGIBLE_COLUMNS = (
    "`first_name`,`middle_name`,`last_name`,`date_of_birth`,`sex_id`,`member_id`,`listahanan_id`,"
    "`lrn`,`philsys`,`grade_level`,`region_name`,`barangay_name`,"
    "`client_status`,`roster_status`,`tuloy_id`"
)
# Mother's maiden name is never returned (data-owner rulings, ADR 2026-10-27: "Mother's maiden name stays
# removed" from R9 and R11). R10 still stores it; storage is a separate ruling.
MATCHED_COLUMNS = (
    "`id`,`match_id`,`enrollment_id`,`tuloy_id`,`member_id`,`student_id`,`first_name`,`middle_name`,"
    "`last_name`,`region_name`,`province_name`,"
    "`city_name`,`barangay_name`,`degree_program_name`,`degree_date_enrolled`,`degree_program_duration`,"
    "`degree_academic_year`,`degree_year_level`,`degree_semester`,`enrollment_status`,`date_encoded`,"
    "`date_updated`,`created_at`,`updated_at`"
)
UNIFIED_MATCHED_COLUMNS = (
    "`agency_name`,`tuloy_id`,`member_id`,`enrollment_details`,`created_at`,`updated_at`"
)
# Reviewed against staging SHOW CREATE VIEW, 2026-10-30. Never return arbitrary
# nested fields if the view is later expanded with personal identifiers/names.
_ENROLLMENT_COMMON = frozenset({'course_type', 'program_name', 'enrollment_status', 'date_encoded', 'date_updated'})
UNIFIED_DETAIL_FIELDS = {
    'degree': _ENROLLMENT_COMMON | {'student_id', 'date_enrolled', 'program_duration', 'academic_year', 'year_level', 'semester'},
    'diploma': _ENROLLMENT_COMMON | {'uli_id', 'date_enrolled', 'program_duration', 'academic_year', 'year_level', 'semester'},
    'short_course': _ENROLLMENT_COMMON | {'uli_id', 'start_date', 'end_date'},
}


def unified_details(value):
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    if not isinstance(value, dict) or not (value.keys() & UNIFIED_DETAIL_FIELDS.keys()):
        raise ValueError('Unexpected enrollment details shape')

    def filter_course(course, allowed):
        if not isinstance(course, dict):
            raise ValueError('Unexpected course shape')
        filtered = {key: item for key, item in course.items() if key in allowed}
        if any(isinstance(item, (dict, list)) for item in filtered.values()):
            raise ValueError('Unexpected nested course value')
        return filtered

    result = {}
    for kind, allowed in UNIFIED_DETAIL_FIELDS.items():
        if kind not in value:
            continue
        course = value[kind]
        if kind == 'short_course' and isinstance(course, list):
            result[kind] = [filter_course(item, allowed) for item in course]
        else:
            result[kind] = filter_course(course, allowed)
    return result


# Identifiers shown only by their last 4 digits in the eligible list (DB design decision 1(a), L08).
MASKED_COLUMNS = ("lrn", "philsys")


def mask_last4(value):
    """Replace every digit except the last 4 with '*', keeping separators: 0000-0000-5678 -> ****-****-5678."""
    if not isinstance(value, str):
        return value
    hide = max(len(re.findall(r"\d", value)) - 4, 0)

    def star(match):
        nonlocal hide
        if hide:
            hide -= 1
            return "*"
        return match.group(0)
    return re.sub(r"\d", star, value)


# Optional request fields -> fixed view columns. Only these literals are ever placed in SQL.
OPTIONAL_FILTER_COLUMNS = {
    "middle_name": "middle_name",
    "sex": "sex_id",
    "member_id": "member_id",
    "household_id": "listahanan_id",
    "lrn": "lrn",
    "pcn": "philsys",
    "geocode": "bgy_psgc_code",
}


def _page(rows, limit: int, offset: int):
    has_more = len(rows) > limit
    return jsonable_encoder(
        {
            "items": rows[:limit],
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        },
        custom_encoder={bytes: lambda value: value.hex()},
    )


@router.post("/VerifyMemberBene", response_model=VerifyResult)
def match_user(user: UserInput, current_user: Principal = Depends(require_scope(LEGACY_MEMBER_VERIFY, shared=True)),
               db=Depends(get_ods_connection)):
    return verify_member_match(user, current_user, db)


def verify_member_match(user: UserInput, current_user: Principal, db,
                        audit_action: str = "legacy.verify_member"):
    select_query = r"""
        SELECT `tuloy_id`, `member_id` FROM vw_verify_member_bene
        WHERE match_keys = UNHEX(MD5(CONCAT_WS('/',
            UPPER(TRIM(REGEXP_REPLACE(%s, '\\s+', ' '))),
            UPPER(TRIM(REGEXP_REPLACE(%s, '\\s+', ' '))),
            %s
        )))
    """
    query_params = [user.first_name, user.last_name, user.date_of_birth]
    for field, column in OPTIONAL_FILTER_COLUMNS.items():
        value = getattr(user, field)
        if value is not None:
            select_query += f" AND `{column}` = %s"
            query_params.append(value)
    # Two rows are enough to tell "one" from "many".
    select_query += " LIMIT 2"

    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(select_query, tuple(query_params))
        results = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "verify_member_query_failed")
    finally:
        cursor.close()

    if not results:
        outcome = {"status": "Not Found", "tuloy_id": None, "member_id": None}
    elif len(results) >= 2:
        outcome = {"status": "Multiple Match Found", "tuloy_id": None, "member_id": None}
    else:
        outcome = {
            "status": "Matched",
            "tuloy_id": results[0].get("tuloy_id"),
            "member_id": results[0].get("member_id"),
        }
    audit(audit_action, outcome["status"], actor=current_user.actor)
    return outcome


@router.post("/VerifyTuloyBene", response_model=VerifyResult, summary="Verify Tuloy Beneficiary")
def verify_tuloy_bene(user: UserInput, current_user: Principal = Depends(require_scope(LEGACY_MEMBER_VERIFY, shared=True)),
                      db=Depends(get_ods_connection)):
    return verify_member_match(user, current_user, db, audit_action="legacy.verify_tuloy")


@router.post("/Verify4psBene", response_model=Verify4psResult, summary="Verify 4Ps Beneficiary")
def verify_4ps_bene(user: UserInput, current_user: Principal = Depends(require_scope(LEGACY_MEMBER_VERIFY, shared=True)),
                    db=Depends(get_ods_connection)):
    select_query = r"""
        SELECT `tuloy_id`, `member_id`, `client_status`, `roster_status` FROM vw_verify_4ps_bene
        WHERE match_keys = UNHEX(MD5(CONCAT_WS('/',
            UPPER(TRIM(REGEXP_REPLACE(%s, '\\s+', ' '))),
            UPPER(TRIM(REGEXP_REPLACE(%s, '\\s+', ' '))),
            %s
        )))
    """
    query_params = [user.first_name, user.last_name, user.date_of_birth]
    for field, column in OPTIONAL_FILTER_COLUMNS.items():
        value = getattr(user, field)
        if value is not None:
            select_query += f" AND `{column}` = %s"
            query_params.append(value)
    select_query += " LIMIT 2"

    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(select_query, tuple(query_params))
        results = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "verify_4ps_query_failed")
    finally:
        cursor.close()

    if not results:
        outcome = {"status": "Not Found", "tuloy_id": None, "member_id": None, "client_status": None, "roster_status": None}
    elif len(results) >= 2:
        outcome = {"status": "Multiple Match Found", "tuloy_id": None, "member_id": None, "client_status": None, "roster_status": None}
    else:
        outcome = {
            "status": "Matched",
            "tuloy_id": results[0].get("tuloy_id"),
            "member_id": results[0].get("member_id"),
            "client_status": results[0].get("client_status"),
            "roster_status": results[0].get("roster_status"),
        }
    audit("legacy.verify_4ps", outcome["status"], actor=current_user.actor)
    return outcome


@router.get("/GetEligibleBene")
@cache(expire=CACHE_EXPIRE_SECONDS, namespace="eligible-bene")
def get_eligible_bene(
    current_user: Principal = Depends(require_scope(LEGACY_BENEFICIARY_READ, shared=True)),
    db=Depends(get_ods_connection),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=10_000_000),
):
    # No ORDER BY: on the current view an ordered page exceeds 30 s (measured 2026-10-26),
    # so page boundaries are not guaranteed stable. See docs/ASSESSMENT.md (F-12).
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {ELIGIBLE_COLUMNS} FROM vw_unmatched_member_bene WHERE `member_id` NOT IN "
            "(SELECT `member_id` FROM enroll_matched_CHED_tbl WHERE `member_id` IS NOT NULL) "
            "LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        rows = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "eligible_bene_query_failed")
    finally:
        cursor.close()
    rows = [{**row, **{c: mask_last4(row[c]) for c in MASKED_COLUMNS if c in row}} for row in rows]
    audit("legacy.eligible_bene.list", "success", actor=current_user.actor,
          returned=min(len(rows), limit), offset=offset)
    return _page(rows, limit, offset)


@router.post("/PostEnrolleeMatchedCHED")
def record_ched_response(
    payload: ChedResponsePayload,
    current_user: Principal = Depends(require_scope(LEGACY_CHED_WRITE)),
    db=Depends(get_app_connection),
):
    member_id_str = ""
    if payload.data:
        if payload.data.id:
            member_id_str = str(payload.data.id)
        elif payload.data.member_id:
            member_id_str = str(payload.data.member_id)
    db_status = payload.message or ("FOUND" if payload.status else "NOT FOUND")

    # First statement of the write transaction: a revocation committed since the request was
    # authorised denies the write.
    grant = recheck_grant(db, current_user, LEGACY_CHED_WRITE)
    cursor = db.cursor()
    try:
        # 1. Transaction status row
        cursor.execute(
            "INSERT INTO matching_transaction_CHED_tbl (`tuloy_id`, `status`) VALUES (%s, %s)",
            (member_id_str, db_status),
        )
        txn_id = cursor.lastrowid

        # 2. Matched data row, only for a FOUND result
        if payload.status and payload.message == "FOUND" and payload.data:
            data = payload.data
            mmn = data.mothers_maiden or MothersMaiden()
            deg = data.degree or Degree()

            def blank_to_none(value):
                return value if value and value.strip() else None

            cursor.execute(
                """
                INSERT INTO enroll_matched_CHED_tbl (
                    match_id, enrollment_id, `tuloy_id`, member_id, student_id,
                    mm_first_name, mm_middle_name, mm_last_name,
                    degree_program_name, degree_date_enrolled, degree_program_duration,
                    degree_academic_year, degree_year_level, degree_semester,
                    enrollment_status, date_encoded, date_updated
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    txn_id, payload.enrollment_id or 0, data.id, data.member_id or 0, data.student_id,
                    mmn.first_name, mmn.middle_name, mmn.last_name,
                    deg.program_name, blank_to_none(deg.date_enrolled), deg.program_duration,
                    deg.academic_year, deg.year_level, deg.semester,
                    data.enrollment_status, blank_to_none(data.date_encoded), blank_to_none(data.date_updated),
                ),
            )
        db.commit()
    except Error as exc:
        db.rollback()
        audit("legacy.ched_response.record", "error", actor=current_user.actor)
        raise query_failed(exc, "ched_response_insert_failed")
    finally:
        cursor.close()
    audit("legacy.ched_response.record", "success", actor=current_user.actor, match_txn_id=txn_id,
          grant_partner=grant.partner_id, grant_agency=grant.agency)
    return {"message": "CHED response recorded successfully."}


@router.get("/GetMatchedChedBene")
def get_matched_ched_records(
    current_user: Principal = Depends(require_scope(LEGACY_CHED_READ, shared=True)),
    db=Depends(get_ods_connection),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=10_000_000),
):
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {MATCHED_COLUMNS} FROM vw_get_matched_CHED ORDER BY `id` LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        rows = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "matched_ched_query_failed")
    finally:
        cursor.close()
    audit("legacy.matched_ched.list", "success", actor=current_user.actor,
          returned=min(len(rows), limit), offset=offset)
    return _page(rows, limit, offset)


@router.post("/PostEnrolleeMatchedTESDA", summary="Record TESDA Response")
def record_tesda_response(
    payload: TesdaResponsePayload,
    current_user: Principal = Depends(require_scope(LEGACY_CHED_WRITE)),
    db=Depends(get_app_connection),
):
    learner = payload.learner
    transaction_tuloy_id = ""
    if learner and learner.tuloy_id:
        transaction_tuloy_id = str(learner.tuloy_id).strip()

    if not transaction_tuloy_id:
        raise ApiError(422, "A tuloy_id is required in the payload learner data", "MISSING_TULOY_ID")

    enrollment_id = payload.enrollment_id or 0

    if payload.matched and learner:
        if learner.tuloy_id and len(learner.tuloy_id) > 50:
            raise ApiError(422, "Matched TESDA data requires a TULOY ID of at most 50 characters", "INVALID_TULOY_ID")
        if learner.member_id is None or not (1 <= learner.member_id <= 4294967295):
            raise ApiError(422, "Matched TESDA data requires a MEMBER ID between 1 and 4294967295", "INVALID_MEMBER_ID")
        if learner.user_uli and len(learner.user_uli) > 50:
            raise ApiError(422, "ULI exceeds the database limit of 50 characters", "INVALID_ULI")
        if not (0 <= enrollment_id <= 4294967295):
            raise ApiError(422, "enrollment_id must be between 0 and 4294967295", "INVALID_ENROLLMENT_ID")

    db_status = "FOUND" if payload.matched else "NOT FOUND"
    grant = recheck_grant(db, current_user, LEGACY_CHED_WRITE)
    cursor = db.cursor()
    try:
        cursor.execute(
            "INSERT INTO matching_transaction_TESDA_tbl (`tuloy_id`, `status`) VALUES (%s, %s)",
            (transaction_tuloy_id, db_status),
        )
        txn_id = cursor.lastrowid

        if learner:
            short_course_json = json.dumps([e.model_dump() for e in payload.enrollments]) if payload.enrollments else "[]"
            cursor.execute(
                """
                INSERT INTO enroll_matched_TESDA_tbl (
                    match_id, enrollment_id, `tuloy_id`, member_id, uli_id,
                    first_name, middle_name, last_name, suffix, birthdate,
                    sex, contact_no, email, region, province, city_municipality, psgc_code,
                    enrollments
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s
                )
                """,
                (
                    txn_id, enrollment_id, learner.tuloy_id, learner.member_id, learner.user_uli,
                    learner.first_name, learner.middle_name, learner.last_name, learner.suffix, learner.birthdate,
                    learner.sex, learner.contact_no, learner.email, learner.region, learner.province,
                    learner.city_municipality, learner.psgc_code, short_course_json
                ),
            )
        db.commit()
    except Error as exc:
        db.rollback()
        audit("legacy.tesda_response.record", "error", actor=current_user.actor)
        raise query_failed(exc, "tesda_response_insert_failed")
    finally:
        cursor.close()

    audit("legacy.tesda_response.record", "success", actor=current_user.actor,
          grant=grant.id, action="insert", matched=payload.matched)
    return {"message": "TESDA response recorded successfully."}


@router.get("/GetMatchedTesdaBene", summary="Get Matched TESDA Beneficiaries")
def get_matched_tesda_records(
    current_user: Principal = Depends(require_scope(LEGACY_CHED_READ, shared=True)),
    db=Depends(get_ods_connection),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=10_000_000),
):
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT * FROM vw_get_matched_TESDA ORDER BY `id` LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        rows = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "matched_tesda_query_failed")
    finally:
        cursor.close()
    audit("legacy.matched_tesda.list", "success", actor=current_user.actor,
          returned=min(len(rows), limit), offset=offset)
    return _page(rows, limit, offset)


@router.get("/GetValidatedEnrollees", summary="Get Validated Enrollees (CHED and TESDA)",
            description="Legacy alias for GetUnifiedMatchedBene without search support.")
def get_validated_enrollees(
    current_user: Principal = Depends(require_scope(LEGACY_CHED_READ, shared=True)),
    db=Depends(get_ods_connection),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=10_000_000),
    agency: Literal['CHED', 'TESDA'] | None = Query(None, description="Filter by agency (CHED or TESDA)"),
):
    # This just defers to the modern implementation's underlying query logic.
    cursor = db.cursor(dictionary=True)
    try:
        sql, params = unified_query(agency, None, '')
        cursor.execute(sql, (*params, limit + 1, offset))
        rows = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "validated_enrollees_query_failed")
    finally:
        cursor.close()
    audit("legacy.validated_enrollees.list", "success", actor=current_user.actor,
          returned=min(len(rows), limit), offset=offset)
    return _page(rows, limit, offset)


@router.get("/GetUnifiedMatchedBene", summary="List unified matched enrollments",
    description="DSWD bulk read under the existing matched-enrollment scope. Returns bounded "
                "pages from vw_unified_matched_enrollment. Includes CHED degree enrollments and TESDA "
                "diploma/short-course enrollments where that tuloy_id has no CHED match. Nested JSON "
                "is restricted to reviewed course fields; personal name and national-ID fields are excluded.")
def get_unified_matched_records(
    current_user: Principal = Depends(require_scope(LEGACY_CHED_READ, shared=True)),
    db=Depends(get_ods_connection),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=10_000_000),
    agency: Literal['CHED', 'TESDA'] | None = Query(None),
    course_type: Literal['degree', 'diploma', 'short_course'] | None = Query(None),
    search: str = Query('', max_length=100),
):
    cursor = db.cursor(dictionary=True)
    try:
        sql, params = unified_query(agency, course_type, search)
        cursor.execute(sql, (*params, limit + 1, offset))
        rows = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "unified_matched_query_failed")
    finally:
        cursor.close()
    try:
        rows = [{**row, 'enrollment_details': unified_details(row.get('enrollment_details'))} for row in rows]
    except (ValueError, UnicodeError, TypeError):
        # Never echo the source JSON or beneficiary values in a client error.
        raise ApiError(503, 'UNIFIED_MATCHED_SOURCE_INVALID', 'Unified enrollment data is not ready') from None
    audit("legacy.unified_matched.list", "success", actor=current_user.actor,
          returned=min(len(rows), limit), offset=offset)
    return _page(rows, limit, offset)


def unified_query(agency=None, course_type=None, search='', *, recent=False):
    """Fixed identifiers and bound values; filtering happens before pagination."""
    clauses, params = [], []
    if agency is not None:
        clauses.append('BINARY agency_name=BINARY %s')
        params.append(agency)
    if course_type is not None:
        paths = {'degree': '$.degree', 'diploma': '$.diploma', 'short_course': '$.short_course'}
        clauses.append("JSON_CONTAINS_PATH(enrollment_details,'one',%s)=1")
        params.append(paths[course_type])
    if search.strip():
        # Escape wildcard characters as literals, using a fixed escape character.
        pattern = '%' + search.strip().replace('=', '==').replace('%', '=%').replace('_', '=_') + '%'
        columns = ['tuloy_id', 'CAST(member_id AS CHAR)', *[
            f"JSON_UNQUOTE(JSON_EXTRACT(enrollment_details,'{path}'))" for path in (
                '$.degree.program_name', '$.diploma.program_name', '$.short_course[*].program_name',
                '$.degree.student_id', '$.diploma.uli_id', '$.short_course[*].uli_id')]]
        clauses.append('(' + ' OR '.join(column + " LIKE %s ESCAPE '='" for column in columns) + ')')
        params.extend([pattern] * len(columns))
    where = 'WHERE ' + ' AND '.join(clauses) + ' ' if clauses else ''
    return (f'SELECT {UNIFIED_MATCHED_COLUMNS} FROM vw_unified_matched_enrollment ' + where
            + ('ORDER BY created_at DESC,updated_at DESC,' if recent else 'ORDER BY ')
            + 'BINARY agency_name,BINARY tuloy_id,member_id,created_at,updated_at,'
            + 'CAST(enrollment_details AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin LIMIT %s OFFSET %s', params)
