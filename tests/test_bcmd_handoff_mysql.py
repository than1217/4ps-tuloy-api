"""Durable BCMD-to-NAPA handoff on the isolated synthetic MySQL only. Every record is synthetic."""
import json
import logging
import threading
from uuid import uuid4

import mysql.connector
import pytest

import bcmd_payment_history as history
from test_bcmd_mysql import BASE, complete_dqa, create, isolate, mysql_candidate, real_stack
from test_bcmd_export_mysql import approve, csv_rows, new_export
from test_bcmd_payment_history_mysql import add_people, internal_id, record_payment, revive

NAMES = ('Synthetic', 'Example', 'Student')


def latest_run(client, batch):
    return client.get(f'{BASE}/batches/{batch}/dqa-runs').json()['items'][0]['public_id']


def hand_off(client, batch, run=None, key=None):
    return client.post(f'{BASE}/batches/{batch}/napa-handoff', json={'dqa_run_id': run or latest_run(client, batch)},
                       headers={'Idempotency-Key': key or str(uuid4())})


def state(client, batch):
    return client.get(f'{BASE}/batches/{batch}/napa-handoff').json()


def code(response):
    return response.status_code, response.json().get('error_code')


def handoff_rows(db, batch):
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT h.* FROM bcmd_napa_handoff h JOIN bcmd_batch b ON b.id=h.batch_id WHERE b.public_id=%s '
                'ORDER BY h.id', (batch,))
    rows = cur.fetchall()
    db.rollback(); cur.close()
    return rows


def batch_of(client, ids):
    made = create(client, ids=ids)
    assert made.status_code == 201, made.text
    return made.json()['public_id']


def test_handoff_is_durable_idempotent_and_versioned(real_stack):
    client, db, _ = real_stack
    add_people(db, [81001, 81002])
    batch = batch_of(client, [81001, 81002])
    assert state(client, batch)['status'] == 'NOT_RECORDED'
    assert code(client.post(f'{BASE}/batches/{batch}/napa-handoff', json={'dqa_run_id': str(uuid4())},
                            headers={'Idempotency-Key': str(uuid4())})) == (409, 'HANDOFF_RUN_NOT_LATEST')
    run = complete_dqa(client, db, batch)
    stale_id = str(uuid4())
    assert code(hand_off(client, batch, run=stale_id)) == (409, 'HANDOFF_RUN_NOT_LATEST')
    key = str(uuid4())
    first = hand_off(client, batch, key=key)
    assert first.status_code == 201, first.text
    body = first.json()
    assert (body['status'], body['reasons'], body['current']['dqa_run_id'], body['current']['passed_count']) == (
        'VALID', [], run['public_id'], 2)
    # Same key and body: the same handoff. Another key for the same run: still that handoff, never a second row.
    assert hand_off(client, batch, key=key).json() == body
    assert hand_off(client, batch).json()['current']['public_id'] == body['current']['public_id']
    assert code(hand_off(client, batch, run=stale_id, key=key)) == (409, 'IDEMPOTENCY_CONFLICT')
    assert len(handoff_rows(db, batch)) == 1
    # Persisted: a fresh read (as after a reload) returns it, with history.
    reread = state(client, batch)
    assert reread['status'] == 'VALID' and [h['public_id'] for h in reread['history']] == [body['current']['public_id']]
    listed = client.get(f'{BASE}/batches/{batch}').json()
    assert listed['napa_handoff']['status'] == 'VALID'
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM bcmd_audit_event a JOIN bcmd_batch b ON b.id=a.batch_id "
                "WHERE b.public_id=%s AND a.event_type='NAPA_HANDOFF_RECORDED'", (batch,))
    assert cur.fetchone()[0] == 1
    db.rollback(); cur.close()


def test_completed_rerun_is_automatically_available_and_keeps_handoff_history(real_stack):
    client, db, _ = real_stack
    add_people(db, [82001])
    batch = batch_of(client, [82001])
    complete_dqa(client, db, batch)
    first = hand_off(client, batch).json()['current']
    second_run = complete_dqa(client, db, batch)
    stale = state(client, batch)
    assert (stale['status'], stale['reasons']) == ('VALID', [])
    assert stale['current']['dqa_run_id'] == second_run['public_id']
    approve(db)
    ready = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert ready['files']['DETAIL']['ready'] is True
    refused = client.post(f'{BASE}/batches/{batch}/exports', json={'file_type': 'DETAIL'},
                          headers={'Idempotency-Key': str(uuid4())})
    assert refused.status_code == 201
    again = hand_off(client, batch).json()
    assert again['status'] == 'VALID' and again['current']['dqa_run_id'] == second_run['public_id']
    assert [h['public_id'] for h in state(client, batch)['history']] == [again['current']['public_id'], first['public_id']]


