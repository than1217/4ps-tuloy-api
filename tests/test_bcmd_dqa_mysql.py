"""Real isolated MySQL: queue, frozen rules, fencing, retry and history."""
from uuid import uuid4
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import mysql.connector

from test_bcmd_mysql import mysql_candidate, real_stack, BASE, complete_dqa, create, isolate
from test_bcmd_export_mysql import approve, handoff, new_export
import bcmd_dqa as dqa
from bcmd_dqa_rules import evaluate
import pytest


def new_run(client, batch, key=None):
    return client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': key or str(uuid4())})


def test_queue_replay_and_passed_results(real_stack):
    client, db, _ = real_stack
    batch = create(client).json()['public_id']
    isolate(db, batch)
    listed = client.get(f'{BASE}/batches/{batch}').json()
    assert listed['dqa_run_status'] == 'NOT_RUN' and listed['latest_dqa'] is None
    key = str(uuid4())
    assert client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': key}, json={'ruleset': 'APPROVED'}).status_code == 422
    first = new_run(client, batch, key)
    assert first.status_code == 202, first.text
    run = first.json()
    assert run['status'] == 'QUEUED' and run['processed'] == 0
    assert new_run(client, batch, key).json() == run
    assert new_run(client, batch).status_code == 409
    assert dqa.work_once(db)
    progress = client.get(f'{BASE}/batches/{batch}/dqa-runs/{run["public_id"]}').json()
    assert progress['status'] == 'COMPLETED'
    assert progress['processed'] == progress['total'] == progress['passed'] + progress['failed'] + progress['review_count'] == 2
    assert progress['passed'] == 2 and progress['failed'] == progress['review_count'] == 0
    assert progress['ruleset_version'] == 'essi-mvp-v4' and progress['ruleset_current'] is True
    assert progress['payment_eligible'] is False and progress['percent'] == 100
    assert {c['rule_code'] for c in progress['checks']} >= {'AGE_OUTSIDE_RANGE', 'DUPLICATE_CLAIM', 'SELECTED_IN_OTHER_BATCH'}
    assert {n['key'] for n in progress['not_evaluated']} >= {'PAYMENT_ACCOUNT', 'EXTERNAL_DISBURSEMENT_LEDGER'}
    results = client.get(f'{BASE}/batches/{batch}/dqa-runs/{run["public_id"]}/results?limit=1').json()
    assert results['has_more'] is True and results['items'][0]['outcome'] == 'PASS'
    assert results['items'][0]['findings'] == [] and results['items'][0]['member_ref'] == '1'
    assert client.get(f'{BASE}/batches/{batch}/dqa-runs/{run["public_id"]}/results?outcome=FAIL').json()['items'] == []
    assert handoff(client, batch).status_code == 201
    reasons = client.get(f'{BASE}/batches/{batch}/export-readiness').json()['blocking_reasons']
    assert reasons == []  # no approval gate: a completed current run with passed records is ready
    latest = client.get(f'{BASE}/batches?limit=200').json()['items']
    assert next(b for b in latest if b['public_id'] == batch)['latest_dqa']['passed'] == 2
    # New run preserves old findings, and an unknown run cannot cross batch boundaries.
    second = new_run(client, batch).json()
    assert second['run_no'] == 2
    assert client.get(f'{BASE}/batches/{batch}/dqa-runs/{run["public_id"]}').json()['status'] == 'COMPLETED'
    assert client.get(f'{BASE}/batches/{uuid4()}/dqa-runs/{run["public_id"]}').status_code == 404
    assert dqa.work_once(db)


def test_expired_worker_is_fenced_and_checkpoint_recovers(real_stack, monkeypatch):
    client, db, _ = real_stack
    batch = create(client).json()['public_id']
    run_id = new_run(client, batch).json()['public_id']
    monkeypatch.setattr(dqa, 'CHUNK', 1)
    run = dqa.claim(db)
    first = dqa.process_chunk(db, run)
    assert first['processed'] == 1 and first['status'] == 'RUNNING'
    assert dqa.process_chunk(db, run) is None  # Replaying the old checkpoint cannot count twice.
    cur = db.cursor()
    cur.execute('UPDATE bcmd_dqa_run SET lease_until=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND) WHERE id=%s', (run['id'],))
    db.commit()
    assert dqa.claim(db) is None  # Watchdog puts it into a delayed RETRYING state.
    assert dqa.process_chunk(db, first) is None
    cur.execute('UPDATE bcmd_dqa_run SET next_attempt_at=UTC_TIMESTAMP(6) WHERE id=%s', (run['id'],)); db.commit()
    recovered = dqa.claim(db)
    assert recovered['lease_token'] != run['lease_token'] and recovered['checkpoint_member_id'] == first['checkpoint_member_id']
    result = dqa.process_chunk(db, recovered)
    assert result['status'] == 'COMPLETED' and result['processed'] == 2
    assert client.get(f'{BASE}/batches/{batch}/dqa-runs/{run_id}/results').json()['has_more'] is False
    cur.close()


def test_terminal_failures_preserve_prior_results_and_retry_as_new_run(real_stack, monkeypatch):
    client, db, _ = real_stack
    batch = create(client).json()['public_id']
    run_id = new_run(client, batch).json()['public_id']
    def broken(*args, **kwargs):
        raise ValueError('private source details must not be stored')
    monkeypatch.setattr(dqa, 'evaluate', broken)
    for _ in range(3):
        assert dqa.work_once(db)
        cur = db.cursor();cur.execute('UPDATE bcmd_dqa_run SET next_attempt_at=UTC_TIMESTAMP(6) WHERE public_id=%s', (run_id,));db.commit();cur.close()
    result = client.get(f'{BASE}/batches/{batch}/dqa-runs/{run_id}').json()
    assert result['status'] == 'FAILED' and result['attempts'] == 3
    assert result['last_error_code'] == 'DQA_WORKER_FAILED'
    assert 'private' not in str(result)
    assert new_run(client, batch).status_code == 202
    # Drain so other tests cannot claim this active run.
    assert dqa.work_once(db)
    cur=db.cursor();cur.execute("UPDATE bcmd_dqa_run SET status='FAILED' WHERE batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s) AND status='RETRYING'",(batch,));db.commit();cur.close()


