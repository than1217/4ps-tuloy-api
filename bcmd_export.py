"""Bounded, transactional NAPA exports of frozen batches; no live source joins.

Three files per batch, each built from one completed DQA run (NAPA-03, NAPA-05, NAPA-06):
DETAIL (NAPA_V2_RAW_DETAILED, passed records only), SUMMARY (NAPA_V2_SUMMARY, passed
records grouped by geography) and FAILED (failed and for-verification records with
remarks). Exports from before DQA runs (dqa_run_id NULL) stay downloadable as before.
"""
import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path

import bcmd_payment_history as payment_history
import bcmd_store as store
from bcmd_access import affiliation
from bcmd_actor import actor_column, actor_values
from bcmd_dqa_rules import HASH as DQA_HASH, VERSION as DQA_VERSION
from errors import ApiError
from napa_detail import FORMULA_PREFIXES, HEADER_FILE, NapaValidationError, serialize_detail

ROOT = Path(__file__).resolve().parent
VERSION = 'detail-v1'  # Legacy DETAIL of every member, from before DQA runs.
FILE_TYPES = ('DETAIL', 'SUMMARY', 'FAILED')
# Files are never stored: each download rebuilds the bytes from frozen data and verifies them against the
# hashes saved at export time. Any change to a file's bytes for the same data (headers, columns, formatting)
# therefore needs a new version here, and the previous builder must stay so issued files keep verifying.
SERIALIZATION = {'DETAIL': 'detail-passed-v1', 'SUMMARY': 'summary-v1', 'FAILED': 'failed-v2'}
# failed-v1 wrote FOR VERIFICATION for a REVIEW record; failed-v2 writes FAILED for every row. Issued v1 files keep their bytes.
DOWNLOADABLE = {'DETAIL': {'detail-passed-v1'}, 'SUMMARY': {'summary-v1'}, 'FAILED': {'failed-v1', 'failed-v2'}}
FILE_PREFIX = {'DETAIL': 'NAPA-DETAILED', 'SUMMARY': 'NAPA-V2-SUMMARY', 'FAILED': 'NAPA-FAILED'}
SUMMARY_HEADER_FILE = ROOT / 'docs' / 'templates' / 'napa-summary-headers.csv'
FAILED_HEADER_FILE = ROOT / 'docs' / 'templates' / 'napa-failed-headers.csv'
GEOGRAPHY = ('region_name', 'region_psgc', 'province_name', 'province_psgc', 'municipality_name',
             'municipality_psgc', 'barangay_name', 'barangay_psgc')
CATEGORIES = ('TK', 'TA', 'TT')
SOURCES = ('CHED', 'TESDA')


def _headers(path):
    with path.open(encoding='utf-8', newline='') as handle:
        return tuple(next(csv.reader(handle)))


SUMMARY_HEADERS = _headers(SUMMARY_HEADER_FILE)
FAILED_HEADERS = _headers(FAILED_HEADER_FILE)


def _group_hash(paths):
    return hashlib.sha256(b''.join(p.name.encode() + b'\0' + p.read_bytes().replace(b'\r\n', b'\n') + b'\0'
                                   for p in paths)).hexdigest()


# Header hash recorded on exports made before the SUMMARY and FAILED templates existed.
LEGACY_HEADER_SHA256 = _group_hash([HEADER_FILE])
HEADER_FILES = {'DETAIL': HEADER_FILE, 'SUMMARY': SUMMARY_HEADER_FILE, 'FAILED': FAILED_HEADER_FILE}


def header_sha256(file_type):
    """Hash of this file type's own header template, so editing one template never affects another file."""
    return _group_hash([HEADER_FILES[file_type]])


