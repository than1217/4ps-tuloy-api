"""Real MySQL export acceptance against disposable synthetic schemas only."""
import csv
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import mysql.connector
import pytest

import bcmd_export as exports
import bcmd_store as store
from errors import ApiError
from security import Principal, Grant, get_beneficiary_principal
from scopes import LEGACY_CHED_READ
from test_bcmd_mysql import mysql_candidate, real_stack, create, complete_dqa, BASE


def approve(db):
    cur = db.cursor()
    hashes = exports.artifacts()
    for gate, roles in exports.ROLES.items():
        cur.execute('INSERT INTO bcmd_approval (gate_code,decision,approver_role,approver_label,`reference`,approved_on,artifact_sha256) '
                    "VALUES (%s,'APPROVED',%s,'SYNTHETIC OWNER','SYNTHETIC ONLY','2026-10-30',%s)",
                    (gate, sorted(roles)[0], hashes.get(gate)))
    db.commit()
    cur.close()


def handoff(client, batch):
    """BCMD's Proceed to NAPA for the batch's latest DQA run. A refused handoff is left for the export to report."""
    runs = client.get(f'{BASE}/batches/{batch}/dqa-runs')
    items = runs.json().get('items') if runs.status_code == 200 else None
    if items:
        return client.post(f'{BASE}/batches/{batch}/napa-handoff', json={'dqa_run_id': items[0]['public_id']},
                           headers={'Idempotency-Key': str(uuid4())})


def new_export(client, batch, key=None, file_type='DETAIL', hand_off=True):
    if hand_off:
        handoff(client, batch)
    return client.post(f'{BASE}/batches/{batch}/exports', json={'file_type': file_type},
                       headers={'Idempotency-Key': key or str(uuid4())})