def test_review_decision_after_handoff_makes_it_stale(real_stack):
    client, db, _ = real_stack
    add_people(db, [83001])
    batch = batch_of(client, [83001])
    complete_dqa(client, db, batch)
    assert hand_off(client, batch).status_code == 201
    cur = db.cursor()
    cur.execute('UPDATE bcmd_batch SET claim_epoch=claim_epoch+1 WHERE public_id=%s', (batch,))  # as a decision does
    db.commit(); cur.close()
    assert state(client, batch)['reasons'] == ['DQA_REFRESH_REQUIRED']
    assert code(hand_off(client, batch)) == (409, 'NAPA_DQA_REQUIRED')


def test_handoff_needs_a_completed_current_run_with_a_pass(real_stack):
    client, db, _ = real_stack
    add_people(db, [84001])
    no_pass = batch_of(client, [84001])
    run = complete_dqa(client, db, no_pass)
    cur = db.cursor()
    # Synthetic database only: the run's single record becomes REVIEW, as if a rule had flagged it.
    cur.execute("UPDATE bcmd_dqa_result r JOIN bcmd_dqa_run d ON d.id=r.run_id SET r.outcome='REVIEW' "
                "WHERE d.public_id=%s", (run['public_id'],))
    db.commit()
    assert code(hand_off(client, no_pass)) == (409, 'NAPA_NO_RECORDS')
    cur.execute("UPDATE bcmd_dqa_run SET ruleset_version='essi-mvp-v3' WHERE public_id=%s", (run['public_id'],))
    db.commit(); cur.close()
    assert code(hand_off(client, no_pass)) == (409, 'NAPA_DQA_REQUIRED')
    assert handoff_rows(db, no_pass) == []


def test_payment_recorded_after_dqa_blocks_handoff_and_new_files(real_stack, caplog):
    client, db, _ = real_stack
    add_people(db, [85001, 85002])
    paid, batch = batch_of(client, [85001]), batch_of(client, [85001, 85002])
    complete_dqa(client, db, batch)
    with caplog.at_level(logging.DEBUG):
        record_payment(db, paid, '85001')
        refused = hand_off(client, batch)
    assert code(refused) == (409, 'PAYMENT_HISTORY_CHANGED') and handoff_rows(db, batch) == []
    cur = db.cursor()
    cur.execute('DELETE FROM payment_transaction WHERE member_id=85001')
    db.commit()
    assert hand_off(client, batch).status_code == 201
    record_payment(db, paid, '85001')  # recorded after the handoff, before any file
    approve(db)
    try:
        response = client.post(f'{BASE}/batches/{batch}/exports', json={'file_type': 'DETAIL'},
                               headers={'Idempotency-Key': str(uuid4())})
        assert code(response) == (409, 'PAYMENT_HISTORY_CHANGED')
        cur.execute('SELECT COUNT(*) FROM bcmd_batch_export e JOIN bcmd_batch b ON b.id=e.batch_id WHERE b.public_id=%s',
                    (batch,))
        assert cur.fetchone()[0] == 0
        # A rerun records the finding; the new run is then handed off without that record.
        run = complete_dqa(client, db, batch)
        assert (run['passed'], run['review_count']) == (1, 1)
        assert hand_off(client, batch).json()['current']['passed_count'] == 1
        detail = new_export(client, batch)
        assert detail.status_code == 201, detail.text
        assert len(csv_rows(client.get(detail.json()['download_url']))) == 2  # header + the one PASS
    finally:
        cur.execute('DELETE FROM payment_transaction WHERE member_id=85001')
        db.commit(); cur.close()
    text = caplog.text
    for value in ('85001', '85002', *NAMES, 'SYN-85001'):
        assert value not in text


def test_unreadable_payment_history_fails_handoff_closed(real_stack, monkeypatch):
    client, db, _ = real_stack
    add_people(db, [86001])
    batch = batch_of(client, [86001])
    complete_dqa(client, db, batch)

    def unreadable(cur, refs):
        raise history.Unavailable()
    monkeypatch.setattr(history, 'recorded', unreadable)
    assert code(hand_off(client, batch)) == (503, 'PAYMENT_HISTORY_UNAVAILABLE')
    assert handoff_rows(db, batch) == []


def test_issued_files_survive_a_stale_handoff_and_stay_byte_identical(real_stack):
    client, db, _ = real_stack
    add_people(db, [87001])
    batch = batch_of(client, [87001])
    complete_dqa(client, db, batch)
    approve(db)
    issued = new_export(client, batch)
    assert issued.status_code == 201, issued.text
    first = client.get(issued.json()['download_url']).content
    # Simulate a later invalidation; the issued file is neither rebuilt differently nor removed.
    cur = db.cursor()
    cur.execute('UPDATE bcmd_batch SET claim_epoch=claim_epoch+1 WHERE public_id=%s', (batch,))
    db.commit(); cur.close()
    assert state(client, batch)['status'] == 'STALE'
    assert client.get(issued.json()['download_url']).content == first
    replay = client.post(f'{BASE}/batches/{batch}/exports', json={'file_type': 'DETAIL'},
                         headers={'Idempotency-Key': str(uuid4())})
    assert replay.status_code == 201 and replay.json()['export_no'] == issued.json()['export_no']
    summary = client.post(f'{BASE}/batches/{batch}/exports', json={'file_type': 'SUMMARY'},
                          headers={'Idempotency-Key': str(uuid4())})
    assert code(summary) == (409, 'NAPA_DQA_REQUIRED')
    # Rerunning DQA after export stays refused: the conflict is explicit, never repaired by mutation.
    rerun = client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())})
    assert code(rerun) == (409, 'DQA_BATCH_EXPORTED')