def artifacts():
    """Content-addressed review artifacts, also printed by the operator command."""
    groups = {
        'HEADER_FORMAT': [HEADER_FILE, SUMMARY_HEADER_FILE, FAILED_HEADER_FILE],
        'COLUMN_MAPPING': [ROOT / 'napa_mapping.py', ROOT / 'bcmd_store.py'],
        'DQA_RULES': [ROOT / 'napa_detail.py', ROOT / 'bcmd_export.py', ROOT / 'bcmd_dqa_rules.py',
                      ROOT / 'bcmd_candidates.py', ROOT / 'bcmd_payment_history.py', ROOT / 'bcmd_eligibility.py'],
        'DPO_DISCLOSURE': [ROOT / 'docs/napa-disclosure.md'],
    }
    return {gate: _group_hash(paths) for gate, paths in groups.items()}


ROLES = {'HEADER_FORMAT': {'BCMD_OWNER'}, 'COLUMN_MAPPING': {'DBA', 'DATA_OWNER'},
         'DQA_RULES': {'BCMD_OWNER'}, 'DPO_DISCLOSURE': {'DPO'}, 'PAYROLL_SEPARATION': {'MARTIN'}}


def blockers(cur):
    """Export authorization follows DQA and access checks; owner attestations are historical only.

    Keep the compatibility helper for existing callers without reading or changing approval rows.
    """
    return []


def require_approvals(cur):
    if blockers(cur):
        raise ApiError(409, 'NAPA_APPROVAL_REQUIRED', 'Current export approvals are incomplete')


def safe_name(name):
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', unicodedata.normalize('NFKD', name or '')).strip('._-')
    return cleaned[:100] or 'batch'


def filename(batch, file_type):
    return f"{FILE_PREFIX[file_type]}-{safe_name(batch['name'])}.csv"


def latest_run(cur, batch):
    cur.execute('SELECT * FROM bcmd_dqa_run WHERE batch_id=%s ORDER BY run_no DESC LIMIT 1', (batch['id'],))
    return cur.fetchone()


def run_current(run):
    return run['ruleset_version'] == DQA_VERSION and run['ruleset_sha256'] == DQA_HASH


def run_reasons(run, batch):
    """Why the latest run cannot be used. A duplicate-selection decision recorded after the run
    started changed the batch's claim epoch, so only a fresh run reflects it."""
    if not run:
        return ['DQA_NOT_RUN']
    if run['status'] in ('QUEUED', 'RUNNING', 'RETRYING'):
        return ['DQA_IN_PROGRESS']
    if run['status'] == 'FAILED':
        return ['DQA_FAILED']
    if not run_current(run):
        return ['DQA_RULESET_OUTDATED']
    if run['claim_epoch'] != batch['claim_epoch']:
        return ['DQA_REFRESH_REQUIRED']
    return []


def latest_handoff(cur, batch):
    cur.execute('SELECT h.*,r.public_id AS dqa_run_public_id,r.run_no AS dqa_run_no FROM bcmd_napa_handoff h '
                'JOIN bcmd_dqa_run r ON r.id=h.dqa_run_id WHERE h.batch_id=%s ORDER BY h.id DESC LIMIT 1', (batch['id'],))
    return cur.fetchone()


def handoff_reasons(handoff, run, batch):
    """DQA completion makes NAPA available without a separate manual handoff."""
    if batch['status'] == 'PURGED':
        return ['BATCH_PURGED']
    return run_reasons(run, batch)


def _utc(value):
    return value.isoformat(timespec='milliseconds') + 'Z' if value is not None else None


def handoff_public(handoff):
    return {'public_id': handoff['public_id'], 'dqa_run_id': handoff['dqa_run_public_id'],
            'dqa_run_no': handoff['dqa_run_no'], 'passed_count': handoff['passed_count'],
            'actor_agency': handoff['actor_agency'], 'created_at': _utc(handoff['created_at'])}


