"""Staff identity read for the staff web app: who is signed in and what they may do.

Always mounted (not part of the partner API switch). Tagged "Staff", which neither docs portal
selects, and left out of the schema altogether: tests/test_partner.py counts every
/v1/api/tuloy operation in the OpenAPI document and expects exactly the six partner ones.
"""
from datetime import datetime, timezone

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPAuthorizationCredentials
from mysql.connector import Error
from pydantic import BaseModel
from errors import ApiError

from database import get_ods_connection, get_app_connection, get_readonly_connection, query_failed
from observability import audit
from routers import beneficiaries, system
from schemas import AppSettingsSchema
from scopes import LEGACY_CHED_READ, agency_permitted
from security import Principal, bearer_scheme, decode_access_token, fetch_grants_with_expiry, get_staff_user, get_verified_staff_token, require_scope

router = APIRouter(prefix="/v1/api/tuloy/staff", tags=["Staff"], include_in_schema=False)


# Minimal binding only (data-owner rulings, ADR 2026-10-27): no internal IDs, partner_id or granted_by.
class StaffUser(BaseModel):
    username: str


class StaffGrant(BaseModel):
    scope: str
    agency: str
    expires_at: str | None  # UTC, RFC 3339; null = the grant does not expire


class StaffToken(BaseModel):
    expires_at: str  # UTC, RFC 3339, from the JWT exp claim


class StaffMe(BaseModel):
    user: StaffUser
    active_grants: list[StaffGrant]
    token: StaffToken


def _rfc3339_utc(moment: datetime) -> str:
    # MySQL TIMESTAMP values come back naive; the grant clock is treated as UTC.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get(
    "/me",
    response_model=StaffMe,
    summary="Who am I",
    responses={
        401: {"description": "Token missing, invalid or expired"},
        403: {"description": "Account disabled"},
    },
)
def staff_me(
    principal: Principal = Depends(get_staff_user),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db=Depends(get_readonly_connection),
):
    # The selected validator has verified the token and enabled identity; no scope is needed.
    exp = principal.expires_at.timestamp() if principal.expires_at else decode_access_token(credentials.credentials)["exp"]
    try:
        # A grant whose agency the rulings exclude authorises nothing, so it is not reported as active.
        if principal.kind == "staff" and principal.full_staff_access:
            # Effective permissions from the explicit temporary policy, not
            # persistent grant rows. Bound their reported lifetime to this token.
            grants = [(g, principal.expires_at) for g in principal.grants]
        else:
            grants = [(g, e) for g, e in fetch_grants_with_expiry(db, principal.id, staff=principal.kind == "staff") if agency_permitted(g.scope, g.agency)]
    except Error as exc:
        raise query_failed(exc, "staff_me_failed")
    audit("staff.me", "success", actor=principal.actor, grants=len(grants),
          access_mode="full" if principal.full_staff_access else "scoped")
    return {
        "user": {"username": principal.username},
        "active_grants": [
            {"scope": g.scope, "agency": g.agency, "expires_at": _rfc3339_utc(expires_at) if expires_at is not None else None}
            for g, expires_at in sorted(grants, key=lambda p: (p[0].scope, p[0].agency))
        ],
        "token": {"expires_at": _rfc3339_utc(datetime.fromtimestamp(exp, timezone.utc))},
    }


@router.post("/session", summary="Register a verified staff identity")
def initialize_staff_session(payload: dict = Depends(get_verified_staff_token), db=Depends(get_app_connection)):
    cursor = db.cursor(dictionary=True)
    identity = (payload["iss"], payload["sub"])
    try:
        cursor.execute("SELECT id, disabled FROM staff_users WHERE issuer = %s AND subject = %s", identity)
        user = cursor.fetchone()
        if user is None:
            try:
                cursor.execute("INSERT INTO staff_users (issuer, subject) VALUES (%s, %s)", identity)
            except Error as exc:
                if exc.errno != 1062:
                    raise
                db.rollback()
            # A concurrent first login may have inserted the same unique identity.
            cursor.execute("SELECT id, disabled FROM staff_users WHERE issuer = %s AND subject = %s", identity)
            user = cursor.fetchone()
        if user is None:
            raise ApiError(503, "SERVICE_UNAVAILABLE", "Staff registration is temporarily unavailable")
        if user.get("disabled"):
            raise ApiError(403, "ACCOUNT_DISABLED", "Inactive staff user")
        db.commit()
        return {"registered": True}
    except Error as exc:
        db.rollback()
        raise query_failed(exc, "staff_registration_failed")
    except ApiError:
        db.rollback()
        raise
    finally:
        cursor.close()


