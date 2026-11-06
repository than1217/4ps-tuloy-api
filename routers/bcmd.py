"""Authenticated BCMD / NAPA snapshots, DQA runs and verified NAPA exports."""
import ipaddress
import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import Response
import bcmd_export as exports
import bcmd_claims as claims
import bcmd_dqa as dqa
from mysql.connector import Error
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import bcmd_store as store
from bcmd_candidates import (CATEGORIES, candidate_query, manila_today, parse_ref, rules, short_course_query,
                             valid_academic_year)
from bcmd_access import affiliation
from bcmd_actor import actor_column
from config import ConfigError
from database import query_failed
from database import get_bcmd_connection
from bcmd_readiness import failure_fields, log_not_ready, verify
from errors import ApiError
from observability import audit, request_id_var
import bcmd_eligibility as eligibility
import bcmd_handoff as handoff
import bcmd_payment_history as history
from scopes import BCMD_SELECTION_REVIEW, STAFF_ADMIN
from security import Principal, get_beneficiary_principal

router = APIRouter(prefix='/v1/api/tuloy/staff/bcmd', tags=['Staff'], include_in_schema=False,
    responses={401: {'description': 'Missing, invalid or expired Bearer token.'},
               403: {'description': 'An active matching DSWD/DSWD or TEST/TEST affiliation is required.'},
               503: {'description': 'Batch service is disabled or database/schema/permission readiness did not pass.'}})

# Synthetic examples only. Record IDs must be selected from the live candidates response.
EXAMPLE_BATCH = {'public_id': '11111111-1111-4111-8111-111111111111',
    'name': 'Example TK batch', 'category': 'TK', 'source_agency': 'CHED',
    'status': 'DQA_COMPLETED', 'member_count': 2, 'dqa_outcome': 'CLEAN',
    'dqa_block_count': 0, 'dqa_warn_count': 0, 'created_at': '2026-01-01T00:00:00Z',
    'export_ready': False}


def example_response(description, example):
    return {'description': description, 'content': {'application/json': {'example': example}}}


def validate_local_candidate(settings):
    if not settings.bcmd_enabled:
        return
    try:
        loopback = settings.ods.host == 'localhost' or ipaddress.ip_address(settings.ods.host).is_loopback
    except ValueError:
        loopback = False
    if settings.is_local:
        if not loopback or settings.ssh_host or not settings.ods.database.startswith('tuloy_napa_'):
            raise ConfigError('Local BCMD requires a loopback tuloy_napa_* synthetic database')
    elif (not settings.bcmd_expected_database or not settings.bcmd_hostnames
          or settings.bcmd_expected_database != settings.ods.database):
        raise ConfigError('BCMD requires explicit matching database and server identity outside local')
    if not settings.bcmd_user or not settings.bcmd_password:
        raise ConfigError('BCMD database credentials are missing')
    if (not settings.bcmd_use_existing_account and
            settings.bcmd_user in {settings.ods.user, settings.rw_user, settings.migration_user}):
        raise ConfigError('Set BCMD_USE_EXISTING_ACCOUNT to explicitly reuse existing credentials')


def require_bcmd_user(request: Request, principal: Principal = Depends(get_beneficiary_principal)):
    # Martin: DSWD and TEST affiliations may use this workspace without a module
    # scope. Active bindings identify affiliation, not authority on other APIs.
    if affiliation(principal) is None:
        raise ApiError(403, 'FORBIDDEN', 'Not permitted for this operation')
    # Migration readiness is checked before any store access. Native and SSO
    # identities use separate foreign keys and separate idempotency namespaces.
    actor_column(principal)
    return principal


def require_selection_reviewer(principal: Principal = Depends(require_bcmd_user)):
    """Duplicate-selection review needs its own grant: BCMD affiliation alone is not enough.

    Checked before the batch database is opened. Reads are covered too, because a conflict
    view links the same member across batches.
    """
    if not principal.grants_for(BCMD_SELECTION_REVIEW):
        audit('authz.scope', 'denied', actor=principal.actor, scope=BCMD_SELECTION_REVIEW)
        raise ApiError(403, 'FORBIDDEN', 'Not permitted for this operation')
    return principal


def get_bcmd_db(request: Request, principal=Depends(require_bcmd_user)):
    if not request.app.state.bcmd_settings.bcmd_enabled:
        raise ApiError(503, 'BCMD_UNAVAILABLE', 'Batch service is not enabled')
    settings = request.app.state.bcmd_settings
    connections = get_bcmd_connection(settings)
    try:
        db = next(connections)
        try:
            verify(db, settings)
        except (ApiError, Error) as exc:
            log_not_ready(exc)
            # Staff admins (no server access needed) see which check failed: check names, versions,
            # schema.table and privilege names only. Everyone else gets the plain 503.
            extra = ({'readiness': failure_fields(exc)}
                     if principal is not None and principal.grants_for(STAFF_ADMIN) else None)
            raise ApiError(503, 'BCMD_NOT_READY', 'Batch service readiness checks did not pass',
                           extra=extra) from None
        cur = db.cursor()
        try:
            cur.execute("SET SESSION innodb_lock_wait_timeout=5")
            cur.execute("SET SESSION time_zone='+00:00'")
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        finally:
            cur.close()
        yield db
    finally:
        connections.close()


Category = Literal['TK', 'TA', 'TT']
ACADEMIC_YEAR_MESSAGE = 'Use YYYY-YYYY with consecutive years, for example 2026-2027'


def plain_text(value):
    """BCMD-07: filter and name text must be printable, without control characters."""
    if value and any(unicodedata.category(ch).startswith('C') for ch in value):
        raise ValueError('Control characters are not allowed')
    return value