def handoff_view(handoff, run, batch):
    """VALID (new files may be made), STALE (with reasons) or NOT_RECORDED, and the latest handoff."""
    reasons = handoff_reasons(handoff, run, batch)
    status = 'STALE' if reasons and (run or handoff) else 'NOT_RECORDED' if reasons else 'VALID'
    current = handoff_public(handoff) if handoff and run and handoff['dqa_run_id'] == run['id'] else None
    if not reasons:
        current = {**(current or {}), 'dqa_run_id': run['public_id'], 'dqa_run_no': run['run_no'],
                   'passed_count': run['passed']}
    return {'status': status, 'reasons': reasons, 'current': current, 'automatic': True}


def payment_recheck(cur, batch, run):
    """Every PASS record of the run against payment history now: a new file never silently drops one."""
    cur.execute("SELECT m.member_ref,m.academic_year,m.semester FROM bcmd_dqa_result r "
                "JOIN bcmd_batch_member m ON m.id=r.member_id WHERE r.run_id=%s AND r.outcome='PASS'", (run['id'],))
    members = cur.fetchall()
    try:
        found = payment_history.check(cur, batch['category'], members)
    except payment_history.Unavailable:
        raise payment_history.unavailable() from None
    blocked = sum(1 for findings in found.values() if findings)
    if blocked:
        raise ApiError(409, 'PAYMENT_HISTORY_CHANGED', f'A recorded payment now blocks {blocked} passed record(s). '
                       'Nothing was exported. Issued files stay as they are; a new file needs a fresh DQA run '
                       '(a new batch once this one has files).')


def file_count(run, file_type):
    if not run or run['status'] != 'COMPLETED':
        return 0
    return run['failed'] + run['review_count'] if file_type == 'FAILED' else run['passed']


def summary_group_rows(cur, run):
    """Barangay rows in the SUMMARY of a run's PASS records, grouped exactly as summary_csv groups them
    (the TOTAL row is not counted)."""
    keys = ','.join(f'CAST(m.{column} AS BINARY)' for column in GEOGRAPHY)
    cur.execute("SELECT COUNT(*) AS n FROM (SELECT 1 FROM bcmd_dqa_result r JOIN bcmd_batch_member m ON m.id=r.member_id "
                f"WHERE r.run_id=%s AND r.outcome='PASS' GROUP BY {keys}) g", (run['id'],))
    return cur.fetchone()['n']


def export_rows(cur, batch, where='', params=()):
    cur.execute('SELECT e.*,r.public_id AS dqa_run_public_id,r.run_no AS dqa_run_no FROM bcmd_batch_export e '
                'LEFT JOIN bcmd_dqa_run r ON r.id=e.dqa_run_id WHERE e.batch_id=%s' + where + ' ORDER BY e.export_no',
                (batch['id'], *params))
    return cur.fetchall()


def readiness(cur, batch, approvals=None):
    """Per-file readiness from the latest DQA run and the batch policy."""
    approvals = blockers(cur) if approvals is None else approvals
    run = latest_run(cur, batch)
    done = export_rows(cur, batch, " AND e.status='COMPLETED'")
    base = []
    if not store.policy_supported(batch) or batch['member_count'] < 1:
        base.append('VERSION_UNSUPPORTED')
    base += run_reasons(run, batch)
    # A current completed DQA run is sufficient; manual handoff history is optional.
    files = {}
    for file_type in FILE_TYPES:
        existing = next((e for e in done if run and e['dqa_run_id'] == run['id'] and e['file_type'] == file_type), None)
        if existing:
            # An immutable file from this run can be downloaded again while approvals stay current.
            reasons, count = [], existing['row_count']
        else:
            reasons, count = list(base), file_count(run, file_type)
            if count == 0 and run and not run_reasons(run, batch):
                reasons.append('NO_FAILED_RECORDS' if file_type == 'FAILED' else 'NO_PASSED_RECORDS')
        reasons += ['APPROVAL_' + gate for gate in approvals]
        # member_count: records the file covers (row_count keeps that meaning for earlier clients);
        # data_rows: CSV rows after the header (SUMMARY: one per barangay plus the TOTAL row).
        data_rows = count
        if file_type == 'SUMMARY':
            data_rows = summary_group_rows(cur, run) + 1 if count else 0
        files[file_type] = {'ready': not reasons, 'blocking_reasons': reasons, 'row_count': count,
                            'member_count': count, 'data_rows': data_rows,
                            'export_no': existing['export_no'] if existing else None}
    return run, files, done