# The staff web app's reads, under the prefix the staging web proxy forwards (/v1/api/tuloy/*); the root
# legacy paths are not forwarded there. Same handlers as the root routes, so the scopes, the data-owner
# rulings (DSWD only, no mother's maiden name, masked LRN/PhilSys), the audit events and the per-user cache
# are identical. Settings are read-only here; writes stay on /PostAppSettings, which is disabled by default.
router.add_api_route("/eligible-beneficiaries", beneficiaries.get_eligible_bene, methods=["GET"])
router.add_api_route("/matched-ched-beneficiaries", beneficiaries.get_matched_ched_records, methods=["GET"])
router.add_api_route("/unified-matched-beneficiaries", beneficiaries.get_unified_matched_records, methods=["GET"])
router.add_api_route("/settings", system.get_app_settings, methods=["GET"], response_model=AppSettingsSchema)

RECENT_MATCHED = 10


@router.get("/overview", summary="Staff overview counts")
def staff_overview(
    current_user: Principal = Depends(require_scope(LEGACY_CHED_READ, shared=True)),
    db=Depends(get_ods_connection),
):
    # Counts on the application's own CHED tables only. The eligible count is null: a COUNT over the
    # member view is too slow to run per page load (an ordered page already exceeds 30 s, F-12), and the
    # app shows the number of rows it has loaded instead.
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT COUNT(*) AS `n` FROM enroll_matched_CHED_tbl")
        matched = cursor.fetchone()["n"]
        cursor.execute("SELECT COUNT(*) AS `n` FROM matching_transaction_CHED_tbl")
        transactions = cursor.fetchone()["n"]
        cursor.execute(f"SELECT {beneficiaries.MATCHED_COLUMNS} FROM vw_get_matched_CHED ORDER BY `id` DESC LIMIT %s",
                       (RECENT_MATCHED,))
        recent = cursor.fetchall()
    except Error as exc:
        raise query_failed(exc, "staff_overview_failed")
    finally:
        cursor.close()
    audit("staff.overview", "success", actor=current_user.actor, recent=len(recent))
    return jsonable_encoder(
        {
            "matched_ched_count": int(matched),
            "matching_txn_count": int(transactions),
            "eligible_beneficiaries_count": None,
            "recent_matched_ched": recent,
        },
        custom_encoder={bytes: lambda value: value.hex()},
    )


UNIFIED_OVERVIEW_COUNTS = """SELECT COUNT(*) AS total_records,
    COALESCE(SUM(BINARY agency_name=BINARY 'CHED'),0) AS ched_records,
    COALESCE(SUM(BINARY agency_name=BINARY 'TESDA'),0) AS tesda_records,
    COUNT(DISTINCT BINARY tuloy_id) AS distinct_tuloy_ids,
    COUNT(DISTINCT CASE WHEN BINARY agency_name=BINARY 'CHED' THEN BINARY tuloy_id END) AS ched_beneficiaries,
    COUNT(DISTINCT CASE WHEN BINARY agency_name=BINARY 'TESDA' THEN BINARY tuloy_id END) AS tesda_beneficiaries
    FROM vw_unified_matched_enrollment"""
UNIFIED_COUNT_KEYS = ('total_records', 'ched_records', 'tesda_records', 'distinct_tuloy_ids',
                      'ched_beneficiaries', 'tesda_beneficiaries')


@router.get('/overview/unified', summary='Unified CHED and TESDA overview')
def unified_overview(
    agency: Literal['CHED', 'TESDA'] | None = Query(None),
    course_type: Literal['degree', 'diploma', 'short_course'] | None = Query(None),
    search: str = Query('', max_length=100),
    offset: int = Query(0, ge=0, le=10_000_000),
    current_user: Principal = Depends(require_scope(LEGACY_CHED_READ, shared=True)),
    db=Depends(get_ods_connection),
):
    # Global aggregates stay independent of the recent-table filters. Never return the full feed.
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(UNIFIED_OVERVIEW_COUNTS)
        counts = cursor.fetchone()
        sql, params = beneficiaries.unified_query(agency, course_type, search, recent=True)
        cursor.execute(sql, (*params, RECENT_MATCHED + 1, offset))
        recent = [{**row, 'enrollment_details': beneficiaries.unified_details(row.get('enrollment_details'))}
                  for row in cursor.fetchall()]
    except Error as exc:
        raise query_failed(exc, 'unified_overview_failed')
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ApiError(503, 'UNIFIED_MATCHED_SOURCE_INVALID', 'Unified enrollment data is not ready') from None
    finally:
        cursor.close()
    audit('staff.overview.unified', 'success', actor=current_user.actor, recent=len(recent))
    has_more = len(recent) > RECENT_MATCHED
    return jsonable_encoder({'counts': {key: int(counts[key]) for key in UNIFIED_COUNT_KEYS},
        'recent_matched': recent[:RECENT_MATCHED], 'recent_limit': RECENT_MATCHED,
        'recent_has_more': has_more, 'recent_offset': offset})