class CandidateCriteria(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    academic_year: str = Field(default='', max_length=9)
    semester: Annotated[int, Field(ge=1, le=3)] | None = None
    year_level: Annotated[int, Field(ge=1, le=6)] | None = None
    program_name: str = Field(default='', max_length=255)
    region_name: str = Field(default='', max_length=100)
    search: str = Field(default='', max_length=100)

    @field_validator('academic_year')
    @classmethod
    def academic_year_format(cls, value):
        if value and not valid_academic_year(value):
            raise ValueError(ACADEMIC_YEAR_MESSAGE)
        return value

    @field_validator('program_name', 'region_name', 'search')
    @classmethod
    def printable(cls, value):
        return plain_text(value)


def check_filters(category, criteria):
    """Semester and year level are optional filters only for Tuloy Aral; other categories fix or omit them."""
    extra = [key for key in ('semester', 'year_level')
             if getattr(criteria, key) is not None and key not in CATEGORIES[category]['optional']]
    if extra:
        raise ApiError(422, 'VALIDATION_ERROR', f'{", ".join(extra)} is not a filter for category {category}')


class CreateBatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True,
        json_schema_extra={'examples': [{'name': 'TLY-KLHYO-20261103-101500-000', 'category': 'TK', 'record_ids': [1001, 1002]}]})
    name: str = Field(min_length=1, max_length=120)
    category: Category
    # No fixed batch size: the only limit is the number of currently eligible records.
    # record_ids are CHED source row IDs (kept for earlier clients); record_refs name any source.
    record_ids: list[Annotated[int, Field(strict=True, gt=0)]] | None = Field(default=None, min_length=1)
    record_refs: list[Annotated[str, Field(min_length=10, max_length=40)]] | None = Field(default=None, min_length=1)
    criteria: CandidateCriteria | None = None
    record_count: Annotated[int, Field(strict=True, ge=1)] | None = None

    @field_validator('name')
    @classmethod
    def printable_name(cls, value):
        return plain_text(value)

    @field_validator('record_ids')
    @classmethod
    def no_duplicate_ids(cls, values):
        if values is not None and len(values) != len(set(values)):
            raise ValueError('Duplicate source record IDs are not allowed')
        return values

    @field_validator('record_refs')
    @classmethod
    def valid_refs(cls, values):
        if values is None:
            return values
        for value in values:
            parse_ref(value)  # ValueError: 'CHED:<id>:degree' or 'TESDA:<id>:diploma' only
        if len(values) != len(set(values)):
            raise ValueError('Duplicate source records are not allowed')
        return values

    @model_validator(mode='after')
    def one_selection(self):
        explicit = [value for value in (self.record_ids, self.record_refs) if value is not None]
        if len(explicit) > 1:
            raise ValueError('Choose record_ids or record_refs, never both')
        if explicit:
            if self.criteria is not None or self.record_count is not None:
                raise ValueError('Choose explicit IDs or criteria, never both')
        elif self.criteria is None or self.record_count is None:
            raise ValueError('Criteria and record_count are required without record_ids or record_refs')
        return self


def db_failure(exc):
    # Unreadable payment history fails closed with its own code: nothing is listed or saved.
    if exc.errno in history.UNREADABLE and 'payment_transaction' in (exc.msg or ''):
        return history.unavailable()
    if exc.errno in (1146, 1054):
        return ApiError(503, 'BCMD_SCHEMA_NOT_READY', 'Batch schema is not ready')
    return query_failed(exc, 'bcmd_query_failed')


def bcmd_available(request: Request, principal=Depends(require_bcmd_user)):
    enabled = request.app.state.bcmd_settings.bcmd_enabled
    if enabled:
        connections = get_bcmd_db(request, principal)
        try:
            next(connections)
        except (ApiError, Error):
            return False
        finally:
            connections.close()
    return enabled


@router.get('/status', summary='Check BCMD / NAPA availability',
    description='Read-only readiness check. available=false means no batches should be requested. '
                'max_records is null: there is no fixed batch size, only the number of eligible records. '
                'File readiness requires a current completed DQA run, eligible records and integrity checks; no owner-approval gate.',
    responses={200: example_response('Service readiness; this does not create a batch.',
        {'available': True, 'categories': ['TK', 'TA', 'TT'], 'max_records': None,
         'export_available': True, 'export_file_types': ['DETAIL', 'SUMMARY', 'FAILED'],
         'reason': None})})
def status(request: Request, principal=Depends(require_bcmd_user), enabled=Depends(bcmd_available)):
    today = manila_today()
    return {'available': enabled, 'categories': list(CATEGORIES), 'max_records': None,
            'category_options': [{'code': code, 'label': spec['label'], 'name_prefix': spec['prefix'],
                                  'optional_filters': list(spec['optional']), 'rules': rules(code, today)}
                                 for code, spec in CATEGORIES.items()],
            'creation_available': enabled, 'dqa_runs_available': enabled,
            'export_available': enabled, 'blocking_approvals': [],
            'export_file_types': list(exports.FILE_TYPES),
            'reason': 'BCMD_UNAVAILABLE' if not enabled else None}


@router.post('/batches', status_code=201, summary='Create a persisted BCMD / NAPA batch',
    description='Writes a frozen batch, members, snapshot checks and audit atomically. Choose category TK, TA or TT; '
                'its fixed rules apply automatically (see /candidates rules). There is no fixed batch size. Select unique '
                'source_ref values (record_refs) from /candidates (record_ids still accepts CHED source_record_id values), '
                'or supply criteria (academic_year, semester and year_level for TA, '
                'program_name, region_name, search) and a record_count of 1 or more. Criteria select stable source-ID order inside the save '
                'transaction; a record_count above the number of eligible records returns 422 RECORD_COUNT_EXCEEDS_ELIGIBLE '
                'and saves nothing. A selected record that is no longer eligible, including one a recorded payment now '
                'blocks, returns 422 SOURCE_NOT_ELIGIBLE and saves nothing; payment_excluded_count records how many '
                'criteria matches payment history excluded at save. 503 PAYMENT_HISTORY_UNAVAILABLE when payment '
                'history cannot be read. Retries use the original frozen batch. Example IDs are synthetic. The name must be unique '
                '(case-insensitive); an existing name returns 409 BATCH_NAME_EXISTS. Blank middle names and '
                'suffixes are allowed; missing family association and unknown IP stay null. '
                'dqa_outcome is the save-time snapshot check, not the DQA result: run DQA separately. Retention has no automatic '
                'deadline. No CSV or payment is produced. Retry an uncertain save with the same '
                'Idempotency-Key and payload; changing the payload under that key returns 409.',
    responses={201: example_response('Created batch, or its identical persisted idempotent replay.', EXAMPLE_BATCH),
               409: {'description': 'Batch name already exists, idempotency key conflict or concurrent-save conflict.'},
               422: {'description': 'Invalid request, missing/ineligible source record, ambiguous/duplicate enrollment episode, '
                                   'or RECORD_COUNT_EXCEEDS_ELIGIBLE (record_count is more than the eligible records).'}})
