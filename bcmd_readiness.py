"""Read-only, fail-closed BCMD activation checks. No DDL or privilege changes."""
import json
import logging
import re
import threading
import time
from pathlib import Path

from mysql.connector import Error

from errors import ApiError
from scripts.build_napa_dbgate_package import metadata_many
from scripts.migrate import discover

SOURCE_TABLES = ('enroll_matched_CHED_tbl', 'enroll_matched_TESDA_tbl', 'tbl_person', 'lib_partner_agency',
    'tbl_hh_roster', 'tbl_household', 'lib_psgc_barangay', 'lib_psgc_denorm',
    'tbl_family_selected', 'tbl_family_assoc_ops', 'vw_verify_member_bene')
# Sex, name extension and IP group lookups read by the batch source query (0027). They are in the
# application database, copied from the PPIS v3 libraries by docs/schema/bcmd_lookup_copy.sql, so
# BCMD needs no rights outside it.
REFERENCE_TABLES = ('lib_sex', 'lib_ext_name', 'lib_ip_group')
DQA_WRITE_TABLES = ('bcmd_dqa_run', 'bcmd_dqa_result')
# Duplicate-selection review (0023): the worker upserts conflicts, reviewers append decisions.
CLAIM_WRITE_TABLES = ('bcmd_claim_conflict', 'bcmd_claim_resolution', 'bcmd_claim_resolution_member')
# Durable BCMD-to-NAPA handoff (0026): appended, never updated.
HANDOFF_WRITE_TABLES = ('bcmd_napa_handoff',)
WRITE_TABLES = ('bcmd_batch', 'bcmd_batch_member', 'bcmd_dqa_issue', 'bcmd_audit_event', *DQA_WRITE_TABLES,
                *CLAIM_WRITE_TABLES, *HANDOFF_WRITE_TABLES)
UPDATE_TABLES = ('bcmd_batch', 'bcmd_batch_export', 'bcmd_dqa_run', 'bcmd_claim_conflict')
# Payment history (bcmd_payment_history): candidates, batch save, DQA and NAPA handoff read it; a
# BCMD account that cannot read it is not ready, rather than treating it as an empty history.
PAYMENT_HISTORY_TABLES = ('payment_transaction',)
READ_TABLES = (*WRITE_TABLES, 'bcmd_batch_export', 'bcmd_approval', 'schema_migrations',
               'app_settings', 'api_user_scopes', *SOURCE_TABLES, *REFERENCE_TABLES, *PAYMENT_HISTORY_TABLES)
ROOT = Path(__file__).resolve().parent
logger = logging.getLogger('tuloy.bcmd')

# A failing gate fails every BCMD request; log each distinct cause at most once a minute.
LOG_INTERVAL_SECONDS = 60.0
_MAX_LISTED = 10
_log_lock = threading.Lock()
_last_logged = (None, 0.0)


def unavailable(check=None, **detail):
    """The public 503 never says why. The cause rides on the exception for the log only:
    check names, versions, schema.table and privilege names, never account, role or grant text."""
    exc = ApiError(503, 'BCMD_NOT_READY', 'Batch service readiness checks did not pass')
    exc.readiness = {'readiness_check': check, **detail} if check else {}
    return exc


def failure_fields(exc):
    """Log fields naming the failed readiness check (also for a driver error raised mid-check)."""
    fields = dict(getattr(exc, 'readiness', None) or {})
    if isinstance(exc, Error) and 'errno' not in fields:
        fields['errno'] = exc.errno
    fields.setdefault('readiness_check', 'unknown')
    return fields


def log_not_ready(exc):
    global _last_logged
    fields = failure_fields(exc)
    key, now = json.dumps(fields, sort_keys=True, default=str), time.monotonic()
    with _log_lock:
        if key == _last_logged[0] and now - _last_logged[1] < LOG_INTERVAL_SECONDS:
            return
        _last_logged = (key, now)
    logger.warning('bcmd_not_ready', extra=fields)


def _missing_rights(required, observed):
    missing = [f'{schema}.{table}:{",".join(sorted(rights - observed.get((schema, table), set())))}'
               for (schema, table), rights in sorted(required.items())
               if not rights <= observed.get((schema, table), set())]
    if missing:
        raise unavailable('grants', problem='missing_rights', missing=missing[:_MAX_LISTED],
                          missing_count=len(missing))