def locked_batch(cur, public_id, exclusive=False):
    cur.execute('SELECT * FROM bcmd_batch WHERE public_id=%s FOR ' + ('UPDATE' if exclusive else 'SHARE'), (str(public_id),))
    batch = cur.fetchone()
    if not batch:
        raise ApiError(404, 'NOT_FOUND', 'Batch not found')
    if batch['status'] == 'PURGED':
        raise ApiError(410, 'BATCH_PURGED', 'Batch has been purged')
    return batch


def metadata(public_id, row, batch=None):
    file_type = row.get('file_type') or 'DETAIL'
    data = {'batch_id': str(public_id), 'export_no': row['export_no'], 'file_type': file_type,
            'status': row['status'], 'sha256': row['sha256'], 'byte_count': row['byte_count'],
            'row_count': row['row_count'], 'dqa_run_id': row.get('dqa_run_public_id'),
            'dqa_run_no': row.get('dqa_run_no'),
            'download_url': f'/v1/api/tuloy/staff/bcmd/batches/{public_id}/exports/{row["export_no"]}/download'}
    if batch is not None:
        data['filename'] = filename(batch, file_type)
    return data


def _aliased(rows, source_agency):
    """source_agency is the member's own source when stored, else the (single-source) batch's."""
    aliases = {'member_id': 'member_ref', 'member_last_name': 'last_name',
               'member_first_name': 'first_name', 'member_middle_name': 'middle_name',
               'member_ext_name': 'ext_name', 'date_of_birth': 'date_of_birth_raw'}
    return [{**r, **{key: r[value] for key, value in aliases.items()},
             'source_agency': r.get('source_agency') or source_agency} for r in rows]


def legacy_content(cur, batch):
    """DETAIL of every frozen member, for exports made before DQA runs existed."""
    cur.execute('SELECT line_no,' + ','.join(store.PERSONAL_COLUMNS) +
                ' FROM bcmd_batch_member WHERE batch_id=%s ORDER BY line_no LIMIT %s', (batch['id'], batch['member_count'] + 1))
    rows = cur.fetchall()
    if len(rows) != batch['member_count'] or not rows:
        raise ApiError(409, 'NAPA_BATCH_BLOCKED', 'Frozen batch membership is inconsistent')
    try:
        return serialize_detail(_aliased(rows, batch['source_agency']), batch['category'], max_rows=len(rows)), len(rows)
    except NapaValidationError:
        raise ApiError(409, 'NAPA_BATCH_BLOCKED', 'Frozen batch failed export validation') from None


def _run_members(cur, batch, run, outcomes, expected):
    cur.execute('SELECT m.line_no,r.outcome,r.findings_json,m.academic_year,m.semester,m.source_agency,'
                + ','.join('m.' + c for c in store.PERSONAL_COLUMNS) +
                ' FROM bcmd_dqa_result r JOIN bcmd_batch_member m ON m.id=r.member_id AND m.batch_id=%s'
                ' WHERE r.run_id=%s AND r.outcome IN (' + ','.join(['%s'] * len(outcomes)) + ')'
                ' ORDER BY m.line_no LIMIT %s', (batch['id'], run['id'], *outcomes, expected + 1))
    return cur.fetchall()


def _cell(value):
    """Plain text for spreadsheet use: no formula execution, no control characters."""
    if value is None:
        return ''
    text = unicodedata.normalize('NFC', str(value)).replace('\r', ' ').replace('\n', ' ').strip()
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def _writer():
    output = io.StringIO(newline='')
    return output, csv.writer(output, lineterminator='\n', quoting=csv.QUOTE_MINIMAL)


