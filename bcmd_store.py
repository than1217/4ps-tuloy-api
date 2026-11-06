"""Local BCMD candidate: transactional frozen snapshots and durable audit.

No DDL, grants, approval writes, payment actions or export are performed here.
"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from mysql.connector import Error

from errors import ApiError
from bcmd_access import affiliation
from napa_detail import FIELDS, NapaValidationError, serialize_detail
from napa_mapping import (MappingBlocked, ReferenceEntry, resolve_extension,
                          resolve_family, resolve_hhid, resolve_ip, resolve_sex)

# Shared by candidate discovery and the write-time source query.
import bcmd_payment_history as payment_history
from bcmd_candidates import (AGE_MAX, AGE_MIN, CATEGORIES, SOURCES, candidate_query, manila_today,
                             selection_branches)

LEGACY_VERSION = 'tk-ched-enrolled-y1-s1-v1'
# CHED-only policies of batches saved before the unified source. They stay supported as saved.
CHED_POLICY_VERSIONS = {'TK': 'tk-ched-enrolled-y1-s1-age-v2', 'TA': 'ta-ched-enrolled-age-v1',
                        'TT': 'tt-ched-graduated-age-v1'}
# New batches: CHED degree or TESDA diploma (no CHED match), and payment history at selection.
POLICY_VERSIONS = {'TK': 'tk-unified-enrolled-y1-s1-age-v3', 'TA': 'ta-unified-enrolled-age-v2',
                   'TT': 'tt-unified-graduated-age-v2'}
_SHARED = {'family_missing': None, 'ip_zero': 'NO', 'ip_null': None, 'geography': 'psgc10',
           'source': 'CHED', 'optional_names': ['middle_name', 'ext_name'],
           'retention': {'automatic_purge': False, 'period_days': None,
                         'mode': 'indefinite_until_manual_purge'}}
_UNIFIED = {**_SHARED, 'source': 'CHED_DEGREE_OR_TESDA_DIPLOMA_WITHOUT_CHED_MATCH',
            'short_course': 'not_selectable_policy_pending', 'payment_history': payment_history.config()}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _policy(category, version, extra, shared=_SHARED):
    canonical = _canonical({**shared, 'mapping': version, 'eligibility': version, **extra})
    return category, canonical, hashlib.sha256(canonical.encode()).hexdigest()


def _rules(code):
    spec = CATEGORIES[code]
    return {'category': code, 'enrollment_status': spec['status'], 'year_level': spec['year_level'],
            'semester': spec['semester'], 'beneficiary_view': 'vw_verify_member_bene',
            'age': {'minimum': AGE_MIN, 'maximum': AGE_MAX, 'inclusive': True, 'as_of': 'selection_date_Asia/Manila'}}


# Every saved batch keeps the exact policy it was selected with. Legacy TK v1 batches
# (no age rule at selection) stay supported: the DQA run applies the age rule to them.
POLICIES = {LEGACY_VERSION: _policy('TK', LEGACY_VERSION, {})}
for _code, _version in CHED_POLICY_VERSIONS.items():
    POLICIES[_version] = _policy(_code, _version, _rules(_code))
for _code, _version in POLICY_VERSIONS.items():
    POLICIES[_version] = _policy(_code, _version, _rules(_code), _UNIFIED)
VERSION = POLICY_VERSIONS['TK']
_, CONFIG, CONFIG_HASH = POLICIES[VERSION]
PUBLIC_COLUMNS = ('public_id,name,category,source_agency,status,member_count,dqa_outcome,'
                  'dqa_block_count,dqa_warn_count,export_seq,created_at,payment_excluded_count')


def policy_supported(batch):
    entry = POLICIES.get(batch.get('policy_version'))
    if not entry or entry[0] != batch.get('category'):
        return False
    canonical = batch.get('dqa_config_canonical') or ''
    return (batch.get('dqa_config_sha256') == entry[2] == batch.get('dqa_ruleset_sha256')
            and hashlib.sha256(canonical.encode()).hexdigest() == entry[2] and canonical == entry[1])
PERSONAL_COLUMNS = (
    'region_name', 'region_psgc', 'province_name', 'province_psgc',
    'municipality_name', 'municipality_psgc', 'barangay_name', 'barangay_psgc',
    'hhid', 'family_id', 'member_ref', 'last_name', 'first_name', 'middle_name',
    'ext_name', 'sex', 'ip_indicator', 'date_of_birth_raw',
)
# source_agency and course_type are per member: a batch may mix CHED and TESDA records.
MEMBER_COLUMNS = ('line_no', 'tuloy_id', 'academic_year', 'semester', 'source_record_id',
                  'entitlement_hash', 'dqa_status', *PERSONAL_COLUMNS, 'source_agency', 'course_type')

SOURCE_SQL = """SELECT u.source_record_id,u.source_agency,u.course_type,u.tuloy_id,u.member_id,
    u.academic_year,u.semester,p.first_name,p.middle_name,p.last_name,p.date_of_birth,
    p.sex_id,s.name AS sex_name,s.archived AS sex_archived,
    p.ext_name_id,e.name AS extension_name,e.archived AS extension_archived,
    p.ip_group_id,i.name AS ip_group_name,i.archived AS ip_archived,
    r.hh_roster_id,h.household_id,b.name AS barangay_name,b.psa_code AS barangay_psgc,
    g.region_name,g.region_psa_code AS region_psgc,g.province_name,
    g.province_psa_code AS province_psgc,g.city_name AS municipality_name,
    g.city_psa_name AS municipality_psgc,fs.family_id AS association_family_id,
    f.fam_assoc_id,f.archived AS family_archived
    FROM ({selected}) u
    JOIN tbl_person p ON p.person_id=u.member_id
    LEFT JOIN tbl_hh_roster r ON r.person_id=p.person_id
    LEFT JOIN tbl_household h ON h.household_id=r.household_id
    LEFT JOIN lib_psgc_barangay b ON b.barangay_id=h.barangay_id
    LEFT JOIN lib_psgc_denorm g ON g.barangay_id=b.barangay_id
    LEFT JOIN tbl_family_selected fs ON fs.household_id=h.household_id
    LEFT JOIN tbl_family_assoc_ops f ON f.family_id=fs.family_id
      AND f.person_id=p.person_id AND f.hh_roster_id=r.hh_roster_id AND f.archived=0
    LEFT JOIN lib_sex s ON s.sex_id=p.sex_id
    LEFT JOIN lib_ext_name e ON e.ext_name_id=p.ext_name_id
    LEFT JOIN lib_ip_group i ON i.ip_group_id=p.ip_group_id
    ORDER BY u.source_order,u.source_record_id,r.hh_roster_id,fs.family_id,f.fam_assoc_id LIMIT %s"""


def source_sql(branches):
    """SOURCE_SQL over exactly the selected, still eligible records of each source."""
    selected = ' UNION ALL '.join(
        f"(SELECT {src['order']} AS source_order,'{agency}' AS source_agency,'{src['course_type']}' AS course_type,"
        f"m.id AS source_record_id,m.tuloy_id,m.member_id,{src['academic_year']} AS academic_year,"
        f"{src['semester']} AS semester FROM {src['from']} WHERE {' AND '.join(clauses)})"
        for agency, src, clauses, _ in branches)
    return SOURCE_SQL.format(selected=selected), [p for *_, params in branches for p in params]
# Bound on joined source rows: guards against an association fan-out, not a batch-size limit.
MIN_SOURCE_ROWS, SOURCE_ROWS_PER_RECORD = 2000, 10
INSERT_CHUNK = 500


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     default=str).encode()).hexdigest()


def _insert(cursor, table, values):
    # Table and column names only ever come from fixed internal call sites.
    columns = list(values)
    cursor.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})",
                   tuple(values[k] for k in columns))
    return cursor.lastrowid


def _insert_many(cursor, table, columns, rows):
    # Table and column names only ever come from fixed internal call sites.
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})"
    for start in range(0, len(rows), INSERT_CHUNK):
        cursor.executemany(sql, rows[start:start + INSERT_CHUNK])


def _audit(cursor, batch_id, actor, request_id, event):
    from bcmd_actor import actor_values
    _insert(cursor, 'bcmd_audit_event', {'batch_id': batch_id, 'event_type': event,
            **actor_values(actor, 'actor'), 'actor_agency': affiliation(actor), 'request_id': request_id})


def public_batch(row, latest_dqa=None, export_ready=False):
    data = {k: row[k] for k in PUBLIC_COLUMNS.split(',')}
    created = data['created_at']
    if isinstance(created, datetime):
        data['created_at'] = created.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
    status = latest_dqa['status'] if latest_dqa else row.get('latest_dqa_status') or 'NOT_RUN'
    return {**data, 'export_ready': export_ready, 'dqa_run_status': status, 'latest_dqa': latest_dqa}


def snapshot(rows, category):
    """Map one source record; reject multiplicity before selecting any source row."""
    # Family rows may differ; every other projected field must be identical.
    variable = {'association_family_id', 'fam_assoc_id', 'family_archived'}
    signatures = {digest({k: v for k, v in row.items() if k not in variable}) for row in rows}
    if len(signatures) != 1:
        raise ApiError(422, 'SOURCE_AMBIGUOUS', 'Source association is ambiguous')
    row = rows[0]
    if row['source_agency'] not in SOURCES or not row['tuloy_id']:
        raise ApiError(422, 'SOURCE_NOT_READY', 'Source does not meet the candidate contract')
    # Graduates (TT) may have no semester; enrolled categories need the full period.
    if not row['academic_year'] or (row['semester'] is None and category != 'TT'):
        raise ApiError(422, 'SOURCE_NOT_READY', 'Source enrollment period is missing')
    result = {field: row.get(field) for field in PERSONAL_COLUMNS}
    result.update(member_ref=str(row['member_id']), ext_name=None, sex=None, ip_indicator=None,
                  date_of_birth_raw=str(row['date_of_birth']) if row['date_of_birth'] is not None else None)
    issues = []
    catalogs = {
        'sex': {row['sex_id']: ReferenceEntry(row['sex_name'], row['sex_archived'] == 0)} if row['sex_name'] else {},
        'extension': {row['ext_name_id']: ReferenceEntry(row['extension_name'], row['extension_archived'] == 0)} if row['extension_name'] else {},
        'ip': {row['ip_group_id']: ReferenceEntry(row['ip_group_name'], row['ip_archived'] == 0)} if row['ip_group_name'] else {},
    }
    candidates = [dict(person_id=row['member_id'], hh_roster_id=row['hh_roster_id'],
                       household_id=row['household_id'], family_id=r['association_family_id'],
                       archived=r['family_archived']) for r in rows if r['fam_assoc_id'] is not None]
    operations = {
        'hhid': lambda: resolve_hhid(row['household_id']),
        'family_id': lambda: resolve_family(person_id=row['member_id'], roster_id=row['hh_roster_id'],
                                           household_id=row['household_id'], candidates=candidates),
        'sex': lambda: resolve_sex(row['sex_id'], catalogs['sex']),
        'ext_name': lambda: resolve_extension(row['ext_name_id'], catalogs['extension']),
        'ip_indicator': lambda: resolve_ip(person_group_id=row['ip_group_id'], household_group_id=None,
                                          source='person', catalog=catalogs['ip']),
    }
    for field, operation in operations.items():
        try:
            result[field] = operation()
        except MappingBlocked as exc:
            issues.append((exc.field, exc.code))
    csv_row = {**result, 'line_no': 1, 'member_id': result['member_ref'],
               'member_last_name': row['last_name'], 'member_first_name': row['first_name'],
               'member_middle_name': row['middle_name'], 'member_ext_name': result['ext_name'],
               'date_of_birth': row['date_of_birth'], 'source_agency': row['source_agency']}
    try:
        serialize_detail([csv_row], category)
    except NapaValidationError as exc:
        issues.append((exc.field, exc.code))
    result.update(source_agency=row['source_agency'], course_type=row['course_type'],
                  tuloy_id=row['tuloy_id'], academic_year=row['academic_year'],
                  semester=row['semester'] or 0, source_record_id=row['source_record_id'],
                  entitlement_hash=digest([category, row['tuloy_id'], row['academic_year'], row['semester']]),
                  dqa_status='BLOCK' if issues else 'PASS')
    return result, sorted(set(issues))


def _name_taken(cursor, name):
    # The column collation is case- and accent-insensitive, matching uq_bcmd_batch_name.
    cursor.execute('SELECT 1 AS taken FROM bcmd_batch WHERE name=%s LIMIT 1', (name,))
    return cursor.fetchone() is not None


def batch_source(agencies):
    """CHED or TESDA when every member has that source, MIXED otherwise. Never used per member."""
    return agencies.pop() if len(agencies) == 1 else 'MIXED'


def create_batch(db, *, actor, request_id, key, name, category, record_ids=None, record_refs=None,
                 criteria=None, record_count=None):
    from bcmd_actor import actor_column, actor_values
    if category not in POLICY_VERSIONS:
        raise ApiError(422, 'VALIDATION_ERROR', 'Unknown category')
    creator_column = actor_column(actor)
    today = manila_today()
    policy_version = POLICY_VERSIONS[category]
    _, canonical, config_hash = POLICIES[policy_version]
    if criteria is not None:
        selection = {'criteria': criteria, 'record_count': record_count}
    elif record_refs is not None:
        selection = {'record_refs': sorted(record_refs)}
    else:
        selection = {'record_ids': sorted(record_ids)}
    request_hash = digest({'name': name, 'category': category, **selection})
    cursor = db.cursor(dictionary=True)
    try:
        db.start_transaction(isolation_level='REPEATABLE READ', consistent_snapshot=True)
        cursor.execute(f'SELECT id,request_hash,{PUBLIC_COLUMNS} FROM bcmd_batch WHERE {creator_column}=%s AND idempotency_key=%s', (actor.id, key))
        previous = cursor.fetchone()
        if previous:
            if previous['request_hash'] != request_hash:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Key was used for a different request')
            db.rollback()
            return public_batch(previous)
        if _name_taken(cursor, name):
            raise ApiError(409, 'BATCH_NAME_EXISTS', 'A batch with this name already exists; generate a new name')
        payment_excluded = None  # not counted for explicit selections
        if criteria is not None:
            # The same filters as the preview, inside this transaction: blocked records cannot be selected.
            sql, params = candidate_query(criteria, category=category, today=today, ids_only=True)
            cursor.execute(sql, (*params, record_count, 0))
            selected = [(row['source_agency'], row['source_record_id']) for row in cursor.fetchall()]
            if len(selected) != record_count:
                raise ApiError(422, 'RECORD_COUNT_EXCEEDS_ELIGIBLE',
                               f'Only {len(selected)} eligible records match; {record_count} were requested')
            sql, params = candidate_query(criteria, category=category, today=today, count=True, payments='blocked')
            try:
                cursor.execute(sql, params)
                payment_excluded = int(cursor.fetchone()['total_count'])
            except Error as exc:
                if exc.errno != 3024:  # informational only: a count that timed out stays NULL
                    raise
        elif record_refs is not None:
            selected = [tuple(ref.split(':')[:2]) for ref in record_refs]
            selected = [(agency, int(source_id)) for agency, source_id in selected]
        else:
            selected = [('CHED', source_id) for source_id in record_ids]
        refs = defaultdict(set)
        for agency, source_id in selected:
            refs[agency].add(source_id)
        sql, params = source_sql(selection_branches(category, today, refs))
        row_limit = max(MIN_SOURCE_ROWS, SOURCE_ROWS_PER_RECORD * len(selected))
        cursor.execute(sql, (*params, row_limit + 1))
        rows = cursor.fetchall()
        if len(rows) > row_limit:
            raise ApiError(422, 'SOURCE_AMBIGUOUS', 'Too many source associations')
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row['source_agency'], row['source_record_id'])].append(row)
        # A record that is missing, no longer eligible or now blocked by a recorded payment refuses the save.
        if set(grouped) != set(selected):
            raise ApiError(422, 'SOURCE_NOT_ELIGIBLE', 'Some selected records are missing or not currently eligible')
        order = {agency: src['order'] for agency, src in SOURCES.items()}
        members = [snapshot(grouped[ref], category) for ref in sorted(grouped, key=lambda r: (order[r[0]], r[1]))]
        hashes = [member['entitlement_hash'] for member, _ in members]
        if len(set(hashes)) != len(hashes):
            raise ApiError(409, 'DUPLICATE_EPISODE', 'The selection repeats an enrollment episode')
        blocked = sum(bool(issues) for _, issues in members)
        block_issues = sum(len(issues) for _, issues in members)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        public_id = str(uuid4())
        agencies = {member['source_agency'] for member, _ in members}
        chosen = ({'record_ids': sorted(source_id for _, source_id in selected)} if agencies == {'CHED'}
                  else {'record_refs': sorted(f"{a}:{i}:{SOURCES[a]['course_type']}" for a, i in selected)})
        values = dict(public_id=public_id, name=name, category=category, source_agency=batch_source(set(agencies)),
            criteria=json.dumps({**selection, **chosen}), status='DQA_COMPLETED', payment_excluded_count=payment_excluded,
            policy_version=policy_version, entitlement_def_version='local-v1', dqa_ruleset_id='napa-local',
            dqa_ruleset_version='1', dqa_ruleset_sha256=config_hash, dqa_config_sha256=config_hash,
            dqa_config_canonical=canonical, dqa_evaluation_date=today, snapshot_started_at=now,
            source_input_sha256=digest(rows), dqa_completed_at=now, candidate_count=len(members),
            member_count=len(members), excluded_not_eligible=0, excluded_no_tuloy_id=0,
            excluded_duplicate_episode=0, dqa_pass_count=len(members)-blocked, dqa_warn_count=0,
            dqa_block_count=blocked, dqa_warn_issue_count=0, dqa_block_issue_count=block_issues,
            dqa_outcome='BLOCKED' if blocked else 'CLEAN', **actor_values(actor),
            created_by_agency=affiliation(actor), request_id=request_id, idempotency_key=key, request_hash=request_hash)
        batch_id = _insert(cursor, 'bcmd_batch', values)
        for line, (member, _) in enumerate(members, 1):
            member['line_no'] = line
        # Multi-row inserts keep large batches fast; ids are read back by line number, never assumed.
        _insert_many(cursor, 'bcmd_batch_member', ('batch_id', *MEMBER_COLUMNS),
                     [(batch_id, *(member[k] for k in MEMBER_COLUMNS)) for member, _ in members])
        if block_issues:
            cursor.execute('SELECT id,line_no FROM bcmd_batch_member WHERE batch_id=%s', (batch_id,))
            member_ids = {row['line_no']: row['id'] for row in cursor.fetchall()}
            _insert_many(cursor, 'bcmd_dqa_issue', ('batch_id', 'member_id', 'rule_code', 'field_no', 'severity'),
                         [(batch_id, member_ids[member['line_no']], code, FIELDS.index(field)+1 if field in FIELDS else 0, 'BLOCK')
                          for member, issues in members for field, code in issues])
        _audit(cursor, batch_id, actor, request_id, 'BATCH_CREATED')
        _audit(cursor, batch_id, actor, request_id, 'DQA_COMPLETED')
        cursor.execute(f'SELECT {PUBLIC_COLUMNS} FROM bcmd_batch WHERE id=%s', (batch_id,))
        created = cursor.fetchone()
        db.commit()
        return public_batch(created)
    except Error as exc:
        db.rollback()
        if exc.errno == 1062:
            # A concurrent save with the same key wins: retrying that key replays it.
            # Otherwise the unique name index caught a concurrent save with this name.
            cursor.execute(f'SELECT 1 AS same FROM bcmd_batch WHERE {creator_column}=%s AND idempotency_key=%s', (actor.id, key))
            same_key = cursor.fetchone() is not None
            name_taken = not same_key and _name_taken(cursor, name)
            db.rollback()
            if name_taken:
                raise ApiError(409, 'BATCH_NAME_EXISTS', 'A batch with this name already exists; generate a new name') from None
            raise ApiError(409, 'BATCH_CONFLICT', 'Concurrent request conflicts; retry the same key') from None
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()


def batch_row(cursor, public_id):
    cursor.execute(f'SELECT id,{PUBLIC_COLUMNS} FROM bcmd_batch WHERE public_id=%s FOR SHARE', (str(public_id),))
    row = cursor.fetchone()
    if not row:
        raise ApiError(404, 'NOT_FOUND', 'Batch not found')
    if row['status'] == 'PURGED':
        raise ApiError(410, 'BATCH_PURGED', 'Batch personal data is no longer available')
    return row