def validate_grants(grants, database, case_insensitive=False):
    """Accept only explicit direct table grants; reject roles and broad privileges.

    This deliberately rejects ALL PRIVILEGES, wildcard grants, column grants,
    GRANT OPTION, proxy privileges and role assignments rather than interpreting
    an incomplete subset of MySQL's effective authorization model.
    """
    allowed = {(database, t): {'SELECT'} | ({'INSERT'} if t in WRITE_TABLES else set())
               for t in READ_TABLES}
    for table in UPDATE_TABLES:
        allowed[(database, table)] |= {'UPDATE'}
    allowed[(database, 'bcmd_batch_export')] |= {'INSERT'}
    if case_insensitive:
        allowed = {(schema, table.lower()): rights for (schema, table), rights in allowed.items()}
    observed = {}
    for grant in grants:
        if re.fullmatch(r'GRANT USAGE ON \*\.\* TO .+', grant) and ' WITH ' not in grant:
            continue
        match = re.fullmatch(r'GRANT ([A-Z, ]+) ON `([^`]+)`\.`([^`]+)` TO .+', grant)
        if not match or ' WITH ' in grant:
            raise unavailable('grants', problem='unsupported_grant_form')
        permissions, schema, table = match.groups()
        if case_insensitive:
            table = table.lower()
        rights = set(permissions.split(', '))
        if not rights <= allowed.get((schema, table), set()):
            raise unavailable('grants', problem='rights_beyond_allowed', object=f'{schema}.{table}',
                              rights=sorted(rights - allowed.get((schema, table), set())))
        observed.setdefault((schema, table), set()).update(rights)
    _missing_rights(allowed, observed)


def _schema_pattern_matches(pattern, database):
    """MySQL database-level grants can contain escaped SQL wildcard patterns."""
    expression = ''
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == '\\' and index + 1 < len(pattern):
            index += 1
            expression += re.escape(pattern[index])
        else:
            expression += '.*' if char == '%' else '.' if char == '_' else re.escape(char)
        index += 1
    return re.fullmatch(expression, database) is not None


def validate_existing_account_grants(grants, database):
    """Check required rights, without requiring a new or narrower account.

    Martin explicitly authorized reuse of his existing account. Extra rights are
    left untouched; this mode does not claim least privilege. Active roles and
    partial revokes are unsupported and fail closed instead of overstating rights.
    """
    required = {(database, t): {'SELECT'} | ({'INSERT'} if t in WRITE_TABLES else set())
                for t in READ_TABLES}
    for table in UPDATE_TABLES:
        required[(database, table)] |= {'UPDATE'}
    required[(database, 'bcmd_batch_export')] |= {'INSERT'}
    observed = {key: set() for key in required}
    for grant in grants:
        if grant.startswith('REVOKE '):
            raise unavailable('grants', problem='partial_revoke')
        match = re.fullmatch(r'GRANT ([A-Z, ]+) ON (\*|`(?:``|[^`])+`)\.(\*|`(?:``|[^`])+`) TO .+', grant)
        if not match:
            continue
        permissions, schema, table = match.groups()
        rights = {'SELECT', 'INSERT', 'UPDATE'} if permissions == 'ALL PRIVILEGES' else set(permissions.split(', '))
        for key in required:
            target_schema, target_table = key
            if schema == '*' and table == '*':
                matches = True
            elif schema != '*' and table == '*':
                matches = _schema_pattern_matches(schema[1:-1].replace('``', '`'), target_schema)
            else:
                matches = (schema[1:-1].replace('``', '`') == target_schema
                           and table[1:-1].replace('``', '`') == target_table)
            if matches:
                observed[key].update(rights)
    _missing_rights(required, observed)


def _ledger_diff(applied, expected):
    """Versions only: which migrations are missing, recorded differently, or unknown to this code."""
    have = {row['version']: (row['name'], row['checksum']) for row in applied}
    want = {row['version']: (row['name'], row['checksum']) for row in expected}
    return {'missing': sorted(set(want) - set(have))[:_MAX_LISTED],
            'different': sorted(v for v in set(want) & set(have) if have[v] != want[v])[:_MAX_LISTED],
            'extra': sorted(set(have) - set(want))[:_MAX_LISTED],
            'latest_applied': max(have) if have else None}