def create(payload: CreateBatch, idempotency_key: Annotated[UUID, Header(description='UUID idempotency key, scoped to the signed-in user. Reuse it for the same uncertain save.')],
           principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    values = payload.model_dump()
    if payload.criteria is not None:
        check_filters(payload.category, payload.criteria)
        # Unused optional filters are omitted so earlier criteria payloads keep their request hash.
        values['criteria'] = {key: value for key, value in values['criteria'].items() if value is not None}
    try:
        return store.create_batch(db, actor=principal, request_id=request_id_var.get(),
            key=str(idempotency_key), **values)
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/candidates', summary='List eligible source records for a category',
    description='Read-only, stable (agency, source ID) order over CHED degree records and TESDA diploma records '
                'without a CHED match (the unified matched source). category TK (default): ENROLLED, year level 1, '
                'semester 1. TA: ENROLLED (optional semester and year_level filters). TT: GRADUATED. Every category also '
                'requires current 4Ps eligibility (vw_verify_member_bene), age 18-30 on the Philippine date and no '
                'recorded payment that blocks this category and period. academic_year must be YYYY-YYYY with consecutive '
                'years. Use source_ref for batch creation. Page with limit (1-200) and offset (0-10,000,000); has_more '
                'indicates another page. total_count is the exact number of eligible records (enrollment records, not '
                'people), or null with total_status UNAVAILABLE when it could not be counted in time; it is never capped. '
                'payment_excluded_count counts records that match but a recorded payment blocks; '
                'short_course_excluded_count counts TESDA short courses, never selectable while their policy is pending. '
                '503 PAYMENT_HISTORY_UNAVAILABLE when payment history cannot be read.',
    responses={200: example_response('Candidate page; all example identifiers are synthetic.',
        {'items': [{'source_ref': 'CHED:1001:degree', 'source_agency': 'CHED', 'course_type': 'degree',
                    'source_record_id': 1001, 'tuloy_id': 'SYNTHETIC-001', 'academic_year': '2026-2027',
                    'semester': 1, 'year_level': '1', 'enrollment_status': 'ENROLLED', 'program_name': 'Example program'}],
         'has_more': False, 'total_count': 1, 'total_status': 'EXACT', 'count_unit': 'ENROLLMENT_RECORD',
         'payment_excluded_count': 0, 'short_course_excluded_count': 0, 'category': 'TK',
         'rules': [{'key': 'enrollment_status', 'label': 'Enrollment status ENROLLED'}]})})
def candidates(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10_000_000),
               category: Category = Query('TK'),
               academic_year: str = Query('', max_length=9), program_name: str = Query('', max_length=255),
               semester: int | None = Query(None, ge=1, le=3), year_level: int | None = Query(None, ge=1, le=6),
               search: str = Query('', max_length=100), region_name: str = Query('', max_length=100),
               principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        criteria = CandidateCriteria(academic_year=academic_year, program_name=program_name, search=search,
                                     region_name=region_name, semester=semester, year_level=year_level)
    except ValueError:
        raise ApiError(422, 'VALIDATION_ERROR', 'Invalid filter value. ' + ACADEMIC_YEAR_MESSAGE + ' for academic year.') from None
    check_filters(category, criteria)
    filters = criteria.model_dump()
    today = manila_today()
    cur = db.cursor(dictionary=True)

    def count(sql, params):
        # An exact count, or None when it did not finish in time. Never a capped number.
        try:
            cur.execute(sql, params)
            return int(cur.fetchone()['total_count'])
        except Error as exc:
            if exc.errno == 3024:
                return None
            raise

    try:
        # Same snapshot for counts and preview; no records are written or returned unbounded.
        db.start_transaction(isolation_level='REPEATABLE READ', consistent_snapshot=True, readonly=True)
        sql, params = candidate_query(filters, category=category, today=today)
        cur.execute(sql, (*params, limit+1, offset))
        rows = cur.fetchall()
        total = count(*candidate_query(filters, category=category, today=today, count=True))
        blocked = count(*candidate_query(filters, category=category, today=today, count=True, payments='blocked'))
        short = count(*short_course_query(filters, today=today))
        items = [{key: value for key, value in row.items() if key != 'source_order'} for row in rows[:limit]]
        return {'items': items, 'has_more': len(rows)>limit, 'total_count': total,
                'total_status': 'EXACT' if total is not None else 'UNAVAILABLE', 'count_unit': 'ENROLLMENT_RECORD',
                'payment_excluded_count': blocked, 'short_course_excluded_count': short,
                'limit': limit, 'offset': offset, 'category': category, 'criteria': filters, 'rules': rules(category, today)}
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


RUN_COLUMNS = ('id', 'public_id', 'run_no', 'status', 'ruleset_version', 'ruleset_sha256', 'evaluation_date',
               'total', 'processed', 'passed', 'failed', 'review_count', 'excluded_count', 'claim_epoch',
               'attempts', 'last_error_code', 'created_at', 'completed_at')


HANDOFF_COLUMNS = ('h.id AS handoff__id,h.public_id AS handoff__public_id,h.dqa_run_id AS handoff__dqa_run_id,'
                   'h.claim_epoch AS handoff__claim_epoch,h.passed_count AS handoff__passed_count,'
                   'h.actor_agency AS handoff__actor_agency,h.created_at AS handoff__created_at,'
                   'hr.public_id AS handoff__dqa_run_public_id,hr.run_no AS handoff__dqa_run_no')
