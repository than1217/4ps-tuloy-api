"""Independent COR review, keyed to the source record and course, not payroll status."""
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from mysql.connector import Error
from pydantic import BaseModel, ConfigDict, Field

from bcmd_access import affiliation
from bcmd_actor import actor_values
from database import get_app_connection
from errors import ApiError
from security import Principal, get_beneficiary_principal

router = APIRouter(prefix='/v1/api/tuloy/staff/cor', tags=['Staff'], include_in_schema=False)
REF = re.compile(r'^(CHED|TESDA):([1-9][0-9]{0,18}):(degree|diploma|short_course)$')


def reviewer(principal: Principal = Depends(get_beneficiary_principal)):
    if affiliation(principal) not in {'DSWD', 'TEST'}:
        raise ApiError(403, 'FORBIDDEN', 'Not permitted')
    actor_values(principal, 'actor')
    return principal


def parse_ref(ref):
    match = REF.fullmatch(ref)
    if not match or ((match[1] == 'CHED') != (match[3] == 'degree')):
        raise ApiError(422, 'VALIDATION_ERROR', 'Invalid enrollment reference')
    return match[1], int(match[2]), match[3]


def source_sql(agency, kind):
    # Table/column identifiers come only from these constants, never request text.
    if agency == 'CHED':
        table = 'enroll_matched_CHED_tbl'
        detail = "JSON_OBJECT('program_name',s.degree_program_name,'academic_year',s.degree_academic_year,'year_level',s.degree_year_level,'semester',s.degree_semester,'enrollment_status',s.enrollment_status,'date_enrolled',s.degree_date_enrolled)"
        available = '1=1'
    else:
        table = 'enroll_matched_TESDA_tbl'
        detail = f's.`{kind}`'
        available = f's.`{kind}` IS NOT NULL AND NOT EXISTS (SELECT 1 FROM enroll_matched_CHED_tbl c WHERE c.tuloy_id=s.tuloy_id)'
    return table, detail, available


def snapshot(row, agency, kind):
    details = row['details']
    if isinstance(details, (str, bytes, bytearray)):
        details = json.loads(details)
    if not isinstance(details, dict):
        raise ApiError(503, 'SOURCE_NOT_READY', 'Invalid enrollment source')
    allowed = ('program_name','academic_year','year_level','semester','enrollment_status','training_status','date_enrolled','start_date','end_date')
    result = {'reference': f'{agency}:{row["id"]}:{kind}', 'agency': agency,
              'course_kind': kind, 'tuloy_id': row['tuloy_id'], 'member_id': row['member_id'],
              'details': {key: value for key, value in details.items() if key in allowed}}
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in result['details'].values()):
        raise ApiError(503, 'SOURCE_NOT_READY', 'Invalid enrollment source')
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    return {**result, 'source_sha256': digest}


def read_source(cur, ref, lock=False):
    agency, source_id, kind = parse_ref(ref)
    table, detail, available = source_sql(agency, kind)
    cur.execute(f'SELECT s.id,s.tuloy_id,s.member_id,{detail} details FROM `{table}` s WHERE s.id=%s AND {available}' + (' FOR SHARE' if lock else ''), (source_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, 'NOT_FOUND', 'Enrollment not found')
    return snapshot(row, agency, kind)


def review_state(cur, ref):
    cur.execute('SELECT revision,decision,source_sha256 FROM cor_review WHERE agency=%s AND source_id=%s AND course_kind=%s', parse_ref(ref))
    return cur.fetchone() or {'revision': 0, 'decision': None, 'source_sha256': None}


def db_error(exc):
    # Never expose schema names, SQL, connection details, or submitted reasons.
    if getattr(exc, 'errno', None) == 1062:
        return ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Request conflicts with an earlier submission')
    if getattr(exc, 'errno', None) in {1205, 1213}:
        return ApiError(409, 'CONFLICT', 'Concurrent review; retry the same request')
    return ApiError(503, 'COR_NOT_READY', 'COR review database is not ready')


@router.get('/enrollments')
def enrollments(agency: Literal['CHED','TESDA'] = 'CHED', course_kind: Literal['degree','diploma','short_course'] = 'degree',
                limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000),
                search: str = Query('', max_length=100),
                principal=Depends(reviewer), db=Depends(get_app_connection)):
    parse_ref(f'{agency}:1:{course_kind}')
    table, detail, available = source_sql(agency, course_kind)
    params = []
    if search.strip():
        # Literal substring: percent and underscore are not caller-controlled wildcards.
        text = search.strip().replace('!', '!!').replace('%', '!%').replace('_', '!_')
        available += " AND (s.tuloy_id LIKE %s ESCAPE '!' OR CAST(s.member_id AS CHAR) LIKE %s ESCAPE '!')"
        params.extend(('%'+text+'%', '%'+text+'%'))
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(f'SELECT s.id,s.tuloy_id,s.member_id,{detail} details FROM `{table}` s WHERE {available} ORDER BY s.id DESC LIMIT %s OFFSET %s', (*params, limit+1, offset))
        rows = cur.fetchall()
        items = []
        for row in rows[:limit]:
            item = snapshot(row, agency, course_kind)
            item['review'] = review_state(cur, item['reference'])
            item['review']['stale'] = bool(item['review']['source_sha256'] and item['review']['source_sha256'] != item['source_sha256'])
            items.append(item)
        return {'items': items, 'has_more': len(rows)>limit, 'limit': limit, 'offset': offset}
    except Error as exc:
        raise db_error(exc) from None
    finally:
        cur.close()
        db.rollback()