def verify(db, settings):
    """Verify on each checkout: identities, current grants, ledger and table shape.

    No success cache: privilege revocation or schema drift must fail the next
    request. Only metadata, the migration ledger and whether each lookup table has
    a row are read, never source rows.
    """
    cur = db.cursor(dictionary=True)
    step = 'identity'
    try:
        cur.execute('SELECT DATABASE() AS db,@@hostname AS host,CURRENT_USER() AS account,CURRENT_ROLE() AS roles,'
                    '@@lower_case_table_names AS table_case,@@server_id AS server_id,@@read_only AS read_only')
        identity = cur.fetchone()
        expected_db = settings.bcmd_expected_database or settings.ods.database
        if identity['db'] != expected_db:
            raise unavailable('identity', problem='database', expected=expected_db, actual=identity['db'])
        accepted = settings.bcmd_hostnames
        if accepted and identity['host'] not in accepted:
            # Which server answered and where the API pointed: tells a replica or a wrong DB_HOST
            # from a renamed primary.
            raise unavailable('identity', problem='hostname', expected=','.join(accepted),
                              actual=identity['host'], server_id=identity.get('server_id'),
                              read_only=identity.get('read_only'),
                              target=f'{settings.ods.host}:{settings.ods.port}', via_tunnel=bool(settings.ssh_host))
        if identity['account'].split('@', 1)[0] != settings.bcmd_user:
            raise unavailable('identity', problem='account')
        if identity['roles'] != 'NONE':
            raise unavailable('identity', problem='active_roles')
        step = 'grants'
        cur.execute('SHOW GRANTS FOR CURRENT_USER')
        grants = [next(iter(row.values())) for row in cur.fetchall()]
        if settings.bcmd_use_existing_account:
            validate_existing_account_grants(grants, expected_db)
        else:
            validate_grants(grants, expected_db, case_insensitive=bool(identity.get('table_case')))
        step = 'migration_ledger'
        cur.execute('SELECT version,name,checksum FROM schema_migrations ORDER BY version')
        expected = [{'version': v, 'name': n, 'checksum': h} for v, n, h, sql in discover()]
        applied = cur.fetchall()
        if applied != expected:
            raise unavailable('migration_ledger', **_ledger_diff(applied, expected))
        step = 'table_shape'
        schema = json.loads((ROOT/'docs/schema/dbgate-apply/expected_schema.json').read_text(encoding='utf8'))
        tables = ('app_settings', *WRITE_TABLES, 'bcmd_batch_export', 'bcmd_approval', *REFERENCE_TABLES)
        actual = metadata_many(cur, tables)
        for table in tables:
            # app_settings is not read by BCMD; its SELECT grant is required for this check.
            if actual[table] != schema[table]:
                raise unavailable('table_shape', table=table,
                                  parts=sorted(k for k in schema[table] if actual[table].get(k) != schema[table][k]),
                                  table_missing=not actual[table].get('table'))
        step = 'reference_data'
        # Empty lookups (0027 applied, copy not yet run) would block every member's mapping.
        cur.execute('SELECT ' + ','.join(f'EXISTS(SELECT 1 FROM `{t}`) AS `{t}`' for t in REFERENCE_TABLES))
        filled = cur.fetchone() or {}
        empty = [t for t in REFERENCE_TABLES if not filled.get(t)]
        if empty:
            raise unavailable('reference_data', problem='empty_lookup', tables=empty)
        step = 'dqa_columns'
        cur.execute('SELECT public_id,batch_id,status,ruleset_sha256,processed,total,lease_token,lease_until FROM bcmd_dqa_run LIMIT 0')
        cur.fetchall()
        cur.execute('SELECT run_id,member_id,line_no,outcome,findings_json FROM bcmd_dqa_result LIMIT 0')
        cur.fetchall()
        step = 'api_user_scopes_expiry'
        cur.execute("""SELECT IS_NULLABLE FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='api_user_scopes' AND COLUMN_NAME='expires_at'""")
        if cur.fetchone() != {'IS_NULLABLE': 'YES'}:
            raise unavailable('api_user_scopes_expiry')
    except Error as exc:
        # A driver error mid-check (missing table or column, revoked right) keeps its type for
        # callers; the step it happened in is attached for the log.
        if not getattr(exc, 'readiness', None):
            exc.readiness = {'readiness_check': step, 'errno': exc.errno}
        raise
    finally:
        db.rollback()
        cur.close()