def test_multiple_failures_one_member_and_nullable_family(real_stack):
    client, db, _ = real_stack
    batch = create(client).json()['public_id']
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT * FROM bcmd_batch_member WHERE batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s) ORDER BY line_no LIMIT 1', (batch,))
    member = cur.fetchone();db.rollback();cur.close()
    outcome, reasons = evaluate(member, {'category': 'TK', 'source_agency': 'CHED'}, date(2026, 11, 3))
    assert outcome == 'PASS' and reasons == []  # Nullable FamilyID is not a finding.
    member.update(member_ref='', date_of_birth_raw='bad-date')
    outcome, reasons = evaluate(member, {'category': 'TK', 'source_agency': 'CHED'}, date(2026, 11, 3))
    assert outcome == 'FAIL' and len([r for r in reasons if r['severity']=='FAIL']) >= 2


def test_only_one_worker_claims_and_live_source_changes_do_not_change_results(real_stack, mysql_candidate):
    client, db, _ = real_stack
    batch = create(client).json()['public_id']
    run_id = new_run(client, batch).json()['public_id']
    def claim_worker():
        connection = mysql.connector.connect(**mysql_candidate)
        try:
            return dqa.claim(connection)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        claimed = list(workers.map(lambda _: claim_worker(), range(2)))
    owners = [run for run in claimed if run]
    assert len(owners) == 1
    cur = db.cursor()
    cur.execute("UPDATE tbl_person SET date_of_birth='1900-01-01' WHERE person_id IN (1,2)");db.commit()
    try:
        result = dqa.process_chunk(db, owners[0])
        assert result['status'] == 'COMPLETED'
        rows = client.get(f'{BASE}/batches/{batch}/dqa-runs/{run_id}/results').json()['items']
        assert not any(f['rule_code'] == 'AGE_OUTSIDE_RANGE' for row in rows for f in row['findings'])
    finally:
        cur.execute("UPDATE tbl_person SET date_of_birth='2001-02-03' WHERE person_id IN (1,2)");db.commit();cur.close()


def test_cross_batch_selection_is_review_and_exported_claim_fails(real_stack):
    client, db, _ = real_stack
    first = create(client, ids=[1, 2]).json()['public_id']
    isolate(db, first)
    second = create(client, ids=[2, 3]).json()['public_id']
    run = complete_dqa(client, db, second, isolated=False)
    assert (run['passed'], run['review_count']) == (1, 1)
    items = client.get(f"{BASE}/batches/{second}/dqa-runs/{run['public_id']}/results?outcome=REVIEW").json()['items']
    assert [i['member_ref'] for i in items] == ['2']
    assert [f['rule_code'] for f in items[0]['findings']] == ['SELECTED_IN_OTHER_BATCH']
    first_run = complete_dqa(client, db, first, isolated=False)
    assert (first_run['passed'], first_run['review_count']) == (1, 1)
    approve(db)
    assert new_export(client, first).status_code == 201  # member 1 only goes to the NAPA
    third = create(client, ids=[1, 2]).json()['public_id']
    run = complete_dqa(client, db, third, isolated=False)
    results = {i['member_ref']: i for i in client.get(f"{BASE}/batches/{third}/dqa-runs/{run['public_id']}/results").json()['items']}
    assert results['1']['outcome'] == 'FAIL' and [f['rule_code'] for f in results['1']['findings']] == ['DUPLICATE_CLAIM']
    # Member 2 was not in the first NAPA (for verification there) but is still selected in `second`.
    assert results['2']['outcome'] == 'REVIEW'


def test_outdated_ruleset_run_blocks_exports_and_preview(real_stack):
    client, db, _ = real_stack
    batch = create(client).json()['public_id']
    run = complete_dqa(client, db, batch)
    cur = db.cursor()
    cur.execute("UPDATE bcmd_dqa_run SET ruleset_version='essi-draft-frozen-review-v1' WHERE public_id=%s", (run['public_id'],))
    db.commit(); cur.close()
    readiness = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert 'DQA_RULESET_OUTDATED' in readiness['files']['DETAIL']['blocking_reasons']
    preview = client.get(f'{BASE}/batches/{batch}/payroll-preview')
    assert preview.status_code == 422 and preview.json()['error_code'] == 'NAPA_DQA_REQUIRED'
    assert client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())}).status_code == 202
    assert dqa.work_once(db)
    assert client.get(f'{BASE}/batches/{batch}/payroll-preview').status_code == 200
    # A newer queued (or failed) run is what the NAPA files would use, so the preview never falls back
    # to the older completed run.
    assert client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())}).status_code == 202
    preview = client.get(f'{BASE}/batches/{batch}/payroll-preview')
    assert preview.status_code == 422 and preview.json()['error_code'] == 'NAPA_DQA_REQUIRED'
    assert 'DQA_IN_PROGRESS' in client.get(f'{BASE}/batches/{batch}/export-readiness').json()['blocking_reasons']
    assert dqa.work_once(db)
    assert client.get(f'{BASE}/batches/{batch}/payroll-preview').json()['dqa_run_no'] == 3