def legacy_export(db, public_id, actor_id=11):
    """An export as written before DQA runs existed: every member, no run, legacy header hash."""
    cur = db.cursor(dictionary=True)
    try:
        batch = exports.locked_batch(cur, public_id, True)
        data, rows = exports.legacy_content(cur, batch)
        sha = hashlib.sha256(data).hexdigest()
        number = batch['export_seq'] + 1
        export_id = store._insert(cur, 'bcmd_batch_export', dict(batch_id=batch['id'], export_no=number,
            status='COMPLETED', file_type='DETAIL', generator_version='detail-v1', serialization_version='detail-v1',
            header_set_sha256=exports.LEGACY_HEADER_SHA256, sha256=sha, byte_count=len(data), row_count=rows,
            idempotency_key=str(uuid4()), request_hash=store.digest({'file_type': 'DETAIL'}),
            created_by_api_user_id=actor_id, created_by_agency='DSWD', request_id='synthetic-legacy',
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        for event, extra in [('EXPORT_STARTED', {}), ('EXPORT_COMPLETED', {'sha256': sha})]:
            store._insert(cur, 'bcmd_audit_event', dict(batch_id=batch['id'], event_type=event, export_id=export_id,
                actor_api_user_id=actor_id, actor_agency='DSWD', request_id='synthetic-legacy', **extra))
        cur.execute("UPDATE bcmd_batch SET export_seq=%s,status='EXPORTED' WHERE id=%s", (number, batch['id']))
        db.commit()
        return number, data
    finally:
        db.rollback()
        cur.close()


def csv_rows(response):
    return list(csv.reader(io.StringIO(response.content.decode('utf8'))))


def test_golden_snapshot_csv_idempotency_and_audit(real_stack):
    client, db, app = real_stack
    batch = create(client, ids=[1, 2]).json()['public_id']
    assert client.get(f'{BASE}/batches/{batch}/export-readiness').json()['ready'] is False
    assert new_export(client, batch).json()['error_code'] == 'NAPA_DQA_REQUIRED'
    approve(db)
    readiness = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert readiness['ready'] is False and readiness['blocking_reasons'] == ['DQA_NOT_RUN']
    assert new_export(client, batch).json()['error_code'] == 'NAPA_DQA_REQUIRED'
    complete_dqa(client, db, batch)
    assert handoff(client, batch).status_code == 201
    assert client.get(f'{BASE}/batches/{batch}/export-readiness').json()['ready'] is True
    key = str(uuid4())
    response = new_export(client, batch, key)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data['row_count'] == 2
    assert new_export(client, batch, key).json() == data
    # Changing live source must never change an already saved batch export.
    cur = db.cursor()
    cur.execute("UPDATE tbl_person SET first_name='CHANGED SOURCE' WHERE person_id=1")
    db.commit()
    download = client.get(data['download_url'])
    assert download.status_code == 200, download.text
    rows = list(csv.reader(io.StringIO(download.content.decode('utf8'))))
    assert len(rows) == 3 and all(len(row) == 20 for row in rows)
    assert rows[1][1] == '0100000000'
    assert rows[1][9] == '' and rows[1][16] == ''
    assert rows[1][17] == '03-02-2001' and rows[1][19] == 'TK'
    assert 'CHANGED SOURCE' not in download.text
    assert download.headers['cache-control'] == 'no-store'
    assert download.headers['content-disposition'] == f'attachment; filename="NAPA-DETAILED-Synthetic_batch_{batch_name(client, batch)}.csv"'
    assert client.get(f'{BASE}/batches/{batch}/export').content == download.content
    cur.execute("SELECT event_type FROM bcmd_audit_event WHERE export_id IS NOT NULL ORDER BY id")
    events = [r[0] for r in cur.fetchall()]
    assert events[:2] == ['EXPORT_STARTED', 'EXPORT_COMPLETED']
    assert events[2:] == ['DOWNLOAD_ADMITTED', 'DOWNLOAD_UNCERTAIN'] * 2
    cur.close()


def batch_name(client, batch):
    return client.get(f'{BASE}/batches/{batch}').json()['name'].removeprefix('Synthetic batch ')


def test_three_napa_files_from_one_dqa_run(real_stack):
    client, db, app = real_stack
    batch = create(client, ids=[1, 2, 3]).json()['public_id']
    cur = db.cursor()
    cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.hhid=NULL '
                'WHERE b.public_id=%s AND m.line_no=3', (batch,))
    db.commit(); cur.close()
    approve(db)
    run = complete_dqa(client, db, batch)
    assert (run['passed'], run['failed'], run['review_count']) == (2, 1, 0)
    files = client.get(f'{BASE}/batches/{batch}/export-readiness').json()['files']
    assert {t: f['blocking_reasons'] for t, f in files.items()} == {t: [] for t in files}
    assert handoff(client, batch).status_code == 201
    readiness = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert readiness['dqa_run']['run_no'] == 1 and readiness['exports'] == []
    assert {t: (f['ready'], f['row_count']) for t, f in readiness['files'].items()} == {
        'DETAIL': (True, 2), 'SUMMARY': (True, 2), 'FAILED': (True, 1)}
    # Members a file covers versus its CSV rows: the SUMMARY has one row per barangay plus TOTAL.
    assert {t: (f['member_count'], f['data_rows']) for t, f in readiness['files'].items()} == {
        'DETAIL': (2, 2), 'SUMMARY': (2, 2), 'FAILED': (1, 1)}
    detail = new_export(client, batch)
    assert detail.status_code == 201, detail.text
    assert detail.json()['row_count'] == 2 and detail.json()['dqa_run_no'] == 1
    assert detail.json()['filename'].startswith('NAPA-DETAILED-Synthetic_batch_')
    rows = csv_rows(client.get(detail.json()['download_url']))
    assert len(rows) == 3 and [r[10] for r in rows[1:]] == ['1', '2']  # passed members only
    summary = new_export(client, batch, file_type='SUMMARY')
    assert summary.status_code == 201 and summary.json()['export_no'] == 2
    rows = csv_rows(client.get(summary.json()['download_url']))
    assert rows[0] == list(exports.SUMMARY_HEADERS) and len(rows[0]) == 18 and 'CATEGORY' not in ''.join(rows[0])
    assert rows[1] == ['Synthetic Region', '0100000000', 'Synthetic Province', '0101000000', 'Synthetic Municipality',
                       '0101010000', 'Synthetic Barangay', '0101010001', '2', '1', '2', 'M 2 / F 0', '0', 'CHED 2',
                       '2', '0', '0', '2']
    assert rows[2] == ['TOTAL', '', '', '', '', '', '', '', '2', '1', '2', 'M 2 / F 0', '0', 'CHED 2', '2', '0', '0', '2']
    failed = new_export(client, batch, file_type='FAILED')
    assert failed.status_code == 201 and failed.json()['row_count'] == 1
    response = client.get(failed.json()['download_url'])
    assert response.headers['content-disposition'].startswith('attachment; filename="NAPA-FAILED-Synthetic_batch_')
    rows = csv_rows(response)
    assert rows[0] == list(exports.FAILED_HEADERS)
    assert rows[1][0] == '3' and rows[1][5] == '' and rows[1][13] == 'FAILED'
    assert 'MISSING_HHID' in rows[1][14] and 'household' in rows[1][15]
    # Same run and type returns the same immutable file, whoever asks and with any key.
    assert new_export(client, batch).json()['export_no'] == detail.json()['export_no']
    assert new_export(client, batch, str(uuid4()), 'SUMMARY').json()['sha256'] == summary.json()['sha256']
    readiness = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert [(e['export_no'], e['file_type']) for e in readiness['exports']] == [(1, 'DETAIL'), (2, 'SUMMARY'), (3, 'FAILED')]
    # Exported batches are frozen: no new DQA run can change what the files were built from.
    rerun = client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())})
    assert rerun.status_code == 409 and rerun.json()['error_code'] == 'DQA_BATCH_EXPORTED'
    detail_batch = client.get(f'{BASE}/batches/{batch}').json()
    assert detail_batch['export_ready'] is True and detail_batch['export_seq'] == 3
    assert detail_batch['latest_dqa']['passed'] == 2 and detail_batch['dqa_run_status'] == 'COMPLETED'