@router.get('/enrollments/{ref}')
def detail(ref: str, principal=Depends(reviewer), db=Depends(get_app_connection)):
    cur = db.cursor(dictionary=True)
    try:
        item = read_source(cur, ref)
        item['review'] = review_state(cur, ref)
        cur.execute('SELECT revision,decision,reason,created_at FROM cor_review_event WHERE agency=%s AND source_id=%s AND course_kind=%s ORDER BY revision DESC LIMIT 100', parse_ref(ref))
        item['history'] = cur.fetchall()
        return item
    except Error as exc:
        raise db_error(exc) from None
    finally:
        cur.close()
        db.rollback()


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    decision: Literal['APPROVED','DENIED']
    reason: str = Field(min_length=3, max_length=1000)
    expected_revision: int = Field(ge=0, le=2147483646)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    request_key: UUID


@router.post('/enrollments/{ref}/decisions')
def decide(ref: str, body: Decision, principal=Depends(reviewer), db=Depends(get_app_connection)):
    key = parse_ref(ref)
    actor = actor_values(principal, 'actor')
    cur = db.cursor(dictionary=True)
    try:
        item = read_source(cur, ref, lock=True)
        cur.execute('INSERT IGNORE INTO cor_review (agency,source_id,course_kind) VALUES (%s,%s,%s)', key)
        cur.execute('SELECT revision,decision,source_sha256 FROM cor_review WHERE agency=%s AND source_id=%s AND course_kind=%s FOR UPDATE', key)
        current = cur.fetchone()
        cur.execute('SELECT * FROM cor_review_event WHERE request_key=%s', (str(body.request_key),))
        prior = cur.fetchone()
        if prior:
            same = all(prior[k] == v for k, v in actor.items()) and (prior['agency'], prior['source_id'], prior['course_kind']) == key
            same = same and prior['decision'] == body.decision and prior['reason'] == body.reason and prior['source_sha256'] == body.source_sha256 and prior['revision'] == body.expected_revision+1
            if not same:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Request conflicts with an earlier submission')
            return {'revision': prior['revision'], 'decision': prior['decision']}
        if body.expected_revision != current['revision'] or body.source_sha256 != item['source_sha256']:
            raise ApiError(409, 'REVISION_CONFLICT', 'Source or review changed; reload')
        revision = current['revision']+1
        cur.execute('INSERT INTO cor_review_event (agency,source_id,course_kind,revision,decision,reason,source_sha256,actor_api_user_id,actor_staff_user_id,request_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (*key, revision, body.decision, body.reason, body.source_sha256, actor['actor_api_user_id'], actor['actor_staff_user_id'], str(body.request_key)))
        cur.execute('UPDATE cor_review SET revision=%s,decision=%s,source_sha256=%s WHERE agency=%s AND source_id=%s AND course_kind=%s', (revision, body.decision, body.source_sha256, *key))
        db.commit()
        return {'revision': revision, 'decision': body.decision}
    except Error as exc:
        raise db_error(exc) from None
    finally:
        db.rollback()
        cur.close()