# NAPA register: batches handed off from BCMD (valid or stale), and earlier batches that already have
# issued files (NOT_RECORDED), so those files stay reachable. Filtered in SQL, before pagination.
NAPA_REGISTER = ("WHERE b.status<>'PURGED' AND (EXISTS (SELECT 1 FROM bcmd_napa_handoff h3 WHERE h3.batch_id=b.id) "
                 "OR EXISTS (SELECT 1 FROM bcmd_dqa_run nr WHERE nr.batch_id=b.id AND nr.status='COMPLETED' "
                 "AND nr.run_no=(SELECT MAX(nr2.run_no) FROM bcmd_dqa_run nr2 WHERE nr2.batch_id=b.id)) "
                 "OR EXISTS (SELECT 1 FROM bcmd_batch_export e3 WHERE e3.batch_id=b.id AND e3.status='COMPLETED')) ")


def batch_views(cur, where, params, approvals):
    """Batches with their latest DQA run, NAPA handoff and DETAIL readiness, in one bounded query."""
    run_columns = ','.join(f'r.{column} AS run__{column}' for column in RUN_COLUMNS)
    cur.execute(f'''SELECT b.*,{run_columns},{HANDOFF_COLUMNS},
        EXISTS (SELECT 1 FROM bcmd_batch_export e WHERE e.batch_id=b.id AND e.dqa_run_id=r.id
                AND e.file_type='DETAIL' AND e.status='COMPLETED') AS detail_exported
        FROM bcmd_batch b LEFT JOIN bcmd_dqa_run r
          ON r.id=(SELECT r2.id FROM bcmd_dqa_run r2 WHERE r2.batch_id=b.id ORDER BY r2.run_no DESC LIMIT 1)
        LEFT JOIN bcmd_napa_handoff h ON h.id=(SELECT MAX(h2.id) FROM bcmd_napa_handoff h2 WHERE h2.batch_id=b.id)
        LEFT JOIN bcmd_dqa_run hr ON hr.id=h.dqa_run_id
        {where}''', params)
    views = []
    for row in cur.fetchall():
        run = {column: row[f'run__{column}'] for column in RUN_COLUMNS} if row['run__id'] else None
        latest = ({key[len('handoff__'):]: value for key, value in row.items() if key.startswith('handoff__')}
                  if row['handoff__id'] else None)
        state = exports.handoff_view(latest, run, row)
        view = store.public_batch(row, dqa.summary(run),
                                  exports.detail_ready(row, run, approvals, bool(row['detail_exported']),
                                                       not state['reasons']))
        view['napa_handoff'] = state
        # A duplicate-selection decision changed this batch after its latest run started.
        view['dqa_refresh_required'] = 'DQA_REFRESH_REQUIRED' in exports.run_reasons(run, row)
        views.append((row, view))
    return views


@router.get('/batches', summary='List saved BCMD / NAPA batches',
    description='Read-only register, newest first, with each batch\'s latest DQA run (latest_dqa, dqa_run_status '
                'NOT_RUN until a run exists) and NAPA handoff (napa_handoff.status VALID, STALE with reasons, or '
                'NOT_RECORDED). register=napa lists completed latest DQA batches automatically, batches with historical handoffs, '
                'and earlier batches that already have issued files; it is filtered before pagination. Its items '
                'also carry napa_files: the export-readiness files object (ready, blocking_reasons, member_count, '
                'data_rows, export_no per DETAIL, SUMMARY and FAILED), so the register needs no request per row. '
                'Pagination uses limit and offset with has_more.',
    responses={200: example_response('Batch register page.', {'items': [EXAMPLE_BATCH], 'has_more': False})})
def batches(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10000),
            register: Literal['all', 'napa'] = Query('all', description='napa: the NAPA file register only'),
            principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    cur = db.cursor(dictionary=True)
    try:
        approvals = exports.blockers(cur)
        where = NAPA_REGISTER if register == 'napa' else ''
        views = batch_views(cur, where + 'ORDER BY b.id DESC LIMIT %s OFFSET %s', (limit+1, offset), approvals)
        if register == 'napa':
            for row, view in views[:limit]:
                view['napa_files'] = exports.readiness(cur, row, approvals)[1]
        return {'items': [view for _, view in views[:limit]], 'has_more': len(views)>limit}
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


@router.get('/batches/{public_id}', summary='Get saved batch details',
    description='Read the frozen batch metadata and its latest DQA run by public UUID. '
                'export_ready means the NAPA detailed file can be produced now; it is not a payment authorization.',
    responses={200: example_response('Frozen batch metadata.', EXAMPLE_BATCH),
               404: {'description': 'Batch not found.'}, 410: {'description': 'Batch personal data has been purged.'}})