def test_reused_key_for_another_file_type_conflicts(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    key = str(uuid4())
    assert new_export(client, batch, key).status_code == 201
    conflict = new_export(client, batch, key, 'SUMMARY')
    assert conflict.status_code == 409 and conflict.json()['error_code'] == 'IDEMPOTENCY_CONFLICT'
    assert new_export(client, batch, file_type='OTHER').status_code == 422


def test_legacy_export_without_dqa_run_still_downloads(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    number, data = legacy_export(db, batch)
    response = client.get(f'{BASE}/batches/{batch}/exports/{number}/download')
    assert response.status_code == 200, response.text
    assert response.content == data and len(csv_rows(response)) == 3
    readiness = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert readiness['files']['DETAIL']['blocking_reasons'] == ['DQA_NOT_RUN']
    assert readiness['exports'][0]['dqa_run_id'] is None


@pytest.mark.parametrize('gate', list(exports.ROLES))
def test_historical_revocation_does_not_block_create_and_download(real_stack, gate):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    data = new_export(client, batch).json()
    cur = db.cursor()
    cur.execute('INSERT INTO bcmd_approval (gate_code,decision,approver_role,approver_label,`reference`,approved_on,artifact_sha256) '
                "VALUES (%s,'REVOKED',%s,'SYNTHETIC','SYNTHETIC REVOKE','2026-10-30',%s)",
                (gate, sorted(exports.ROLES[gate])[0], exports.artifacts().get(gate)))
    db.commit()
    assert new_export(client, batch).status_code == 201
    assert client.get(data['download_url']).status_code == 200
    assert client.get(f'{BASE}/batches/{batch}').json()['export_ready']
    cur.close()


@pytest.mark.parametrize('target', ['snapshot', 'export', 'audit'])
def test_tampering_never_returns_csv(real_stack, target):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    data = new_export(client, batch).json()
    cur = db.cursor()
    table, column, predicate = {
        'snapshot': ('bcmd_batch_member', 'first_name', 'batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s)'),
        'export': ('bcmd_batch_export', 'sha256', 'batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s)'),
        'audit': ('bcmd_audit_event', 'sha256', "batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s) AND event_type='EXPORT_COMPLETED'")}[target]
    cur.execute(f'UPDATE {table} SET {column}=%s WHERE {predicate}', ('0'*64 if target != 'snapshot' else 'Tampered', batch))
    db.commit()
    response = client.get(data['download_url'])
    assert response.status_code == 409
    assert response.json()['error_code'] == 'NAPA_INTEGRITY_FAILED'
    cur.close()


def test_audit_failure_rolls_back_entire_export(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    cur = db.cursor()
    cur.execute("CREATE TRIGGER synthetic_export_fail BEFORE INSERT ON bcmd_audit_event FOR EACH ROW BEGIN IF NEW.event_type='EXPORT_COMPLETED' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic fault'; END IF; END")
    try:
        assert new_export(client, batch).status_code >= 500
        cur.execute('SELECT COUNT(*) FROM bcmd_batch_export WHERE batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s)', (batch,))
        assert cur.fetchone()[0] == 0
        cur.execute('SELECT export_seq,status FROM bcmd_batch WHERE public_id=%s', (batch,))
        assert cur.fetchone() == (0, 'DQA_COMPLETED')
    finally:
        cur.execute('DROP TRIGGER synthetic_export_fail')
        db.commit()
        cur.close()


def test_concurrent_same_retry_key_creates_one_version(real_stack, mysql_candidate):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    assert handoff(client, batch).status_code == 201
    actor = Principal(11, 'synthetic', frozenset({Grant(LEGACY_CHED_READ, 'TEST', 'TEST')}))
    key = uuid4()
    def run():
        connection = mysql.connector.connect(**mysql_candidate)
        try:
            return exports.create(connection, batch, key, actor, 'synthetic-request')
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert results[0]['export_no'] == 1


def test_export_auth_and_purge(real_stack, mysql_candidate):
    from scripts.purge_napa_batch import purge_batch
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    data = new_export(client, batch).json()
    for agency in ('CHED', 'TESDA'):
        app.dependency_overrides[get_beneficiary_principal] = lambda a=agency: Principal(11, 'other', frozenset({Grant(LEGACY_CHED_READ, a, a)}))
        assert new_export(client, batch).status_code == 403
        assert client.get(data['download_url']).status_code == 403
    app.dependency_overrides[get_beneficiary_principal] = lambda: Principal(11, 'test', frozenset({Grant(LEGACY_CHED_READ, 'TEST', 'TEST')}))
    assert client.get(data['download_url']).status_code == 200
    cur = db.cursor()
    cur.execute('SELECT @@hostname')
    hostname = cur.fetchone()[0]
    db.rollback()
    purge_batch(db, batch, expected_database=mysql_candidate['database'], expected_hostname=hostname, apply=True)
    assert client.get(data['download_url']).status_code == 410
    assert new_export(client, batch).status_code == 410
    cur.close()


def test_historical_artifacts_do_not_gate_exports_but_no_passed_records_do(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    cur = db.cursor()
    cur.execute("UPDATE bcmd_approval SET artifact_sha256=%s WHERE gate_code='COLUMN_MAPPING'", ('0'*64,))
    db.commit()
    assert new_export(client, batch).status_code == 201
    approve(db)
    nobody = create(client, ids=[1]).json()['public_id']
    cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.hhid=NULL WHERE b.public_id=%s', (nobody,))
    db.commit()
    complete_dqa(client, db, nobody)
    files = client.get(f'{BASE}/batches/{nobody}/export-readiness').json()['files']
    assert files['DETAIL']['blocking_reasons'] == files['SUMMARY']['blocking_reasons'] == [
        'NO_PASSED_RECORDS']
    # No PASS record: Detailed/Summary are unavailable, but the Failed file is downloadable.
    refused = handoff(client, nobody)
    assert refused.status_code == 409 and refused.json()['error_code'] == 'NAPA_NO_RECORDS'
    assert new_export(client, nobody).json()['error_code'] == 'NAPA_NO_RECORDS'
    assert new_export(client, nobody, file_type='SUMMARY').json()['error_code'] == 'NAPA_NO_RECORDS'
    assert new_export(client, nobody, file_type='FAILED').status_code == 201
    assert client.get(f'{BASE}/batches/{nobody}/payroll-preview').json()['payable_count'] == 0
    cur.close()


def test_download_audit_failure_emits_no_file(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    data = new_export(client, batch).json()
    cur = db.cursor()
    cur.execute("CREATE TRIGGER synthetic_admission_fail BEFORE INSERT ON bcmd_audit_event FOR EACH ROW BEGIN IF NEW.event_type='DOWNLOAD_UNCERTAIN' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic fault'; END IF; END")
    try:
        response = client.get(data['download_url'])
        assert response.status_code >= 500
        assert response.headers['content-type'].startswith('application/json')
        cur.execute("SELECT COUNT(*) FROM bcmd_audit_event WHERE batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s) AND event_type='DOWNLOAD_ADMITTED'", (batch,))
        assert cur.fetchone()[0] == 0
    finally:
        cur.execute('DROP TRIGGER synthetic_admission_fail')
        db.commit()
        cur.close()


def test_sso_nonexistent_batch_remains_not_found(real_stack):
    client, db, app = real_stack
    staff = Principal(11, 'synthetic-sso', frozenset({Grant(LEGACY_CHED_READ, 'DSWD', 'DSWD')}), kind='staff')
    app.dependency_overrides[get_beneficiary_principal] = lambda: staff
    batch = str(uuid4())
    for method, path, payload in [
        ('POST', f'{BASE}/batches/{batch}/exports', {'file_type': 'DETAIL'}),
        ('GET', f'{BASE}/batches/{batch}/exports/1/download', None),
        ('GET', f'{BASE}/batches/{batch}/export', None),
    ]:
        result = client.request(method, path, json=payload, headers={'Idempotency-Key': str(uuid4())})
        assert result.status_code == 404
        assert result.json()['error_code'] == 'NOT_FOUND'


def test_each_file_records_its_own_header_hash(real_stack, tmp_path, monkeypatch):
    client, db, app = real_stack
    batch = create(client, ids=[1, 2, 3]).json()['public_id']
    cur = db.cursor()
    cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.hhid=NULL '
                'WHERE b.public_id=%s AND m.line_no=3', (batch,))
    db.commit()
    approve(db)
    complete_dqa(client, db, batch)
    made = {t: new_export(client, batch, file_type=t).json() for t in exports.FILE_TYPES}
    cur.execute('SELECT e.file_type,e.header_set_sha256 FROM bcmd_batch_export e JOIN bcmd_batch b ON b.id=e.batch_id '
                'WHERE b.public_id=%s', (batch,))
    assert dict(cur.fetchall()) == {t: exports.header_sha256(t) for t in exports.FILE_TYPES}
    db.rollback(); cur.close()
    assert exports.header_sha256('DETAIL') == exports.LEGACY_HEADER_SHA256
    # A later edit of the FAILED template breaks only FAILED downloads, never DETAIL or SUMMARY.
    edited = tmp_path / 'napa-failed-headers.csv'
    edited.write_text(exports.FAILED_HEADER_FILE.read_text(encoding='utf8').replace('REMARKS', 'NOTES'), encoding='utf8')
    monkeypatch.setitem(exports.HEADER_FILES, 'FAILED', edited)
    assert client.get(made['DETAIL']['download_url']).status_code == 200
    assert client.get(made['SUMMARY']['download_url']).status_code == 200
    failed = client.get(made['FAILED']['download_url'])
    assert failed.status_code == 409 and failed.json()['error_code'] == 'NAPA_INTEGRITY_FAILED'


def test_rollback_0021_guard_refuses_while_run_based_exports_exist(real_stack):
    """Only the guard statement runs: it must fail and leave the shared schema unchanged."""
    from pathlib import Path
    from scripts.migrate import statements
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    assert new_export(client, batch).status_code == 201  # a run-based DETAIL (dqa_run_id set)
    guard = statements(Path('migrations/rollback/0021_bcmd_export_file_types_down.sql').read_text(encoding='utf8'))[0]
    assert 'ck_bcmd_export_rollback_guard' in guard
    cur = db.cursor()
    with pytest.raises(mysql.connector.Error) as refused:
        cur.execute(guard)
    assert refused.value.errno == 3819  # ER_CHECK_CONSTRAINT_VIOLATED
    cur.execute("SELECT COUNT(*) FROM information_schema.CHECK_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() "
                "AND CONSTRAINT_NAME='ck_bcmd_export_rollback_guard'")
    assert cur.fetchone()[0] == 0
    db.rollback(); cur.close()
