"""DQA completion is sufficient for NAPA/Payroll discovery, without approvals or handoff writes."""
from database import get_ods_connection
from test_bcmd_mysql import mysql_candidate, real_stack, create, complete_dqa, BASE
from test_bcmd_export_mysql import new_export


def test_completed_dqa_is_visible_and_exportable_without_manual_approval(real_stack):
    client, db, app = real_stack
    batch = create(client, ids=[1]).json()['public_id']
    assert batch not in {b['public_id'] for b in client.get(BASE+'/batches?register=napa').json()['items']}
    run = complete_dqa(client, db, batch)
    row = next(b for b in client.get(BASE+'/batches?register=napa').json()['items'] if b['public_id'] == batch)
    assert row['napa_handoff']['automatic'] is True
    assert row['napa_handoff']['current']['dqa_run_id'] == run['public_id']
    assert row['napa_files']['DETAIL']['ready'] is True
    from routers.bcmd import bcmd_available
    app.dependency_overrides[bcmd_available] = lambda: True
    status = client.get(BASE+'/status').json()
    assert status['blocking_approvals'] == [] and status['export_available'] is True
    cur = db.cursor()
    cur.execute('SELECT COUNT(*) FROM bcmd_napa_handoff WHERE batch_id=(SELECT id FROM bcmd_batch WHERE public_id=%s)', (batch,))
    assert cur.fetchone()[0] == 0
    cur.close(); db.rollback()

    def connection():
        yield db
        db.rollback()
    app.dependency_overrides[get_ods_connection] = connection
    try:
        register = client.get('/v1/api/tuloy/staff/payments/batches')
        assert register.status_code == 200
        assert batch in {b['batch_id'] for b in register.json()['items']}
    finally:
        del app.dependency_overrides[get_ods_connection]
    exported = new_export(client, batch, hand_off=False)
    assert exported.status_code == 201, exported.text
    assert exported.json()['row_count'] == run['passed']
    assert client.get(exported.json()['download_url']).status_code == 200


def test_queued_rerun_cannot_export_or_appear_as_completed_payroll(real_stack):
    from uuid import uuid4
    client, db, app = real_stack
    batch = create(client, ids=[1]).json()['public_id']
    complete_dqa(client, db, batch)
    queued = client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())})
    assert queued.status_code == 202
    ready = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
    assert 'DQA_IN_PROGRESS' in ready['files']['DETAIL']['blocking_reasons']
    assert new_export(client, batch, hand_off=False).status_code == 409

    def connection():
        yield db
        db.rollback()
    app.dependency_overrides[get_ods_connection] = connection
    try:
        response = client.get('/v1/api/tuloy/staff/payments/batches')
        assert response.status_code == 200
        assert batch not in {b['batch_id'] for b in response.json()['items']}
    finally:
        del app.dependency_overrides[get_ods_connection]