def detail_csv(rows, batch):
    members = [{**row, 'line_no': number} for number, row in enumerate(rows, 1)]
    return serialize_detail(_aliased(members, batch['source_agency']), batch['category'], max_rows=max(1, len(members)))


def _sources(counter):
    return ' / '.join(f'{source} {counter[source]}' for source in SOURCES if counter[source])


def summary_csv(rows, batch):
    """NAPA_V2_SUMMARY: one row per barangay, counts across, then a TOTAL row down."""
    groups = OrderedDict()
    for row in sorted(rows, key=lambda r: tuple((r[c] or '') for c in (
            'region_psgc', 'province_psgc', 'municipality_psgc', 'barangay_psgc', 'region_name',
            'province_name', 'municipality_name', 'barangay_name'))):
        groups.setdefault(tuple(row[c] for c in GEOGRAPHY), []).append(row)
    output, writer = _writer()
    writer.writerow(SUMMARY_HEADERS)

    def counts(members):
        sex = Counter(m['sex'] for m in members)
        source = Counter(m.get('source_agency') or batch['source_agency'] for m in members)
        category = Counter(batch['category'] for _ in members)
        return [len({m['hhid'] for m in members if m['hhid']}),
                len({m['family_id'] for m in members if m['family_id']}),
                len(members), f"M {sex['MALE']} / F {sex['FEMALE']}",
                sum(1 for m in members if m['ip_indicator'] == 'YES'), _sources(source),
                *(category[c] for c in CATEGORIES), len(members)]

    for geography, members in groups.items():
        writer.writerow([*(_cell(v) for v in geography), *counts(members)])
    writer.writerow(['TOTAL', *([''] * (len(GEOGRAPHY) - 1)), *counts(rows)])
    return output.getvalue().encode('utf-8')


def failed_csv(rows, version='failed-v2'):
    output, writer = _writer()
    writer.writerow(FAILED_HEADERS)
    for row in rows:
        findings = [f for f in json.loads(row['findings_json']) if f.get('severity') in ('FAIL', 'REVIEW')]
        writer.writerow([row['line_no'], *(_cell(row[c]) for c in (
            'region_name', 'province_name', 'municipality_name', 'barangay_name', 'hhid', 'member_ref',
            'last_name', 'first_name', 'middle_name', 'ext_name', 'academic_year')),
            row['semester'] or '', 'FAILED' if version != 'failed-v1' or row['outcome'] == 'FAIL' else 'FOR VERIFICATION',
            _cell('; '.join(dict.fromkeys(f['rule_code'] for f in findings))),
            _cell(' | '.join(dict.fromkeys(f['remark'] for f in findings)))])
    return output.getvalue().encode('utf-8')


def build(cur, batch, run, file_type, version=None):
    """Regenerate a file from the frozen members and the stored DQA results of one run, in its own serialization version."""
    version = version or SERIALIZATION[file_type]
    outcomes = ('FAIL', 'REVIEW') if file_type == 'FAILED' else ('PASS',)
    expected = file_count(run, file_type)
    rows = _run_members(cur, batch, run, outcomes, expected)
    if len(rows) != expected:
        raise ApiError(409, 'NAPA_BATCH_BLOCKED', 'Stored DQA results are inconsistent with the run')
    try:
        if file_type == 'DETAIL':
            data = detail_csv(rows, batch)
        elif file_type == 'SUMMARY':
            data = summary_csv(rows, batch)
        else:
            data = failed_csv(rows, version)
    except NapaValidationError:
        raise ApiError(409, 'NAPA_BATCH_BLOCKED', 'Passed records failed export validation') from None
    return data, len(rows)