def detail(public_id: UUID, principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    cur = db.cursor(dictionary=True)
    try:
        store.batch_row(cur, public_id)
        views = batch_views(cur, 'WHERE b.public_id=%s', (str(public_id),), exports.blockers(cur))
        return views[0][1]
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


class StartDQA(BaseModel):
    model_config = ConfigDict(extra='forbid')


@router.post('/batches/{public_id}/dqa-runs', status_code=202, summary='Queue a versioned draft DQA run')
def start_dqa(public_id: UUID, idempotency_key: Annotated[UUID, Header()],
              payload: StartDQA | None = None,
              principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        return dqa.start(db, public_id, idempotency_key, principal, request_id_var.get())
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/dqa-runs', summary='List DQA run history')
def list_dqa(public_id: UUID, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0, le=10000),
             principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        return dqa.history(db, public_id, limit, offset)
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/dqa-runs/{run_id}', summary='Read persisted DQA progress')
def dqa_progress(public_id: UUID, run_id: UUID, principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        return dqa.read(db, public_id, run_id)
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/dqa-runs/{run_id}/results', summary='Read DQA findings by member')
def dqa_results(public_id: UUID, run_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10000),
                outcome: Literal['PASS', 'FAIL', 'REVIEW', 'EXCLUDED'] | None = Query(None),
                principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        return dqa.read(db, public_id, run_id, results=True, limit=limit, offset=offset, outcome=outcome)
    except Error as exc:
        raise db_failure(exc) from None


class CreateExport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file_type: Literal['DETAIL', 'SUMMARY', 'FAILED'] = 'DETAIL'


@router.get('/batches/{public_id}/export-readiness', summary='Check NAPA export readiness per file',
    description='Per-file readiness from the latest DQA run and batch policy, without owner approvals or manual handoff. '
                'DETAIL and SUMMARY contain passed records only; FAILED contains failed records '
                'with remarks. ready/blocking_reasons repeat DETAIL for older clients. Reason codes: DQA_NOT_RUN, '
                'DQA_IN_PROGRESS, DQA_FAILED, DQA_RULESET_OUTDATED, DQA_REFRESH_REQUIRED (a duplicate-selection '
                'decision changed the batch after its latest run started), NO_PASSED_RECORDS, NO_FAILED_RECORDS, '
                'VERSION_UNSUPPORTED. '
                'A file already issued from the latest run stays ready. EXCLUDED records are in no file. Per file, '
                'member_count (and row_count, its earlier name) is the number of member records the file covers; '
                'data_rows is the number of CSV rows after the header (SUMMARY: one per barangay plus the TOTAL row, '
                'so it is not a member count). Exports nothing.')
def export_readiness(public_id: UUID, principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    cur = db.cursor(dictionary=True)
    try:
        batch = exports.locked_batch(cur, public_id)
        run, files, done = exports.readiness(cur, batch)
        return {'batch_id': str(public_id), 'ready': files['DETAIL']['ready'],
                'blocking_reasons': files['DETAIL']['blocking_reasons'],
                'dqa_run': None if not run else {key: run[key] for key in (
                    'public_id', 'run_no', 'status', 'total', 'passed', 'failed', 'review_count', 'excluded_count')},
                'files': files,
                'napa_handoff': exports.handoff_view(exports.latest_handoff(cur, batch), run, batch),
                'exports': [{**exports.metadata(public_id, row, batch), 'completed_at': row['completed_at'].isoformat()
                             if row['completed_at'] else None} for row in done]}
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


@router.post('/batches/{public_id}/exports', status_code=201, summary='Create an immutable NAPA export (DETAIL, SUMMARY or FAILED)',
    description='Builds the file from the latest completed DQA run. Requires a completed current DQA run, '
                'and a UUID Idempotency-Key. If that run already has a '
                'completed file of the same type, that file is returned instead of a new version. A new file rechecks '
                'payment history for every PASS record: one that a recorded payment now blocks returns 409 '
                'PAYMENT_HISTORY_CHANGED and nothing is exported (503 PAYMENT_HISTORY_UNAVAILABLE when history cannot '
                'be read). Any export freezes the batch: no new DQA run can start. No payment action.')
def create_export(public_id: UUID, body: CreateExport,
                  idempotency_key: Annotated[UUID, Header(alias='Idempotency-Key')],
                  principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        return exports.create(db, public_id, idempotency_key, principal, request_id_var.get(), body.file_type)
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/exports/{export_no}', summary='Get NAPA export metadata')
def export_metadata(public_id: UUID, export_no: int = Path(ge=1),
                    principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    """Return version metadata after fresh access and approval checks."""
    cur = db.cursor(dictionary=True)
    try:
        batch = exports.locked_batch(cur, public_id)
        exports.require_approvals(cur)
        rows = exports.export_rows(cur, batch, ' AND e.export_no=%s', (export_no,))
        if not rows:
            raise ApiError(404, 'NOT_FOUND', 'Export not found')
        return exports.metadata(public_id, rows[0], batch)
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


@router.get('/batches/{public_id}/exports/{export_no}/download', summary='Download a verified NAPA CSV',
            responses={200: {'content': {'text/csv': {}}, 'description': 'UTF-8 CSV: NAPA-DETAILED-, NAPA-V2-SUMMARY- or NAPA-FAILED-<batch name>.csv'}})
def download_export(public_id: UUID, export_no: int,
                    principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    """Recheck approvals, regenerate from frozen data and verify both hashes before admitting a CSV disclosure."""
    if export_no < 1:
        raise ApiError(422, 'VALIDATION_ERROR', 'Export number must be positive')
    try:
        data, name = exports.download(db, public_id, export_no, principal, request_id_var.get())
        return Response(data, media_type='text/csv', headers={
            'Content-Disposition': f'attachment; filename="{name}"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/export', summary='Download the latest completed NAPA DETAIL export')
def export(public_id: UUID, principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    """Download the latest completed DETAIL with the same access, approval and integrity checks. Never creates exports on GET."""
    cur = db.cursor(dictionary=True)
    try:
        batch = exports.locked_batch(cur, public_id)
        cur.execute("SELECT MAX(export_no) n FROM bcmd_batch_export WHERE batch_id=%s AND status='COMPLETED' AND file_type='DETAIL'",
                    (batch['id'],))
        number = cur.fetchone()['n']
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()
    if number is None:
        raise ApiError(409, 'NAPA_EXPORT_REQUIRED', 'Create an export version first')
    return download_export(public_id, number, principal, db)


class NapaHandoff(BaseModel):
    model_config = ConfigDict(extra='forbid',
        json_schema_extra={'examples': [{'dqa_run_id': '00000000-0000-4000-8000-000000000002'}]})
    dqa_run_id: UUID = Field(description="public_id of the batch's latest DQA run, as last read: a version check")


EXAMPLE_HANDOFF = {'batch_id': '00000000-0000-4000-8000-000000000001', 'status': 'VALID', 'reasons': [],
                   'current': {'public_id': '00000000-0000-4000-8000-000000000003',
                               'dqa_run_id': '00000000-0000-4000-8000-000000000002', 'dqa_run_no': 2,
                               'passed_count': 1, 'actor_agency': 'DSWD', 'created_at': '2026-11-04T08:00:00.000Z'}}


@router.post('/batches/{public_id}/napa-handoff', status_code=201, summary='Proceed to NAPA: record the durable handoff',
    description='BCMD hands the batch to NAPA with its latest DQA run. Send that run\'s public_id (dqa_run_id) and a UUID '
                'Idempotency-Key; the same key and body return the recorded handoff, and a run already handed off is '
                'returned, never recorded twice. Requires the latest run to be completed, current and not invalidated by '
                'a later duplicate-selection decision, with at least one PASS record, and no recorded payment that '
                'blocks any PASS record now. A later DQA run or decision makes the handoff STALE: hand off again after '
                'the new run. Earlier handoffs are kept as history. Same permission as starting DQA. Writes no file and '
                'no payment. Example IDs are synthetic.',
    responses={201: example_response('Recorded handoff, or the handoff already recorded for that run.', EXAMPLE_HANDOFF),
               404: {'description': 'Batch not found.'},
               409: {'description': 'HANDOFF_RUN_NOT_LATEST (reload: dqa_run_id is not the latest run), NAPA_DQA_REQUIRED '
                                    '(run missing, in progress, failed, outdated or invalidated), NAPA_NO_RECORDS (no PASS '
                                    'record), PAYMENT_HISTORY_CHANGED (a recorded payment now blocks a PASS record: run DQA '
                                    'again), NAPA_VERSION_UNSUPPORTED, NAPA_BATCH_BLOCKED or IDEMPOTENCY_CONFLICT.'},
               410: {'description': 'Batch has been purged.'},
               503: {'description': 'PAYMENT_HISTORY_UNAVAILABLE: payment history could not be read; nothing was recorded.'}})
def napa_handoff(public_id: UUID, body: NapaHandoff,
                 idempotency_key: Annotated[UUID, Header(alias='Idempotency-Key', description='UUID idempotency key, '
                                                         'scoped to the signed-in user and batch.')],
                 principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    try:
        return handoff.record(db, public_id, body.dqa_run_id, idempotency_key, principal, request_id_var.get())
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/napa-handoff', summary='Read the NAPA handoff status and history',
    description='status VALID (new NAPA files may be made from current.dqa_run_id), STALE (reasons: HANDOFF_STALE_DQA_RERUN, '
                'HANDOFF_STALE_REVIEW_CHANGED, HANDOFF_STALE_RULESET_OUTDATED) or NOT_RECORDED (HANDOFF_REQUIRED). '
                'history lists every handoff, newest first. Read-only.',
    responses={200: example_response('Handoff status and history.', {**EXAMPLE_HANDOFF, 'history': [EXAMPLE_HANDOFF['current']]}),
               404: {'description': 'Batch not found.'}, 410: {'description': 'Batch has been purged.'}})
def napa_handoff_status(public_id: UUID, principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    cur = db.cursor(dictionary=True)
    try:
        return handoff.read(cur, exports.locked_batch(cur, public_id))
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


@router.get('/batches/{public_id}/payroll-preview', summary='Read-only payroll estimate from eligible records',
    description='Estimate from the records that are payable now: PASS in the batch\'s latest completed DQA run '
                '(422 NAPA_DQA_REQUIRED when there is none, or it used an outdated ruleset) with no recorded payment '
                'that blocks them (503 PAYMENT_HISTORY_UNAVAILABLE when payment history cannot be read). '
                'No payment transaction is written.')
def payroll_preview(public_id: UUID, months: int = Query(1, ge=1, le=12),
                    principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    from collections import defaultdict
    from routers.payments import amount_for, HOUSEHOLD_CAP, TRACK_ID_BY_NAME
    cur = db.cursor(dictionary=True)
    try:
        # Same shared eligibility as NAPA handoff and payroll: the latest run by number, COMPLETED on the
        # current ruleset; an older completed run never stands in for a newer queued, running or failed one.
        result = eligibility.evaluate(cur, public_id, columns=eligibility.MEMBER_COLUMNS)
        if result is None:
            raise ApiError(404, 'NOT_FOUND', 'Batch not found')
        if 'BATCH_PURGED' in result['reasons']:
            raise ApiError(410, 'BATCH_PURGED', 'Batch has been purged')
        if result['reasons']:
            raise ApiError(422, 'NAPA_DQA_REQUIRED', 'Run DQA on this batch to completion before the payroll preview')
        batch, run = result['batch'], result['run']
        members = [m for m in result['members'] if eligibility.eligible(result, m)]
        if any(not row['hhid'] for row in members):
            raise ApiError(422, 'SOURCE_NOT_READY', 'Batch household data is not ready')
        households = defaultdict(list)
        for row in members:
            households[row['hhid']].append(row)
        paid = [row for rows in households.values() for row in rows[:HOUSEHOLD_CAP]]
        rates = [amount_for(TRACK_ID_BY_NAME.get(row.get('source_agency') or batch['source_agency'])) for row in paid]
        if any(rate <= 0 for rate in rates):
            raise ApiError(422, 'SOURCE_NOT_READY', 'No approved rate for a record source')
        rate = amount_for(0)  # monthly CHED rate; payments.amount_for takes only the track since API !27
        return {'public_id': str(public_id), 'preview_only': True, 'months': months, 'category': batch['category'],
                'member_count': len(members), 'payable_count': len(paid), 'capped_count': len(members)-len(paid),
                'payment_blocked_count': sum(1 for m in result['members'] if m['payment_findings']),
                'payment_findings': eligibility.finding_counts(result['members']),
                'monthly_rate': str(rate), 'total_amount': str(sum(rates, rate * 0) * months),
                'dqa_run_id': run['public_id'], 'dqa_run_no': run['run_no'], 'source': 'NAPA passed records',
                'scope': 'Passed records of this batch with no blocking recorded payment. Not a release authorization.'}
    except history.Unavailable:
        raise history.unavailable() from None
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()


EXAMPLE_CONFLICT = {
    'conflict_id': '22222222-2222-4222-8222-222222222222', 'version': 1, 'status': 'RESOLVED',
    'academic_year': '2026-2027', 'semester': 1, 'selection_count': 2,
    'selections': [
        {'batch_id': '11111111-1111-4111-8111-111111111111', 'batch_name': 'Example TK batch A', 'category': 'TK',
         'batch_status': 'DQA_COMPLETED', 'exported': False, 'line_no': 2, 'decision': 'RETAINED',
         'can_be_retained': False, 'dqa_run_status': 'COMPLETED', 'dqa_refresh_required': True},
        {'batch_id': '33333333-3333-4333-8333-333333333333', 'batch_name': 'Example TK batch B', 'category': 'TK',
         'batch_status': 'DQA_COMPLETED', 'exported': False, 'line_no': 1, 'decision': 'EXCLUDED',
         'can_be_retained': False, 'dqa_run_status': 'COMPLETED', 'dqa_refresh_required': True}],
    'selections_truncated': False, 'retained_batch_id': '11111111-1111-4111-8111-111111111111',
    'actions': {'resolve': {'allowed': False, 'reason': 'CONFLICT_ALREADY_RESOLVED'},
                'reverse': {'allowed': False, 'reason': 'SECOND_REVIEWER_REQUIRED'}},
    'updated_at': '2026-01-01T00:00:00'}
EXAMPLE_DECISION = {
    'decision_id': '44444444-4444-4444-8444-444444444444', 'version': 1, 'action': 'RESOLVE',
    'retained_batch_id': '11111111-1111-4111-8111-111111111111', 'retained_batch_name': 'Example TK batch A',
    'excluded_batches': [{'batch_id': '33333333-3333-4333-8333-333333333333', 'batch_name': 'Example TK batch B'}],
    'reverses_decision_id': None, 'reason': 'Synthetic example: batch A was prepared for this period first.',
    'reviewer': 'synthetic-reviewer', 'reviewer_agency': 'DSWD', 'by_you': True, 'recorded_at': '2026-01-01T00:00:00'}
REVIEW_ERRORS = {
    403: {'description': 'FORBIDDEN without an active tuloy:bcmd:selection:review grant (DSWD); '
                         'SECOND_REVIEWER_REQUIRED when the reviewer who recorded the decision tries to reverse it.'},
    404: {'description': 'NOT_FOUND: unknown duplicate selection.'},
    409: {'description': 'CONFLICT_VERSION_STALE, IDEMPOTENCY_CONFLICT, CLAIM_EXPORTED, CLAIM_PAYMENT_RECORDED, '
                         'CONFLICT_NOT_ACTIVE, CONFLICT_ALREADY_RESOLVED, REVERSAL_REQUIRED, CONFLICT_NOT_RESOLVED, '
                         'CONFLICT_AMBIGUOUS or CLAIM_REVIEW_BUSY. Nothing was recorded.'},
    422: {'description': 'VALIDATION_ERROR or RETAINED_BATCH_NOT_IN_CONFLICT. Nothing was recorded.'},
    503: {'description': 'CLAIM_PAYMENT_CHECK_UNAVAILABLE (recorded payments could not be read) or BCMD_NOT_READY.'}}


class SelectionDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    reason: str = Field(min_length=10, max_length=500, description='Plain text, one line, 10-500 characters. '
                        'Do not include names, HHIDs or other beneficiary details.')
    expected_version: Annotated[int, Field(strict=True, ge=0)] = Field(
        description='The version from the conflict you reviewed; a different current version returns 409.')

    @field_validator('reason')
    @classmethod
    def printable_reason(cls, value):
        return plain_text(value)


class ResolveSelection(SelectionDecision):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, json_schema_extra={'examples': [
        {'retained_batch_id': '11111111-1111-4111-8111-111111111111', 'expected_version': 0,
         'reason': 'Synthetic example: batch A was prepared for this period first.'}]})
    retained_batch_id: UUID = Field(description='Public ID of the competing unexported batch that keeps the claim.')


class ReverseSelection(SelectionDecision):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, json_schema_extra={'examples': [
        {'expected_version': 1, 'reason': 'Synthetic example: the wrong batch was kept; review again.'}]})


@router.get('/duplicate-selections', summary='List duplicate selections across unexported batches',
    description='Requires tuloy:bcmd:selection:review. Conflicts are claim identities (member and academic period) that '
                'DQA found selected in more than one batch (SELECTED_IN_OTHER_BATCH). Filter by batch_id to see the '
                'conflicts touching one batch. Each selection is a batch and a line number in its frozen member list; '
                'no names, HHIDs, member IDs or birth dates are returned. Statuses: UNRESOLVED, RESOLVED, NEEDS_REVIEW '
                '(a selection was added after the decision), RETAINED_UNAVAILABLE (the kept batch was purged). A '
                'decision changes the outcome only at each affected batch\'s next DQA run. It never approves a '
                'beneficiary or a payment. Page with limit (1-50) and offset.',
    responses={200: example_response('Conflict page; all values are synthetic.', {'items': [EXAMPLE_CONFLICT], 'has_more': False}),
               403: REVIEW_ERRORS[403], 404: {'description': 'Batch not found.'}, 410: {'description': 'Batch was purged.'}})
def list_duplicate_selections(batch_id: UUID | None = Query(None), limit: int = Query(20, ge=1, le=50),
                              offset: int = Query(0, ge=0, le=10000),
                              principal=Depends(require_selection_reviewer), db=Depends(get_bcmd_db)):
    try:
        return claims.list_conflicts(db, principal, batch_id=batch_id, limit=limit, offset=offset)
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/duplicate-selections/{conflict_id}', summary='Read a duplicate selection with its decision history',
    description='Requires tuloy:bcmd:selection:review. Returns the competing batches, the effective decision, which '
                'actions are available (with the reason when not) and the newest 50 decisions: version, RESOLVE or '
                'REVERSE, kept and excluded batches, reviewer, agency, time and reason.',
    responses={200: example_response('Conflict with history; synthetic.', {**EXAMPLE_CONFLICT, 'history': {
                   'items': [EXAMPLE_DECISION], 'has_more': False}}), 403: REVIEW_ERRORS[403], 404: REVIEW_ERRORS[404]})
def read_duplicate_selection(conflict_id: UUID, principal=Depends(require_selection_reviewer), db=Depends(get_bcmd_db)):
    try:
        return claims.read_conflict(db, conflict_id, principal)
    except Error as exc:
        raise db_failure(exc) from None


@router.post('/duplicate-selections/{conflict_id}/resolution', status_code=201,
    summary='Record which batch keeps a duplicate selection',
    description='Requires tuloy:bcmd:selection:review, a UUID Idempotency-Key, a plain-text reason and the '
                'expected_version you reviewed. The chosen unexported batch keeps the member and period; every other '
                'current selection becomes EXCLUDED at its batch\'s next DQA run and is left out of the NAPA files and '
                'the payroll preview. Unrelated failed findings stay in effect. Every affected '
                'batch needs a fresh DQA run (export readiness reports DQA_REFRESH_REQUIRED) and any queued or running '
                'run of those batches stops with CLAIM_DECISION_CHANGED. Refused when any batch holding the claim has '
                'an export or a recorded payment. A conflict in NEEDS_REVIEW may only keep the same batch; choosing '
                'another needs a reversal first. Same key and body replays the decision; a different body returns '
                '409 IDEMPOTENCY_CONFLICT. This does not approve a beneficiary or a payment.',
    responses={201: example_response('Recorded decision, or its replay; synthetic.',
                                     {'decision': EXAMPLE_DECISION, 'conflict': EXAMPLE_CONFLICT}), **REVIEW_ERRORS})
def resolve_duplicate_selection(conflict_id: UUID, body: ResolveSelection,
                                idempotency_key: Annotated[UUID, Header(alias='Idempotency-Key')],
                                principal=Depends(require_selection_reviewer), db=Depends(get_bcmd_db)):
    try:
        return claims.resolve(db, conflict_id, retained_batch_id=body.retained_batch_id, reason=body.reason,
                              expected_version=body.expected_version, key=idempotency_key, actor=principal,
                              request_id=request_id_var.get())
    except Error as exc:
        raise db_failure(exc) from None


@router.post('/duplicate-selections/{conflict_id}/reversal', status_code=201,
    summary='Reverse the effective duplicate-selection decision',
    description='Requires tuloy:bcmd:selection:review and a different reviewer from the one who recorded the decision '
                '(403 SECOND_REVIEWER_REQUIRED). The history keeps both entries. Every selection returns to for '
                'verification at its batch\'s next DQA run; affected batches need fresh DQA. Refused when any batch '
                'holding the claim has an export or a recorded payment. Same idempotency rules as a resolution.',
    responses={201: example_response('Recorded reversal, or its replay; synthetic.', {
                   'decision': {**EXAMPLE_DECISION, 'version': 2, 'action': 'REVERSE', 'retained_batch_id': None,
                                'retained_batch_name': None, 'excluded_batches': [],
                                'reverses_decision_id': EXAMPLE_DECISION['decision_id'],
                                'reason': 'Synthetic example: the wrong batch was kept; review again.'},
                   'conflict': {**EXAMPLE_CONFLICT, 'version': 2, 'status': 'UNRESOLVED', 'retained_batch_id': None}}),
               **REVIEW_ERRORS})
def reverse_duplicate_selection(conflict_id: UUID, body: ReverseSelection,
                                idempotency_key: Annotated[UUID, Header(alias='Idempotency-Key')],
                                principal=Depends(require_selection_reviewer), db=Depends(get_bcmd_db)):
    try:
        return claims.reverse(db, conflict_id, reason=body.reason, expected_version=body.expected_version,
                              key=idempotency_key, actor=principal, request_id=request_id_var.get())
    except Error as exc:
        raise db_failure(exc) from None


@router.get('/batches/{public_id}/{section}', summary='Read frozen members, DQA issues or audit history',
    description='Choose section=members, issues or audit. members returns the saved personal snapshot, '
                'not a fresh join to live source rows. HHID uses household_id; FamilyID uses the same '
                'value when its association exists, otherwise null. IP 0 maps to NO and unknown IP '
                'stays null. issues returns line number, field number, rule and severity without raw '
                'offending values. audit returns event, time, actor agency and request ID. '
                'Paginate each section separately. Purged batches return 410.',
    responses={200: {'description': 'A bounded section page.', 'content': {'application/json': {'examples': {
        'issues': {'value': {'items': [{'line_no': 1, 'rule_code': 'VALUE_REQUIRED', 'field_no': 16, 'severity': 'BLOCK'}], 'has_more': False}},
        'audit': {'value': {'items': [{'event_type': 'BATCH_CREATED', 'occurred_at': '2026-01-01T00:00:00Z', 'actor_agency': 'TEST', 'request_id': 'synthetic-request-id'}], 'has_more': False}},
        'members': {'value': {'items': [{'line_no': 1, 'dqa_status': 'PASS', 'region_name': 'Example region', 'region_psgc': '0100000000', 'province_name': 'Example province', 'province_psgc': '0101000000', 'municipality_name': 'Example municipality', 'municipality_psgc': '0101010000', 'barangay_name': 'Example barangay', 'barangay_psgc': '0101010001', 'hhid': '1001', 'family_id': None, 'member_ref': '2001', 'last_name': 'Synthetic', 'first_name': 'Example', 'middle_name': None, 'ext_name': None, 'sex': 'FEMALE', 'ip_indicator': None, 'date_of_birth_raw': '2001-01-01'}], 'has_more': False}},
        }}}}, 404: {'description': 'Batch not found.'}, 410: {'description': 'Batch personal data has been purged.'}})
def section(public_id: UUID, section: Literal['members', 'issues', 'audit'],
            limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10_000_000),
            principal=Depends(require_bcmd_user), db=Depends(get_bcmd_db)):
    cur = db.cursor(dictionary=True)
    try:
        batch = store.batch_row(cur, public_id)
        # batch_row holds a shared lock and checks PURGED before materializing data.
        if section == 'members':
            sql = (f'SELECT line_no,dqa_status,source_agency,course_type,{",".join(store.PERSONAL_COLUMNS)} '
                   'FROM bcmd_batch_member WHERE batch_id=%s ORDER BY line_no LIMIT %s OFFSET %s')
        elif section == 'issues':
            sql = '''SELECT m.line_no,i.rule_code,i.field_no,i.severity FROM bcmd_dqa_issue i
                LEFT JOIN bcmd_batch_member m ON m.batch_id=i.batch_id AND m.id=i.member_id
                WHERE i.batch_id=%s ORDER BY i.id LIMIT %s OFFSET %s'''
        else:
            sql = 'SELECT event_type,occurred_at,actor_agency,request_id FROM bcmd_audit_event WHERE batch_id=%s ORDER BY id LIMIT %s OFFSET %s'
        cur.execute(sql, (batch['id'], limit+1, offset))
        rows = cur.fetchall()
        return {'items': rows[:limit], 'has_more': len(rows)>limit}
    except Error as exc:
        raise db_failure(exc) from None
    finally:
        db.rollback()
        cur.close()