def test_napa_register_is_filtered_before_pagination(real_stack):
    client, db, _ = real_stack
    add_people(db, [88001, 88002, 88003])
    handed = []
    for number in (88001, 88002):
        batch = batch_of(client, [number])
        complete_dqa(client, db, batch)
        assert hand_off(client, batch).status_code == 201
        handed.append(batch)
        revive(db, batch)
    not_handed = batch_of(client, [88003])
    revive(db, handed[0])
    page = client.get(f'{BASE}/batches?register=napa&limit=1').json()
    assert page['has_more'] is True and [b['public_id'] for b in page['items']] == [handed[1]]
    second = client.get(f'{BASE}/batches?register=napa&limit=1&offset=1').json()
    assert [b['public_id'] for b in second['items']] == [handed[0]]
    everything = [b['public_id'] for b in client.get(f'{BASE}/batches?register=napa&limit=200').json()['items']]
    assert not_handed not in everything and set(handed) <= set(everything)
    assert not_handed in [b['public_id'] for b in client.get(f'{BASE}/batches?limit=200').json()['items']]
    statuses = {b['public_id']: b['napa_handoff']['status']
                for b in client.get(f'{BASE}/batches?register=napa&limit=200').json()['items']}
    assert statuses[handed[0]] == statuses[handed[1]] == 'VALID'


def test_concurrent_handoffs_of_one_run_record_one_row(real_stack, mysql_candidate):
    from routers.bcmd import get_bcmd_db
    client, db, app = real_stack
    add_people(db, [89001])
    batch = batch_of(client, [89001])
    complete_dqa(client, db, batch)
    run = latest_run(client, batch)
    results = []

    def separate():
        conn = mysql.connector.connect(**mysql_candidate)
        try:
            yield conn
        finally:
            conn.rollback(); conn.close()

    def go():
        response = hand_off(client, batch, run=run)
        results.append((response.status_code, response.json().get('current', {}).get('public_id')))
    shared = app.dependency_overrides[get_bcmd_db]
    app.dependency_overrides[get_bcmd_db] = separate
    try:
        threads = [threading.Thread(target=go) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        app.dependency_overrides[get_bcmd_db] = shared
    assert {status for status, _ in results} == {201} and len({ref for _, ref in results}) == 1
    assert len(handoff_rows(db, batch)) == 1


def test_handoff_requires_bcmd_access_before_any_change(real_stack):
    from scopes import LEGACY_CHED_READ
    from security import Grant, Principal, get_beneficiary_principal
    client, db, app = real_stack
    add_people(db, [89101])
    batch = batch_of(client, [89101])
    complete_dqa(client, db, batch)
    run = latest_run(client, batch)
    original = app.dependency_overrides.get(get_beneficiary_principal)
    try:
        for agency in ('CHED', 'TESDA'):
            app.dependency_overrides[get_beneficiary_principal] = (
                lambda a=agency: Principal(11, 'other', frozenset({Grant(LEGACY_CHED_READ, a, a)})))
            assert hand_off(client, batch, run=run).status_code == 403
            assert client.get(f'{BASE}/batches/{batch}/napa-handoff').status_code == 403
    finally:
        app.dependency_overrides[get_beneficiary_principal] = original
    assert handoff_rows(db, batch) == []
    assert state(client, batch)['status'] == 'VALID'


def test_rollback_0026_stops_once_a_handoff_is_recorded(real_stack):
    """Only the first rollback statement runs: it must fail and leave the audit check and handoffs as they are."""
    from pathlib import Path
    from scripts.migrate import statements
    client, db, _ = real_stack
    add_people(db, [89201])
    batch = batch_of(client, [89201])
    complete_dqa(client, db, batch)
    assert hand_off(client, batch).status_code == 201
    first = statements(Path('migrations/rollback/0026_bcmd_napa_handoff_down.sql').read_text(encoding='utf8'))[0]
    assert 'ck_bcmd_audit_type' in first and 'NAPA_HANDOFF_RECORDED' not in first
    cur = db.cursor()
    with pytest.raises(mysql.connector.Error) as refused:
        cur.execute(first)
    assert refused.value.errno == 3819  # ER_CHECK_CONSTRAINT_VIOLATED
    cur.execute("SELECT CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() "
                "AND CONSTRAINT_NAME='ck_bcmd_audit_type'")
    assert 'NAPA_HANDOFF_RECORDED' in cur.fetchone()[0]
    db.rollback(); cur.close()
    assert len(handoff_rows(db, batch)) == 1