def audit(cur, batch, actor, request_id, event, **extra):
    return store._insert(cur, 'bcmd_audit_event', dict(batch_id=batch['id'], event_type=event,
        **actor_values(actor, 'actor'), actor_agency=affiliation(actor), request_id=request_id, **extra))


def detail_ready(batch, run, approvals, detail_exported, handoff_valid=True):
    """Light DETAIL readiness for batch lists, without per-batch queries."""
    if approvals:
        return False
    if run and detail_exported:
        return True
    return (store.policy_supported(batch) and batch['member_count'] > 0 and handoff_valid
            and not run_reasons(run, batch) and file_count(run, 'DETAIL') > 0)


def refusal(reasons):
    if 'VERSION_UNSUPPORTED' in reasons:
        return ApiError(409, 'NAPA_VERSION_UNSUPPORTED', 'Batch policy version is unsupported')
    if any(reason.startswith('DQA_') for reason in reasons):
        return ApiError(409, 'NAPA_DQA_REQUIRED', 'Run DQA to completion with the current ruleset first')
    if 'NO_PASSED_RECORDS' in reasons or 'NO_FAILED_RECORDS' in reasons:
        return ApiError(409, 'NAPA_NO_RECORDS', 'This file would contain no records')
    if any(reason.startswith('HANDOFF_') for reason in reasons):
        return ApiError(409, 'NAPA_HANDOFF_REQUIRED', 'Proceed to NAPA from BCMD with the latest DQA run first')
    if any(reason.startswith('APPROVAL_') for reason in reasons):
        return ApiError(409, 'NAPA_APPROVAL_REQUIRED', 'Current export approvals are incomplete')
    return ApiError(409, 'NAPA_NO_RECORDS', 'This file would contain no records')


def create(db, public_id, key, actor, request_id, file_type='DETAIL'):
    """All-or-nothing synchronous generation: crashes roll back STARTED and sequence.

    Lock batch exclusively before any export rows; no shared-to-exclusive upgrades.
    A completed file of the same type from the same DQA run is returned, never rebuilt.
    """
    if file_type not in FILE_TYPES:
        raise ApiError(422, 'VALIDATION_ERROR', 'Unsupported file type')
    creator_column = actor_column(actor)
    cur = db.cursor(dictionary=True)
    try:
        batch = locked_batch(cur, public_id, True)
        approvals = blockers(cur)
        if approvals:
            raise ApiError(409, 'NAPA_APPROVAL_REQUIRED', 'Current export approvals are incomplete')
        request_hash = store.digest({'file_type': file_type})
        previous = export_rows(cur, batch, f' AND e.{creator_column}=%s AND e.idempotency_key=%s', (actor.id, str(key)))
        if previous:
            if previous[0]['request_hash'] != request_hash:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Key was used for a different request')
            return metadata(public_id, previous[0], batch)
        run, files, done = readiness(cur, batch, approvals)
        info = files[file_type]
        if info['export_no']:
            return metadata(public_id, next(e for e in done if e['export_no'] == info['export_no']), batch)
        if info['blocking_reasons']:
            raise refusal(info['blocking_reasons'])
        payment_recheck(cur, batch, run)
        data, row_count = build(cur, batch, run, file_type)
        sha = hashlib.sha256(data).hexdigest()
        number = batch['export_seq'] + 1
        export_id = store._insert(cur, 'bcmd_batch_export', dict(batch_id=batch['id'], dqa_run_id=run['id'],
            export_no=number, status='STARTED', file_type=file_type, generator_version=SERIALIZATION[file_type],
            serialization_version=SERIALIZATION[file_type], header_set_sha256=header_sha256(file_type),
            idempotency_key=str(key), request_hash=request_hash, **actor_values(actor),
            created_by_agency=affiliation(actor), request_id=request_id))
        audit(cur, batch, actor, request_id, 'EXPORT_STARTED', export_id=export_id)
        cur.execute("UPDATE bcmd_batch_export SET status='COMPLETED',sha256=%s,byte_count=%s,row_count=%s,completed_at=UTC_TIMESTAMP(3) WHERE id=%s AND status='STARTED'",
                    (sha, len(data), row_count, export_id))
        audit(cur, batch, actor, request_id, 'EXPORT_COMPLETED', export_id=export_id, sha256=sha)
        # Any export freezes the batch: no new DQA run can change what a file was built from.
        cur.execute("UPDATE bcmd_batch SET export_seq=%s,status='EXPORTED' WHERE id=%s", (number, batch['id']))
        db.commit()
        return metadata(public_id, dict(export_no=number, file_type=file_type, status='COMPLETED', sha256=sha,
                                        byte_count=len(data), row_count=row_count, dqa_run_public_id=run['public_id'],
                                        dqa_run_no=run['run_no']), batch)
    finally:
        db.rollback()
        cur.close()


def download(db, public_id, number, actor, request_id):
    """Recheck approvals, regenerate from frozen data and verify both stored hashes first."""
    cur = db.cursor(dictionary=True)
    try:
        batch = locked_batch(cur, public_id)
        require_approvals(cur)
        rows = export_rows(cur, batch, ' AND e.export_no=%s', (number,))
        if not rows:
            raise ApiError(404, 'NOT_FOUND', 'Export not found')
        row = rows[0]
        file_type = row['file_type']
        if row['dqa_run_id'] is None:
            expected = VERSION
            if file_type != 'DETAIL' or not store.policy_supported(batch) or batch['dqa_outcome'] != 'CLEAN':
                raise ApiError(409, 'NAPA_VERSION_UNSUPPORTED', 'Export version is unavailable')
        else:
            expected = row['serialization_version'] if row['serialization_version'] in DOWNLOADABLE.get(file_type, ()) else None
        if row['status'] != 'COMPLETED' or not expected or row['serialization_version'] != expected or row['generator_version'] != expected:
            raise ApiError(409, 'NAPA_VERSION_UNSUPPORTED', 'Export version is unavailable')
        if row['dqa_run_id'] is None:
            data, row_count = legacy_content(cur, batch)
        else:
            cur.execute('SELECT * FROM bcmd_dqa_run WHERE id=%s AND batch_id=%s', (row['dqa_run_id'], batch['id']))
            run = cur.fetchone()
            if not run or run['status'] != 'COMPLETED':
                raise ApiError(409, 'NAPA_VERSION_UNSUPPORTED', 'Export version is unavailable')
            data, row_count = build(cur, batch, run, file_type, expected)
        sha = hashlib.sha256(data).hexdigest()
        cur.execute("SELECT sha256 FROM bcmd_audit_event WHERE export_id=%s AND event_type='EXPORT_COMPLETED' ORDER BY id", (row['id'],))
        copies = cur.fetchall()
        if (len(copies) != 1 or copies[0]['sha256'] != sha or row['sha256'] != sha
                or row['byte_count'] != len(data) or row['row_count'] != row_count
                or row['header_set_sha256'] != (LEGACY_HEADER_SHA256 if row['dqa_run_id'] is None
                                                else header_sha256(file_type))):
            audit(cur, batch, actor, request_id, 'TAMPER_DETECTED', export_id=row['id'])
            db.commit()
            raise ApiError(409, 'NAPA_INTEGRITY_FAILED', 'Export integrity verification failed')
        admission = audit(cur, batch, actor, request_id, 'DOWNLOAD_ADMITTED', export_id=row['id'], sha256=sha)
        # A durable conservative result survives process death or send interruption.
        # Admission counts as disclosure; server acceptance never proves client receipt.
        audit(cur, batch, actor, request_id, 'DOWNLOAD_UNCERTAIN', export_id=row['id'], parent_event_id=admission, sha256=sha)
        db.commit()
        return data, filename(batch, file_type)
    finally:
        db.rollback()
        cur.close()
