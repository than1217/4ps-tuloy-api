"""Real local MySQL coverage of distinct native/SSO creators and durable audit."""
import csv
import io
from uuid import uuid4

import mysql.connector
import pytest

from bcmd_actor import actor_column, actor_values
from errors import ApiError
from security import Grant, Principal, get_beneficiary_principal
from scopes import LEGACY_CHED_READ
from test_bcmd_mysql import mysql_candidate, real_stack, complete_dqa, create, BASE
from test_bcmd_export_mysql import approve, handoff, legacy_export, new_export


def identity(kind='staff', agency='DSWD', identifier=11):
    return Principal(identifier, 'synthetic-actor',
                     frozenset({Grant(LEGACY_CHED_READ, agency, agency)}), kind=kind)


@pytest.mark.parametrize('agency', ['DSWD', 'TEST'])
def test_sso_batch_export_download_and_payroll_preview(real_stack, agency, monkeypatch):
    client, db, app = real_stack
    # The shared fixture replaces FastAPI's DB dependency. Status also invokes
    # this generator directly; give that explicit read the same synthetic DB.
    from routers import bcmd
    def status_db(*args):
        try:
            yield db
        finally:
            db.rollback()
    monkeypatch.setattr(bcmd, 'get_bcmd_db', status_db)
    app.dependency_overrides[bcmd.bcmd_available] = lambda: True
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity(agency=agency)
    status = client.get(BASE+'/status').json()
    assert status['available'] is True and status['creation_available'] is True
    response = client.post(BASE+'/batches', headers={'Idempotency-Key': str(uuid4())},
                           json={'name': f'Synthetic SSO criteria {uuid4()}', 'category': 'TK',
                                 'criteria': {}, 'record_count': 2})
    assert response.status_code == 201, response.text
    batch = response.json()['public_id']
    cur = db.cursor()
    cur.execute("INSERT INTO bcmd_approval(gate_code,decision,approver_role,approver_label,`reference`,approved_on) "
                "VALUES ('PAYROLL_SEPARATION','REVOKED','MARTIN','SYNTHETIC','Synthetic gate test','2026-10-31')")
    db.commit(); cur.close()
    assert client.get(f'{BASE}/batches/{batch}/export-readiness').json()['ready'] is False
    assert new_export(client, batch).json()['error_code'] == 'NAPA_DQA_REQUIRED'
    approve(db)
    assert client.get(BASE+'/status').json()['export_available'] is True
    complete_dqa(client, db, batch)
    assert handoff(client, batch).status_code == 201
    assert client.get(f'{BASE}/batches/{batch}/export-readiness').json()['ready'] is True
    exported = new_export(client, batch)
    assert exported.status_code == 201, exported.text
    download = client.get(exported.json()['download_url'])
    assert download.status_code == 200
    assert len(list(csv.reader(io.StringIO(download.content.decode('utf8'))))) == 3
    preview = client.get(f'{BASE}/batches/{batch}/payroll-preview?months=2')
    assert preview.status_code == 200 and preview.json()['preview_only'] is True
    assert preview.json()['member_count'] == 2
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT id,created_by_api_user_id,created_by_staff_user_id,created_by_agency '
                'FROM bcmd_batch WHERE public_id=%s', (batch,))
    stored = cur.fetchone()
    assert stored['created_by_api_user_id'] is None
    assert stored['created_by_staff_user_id'] == 11 and stored['created_by_agency'] == agency
    cur.execute('SELECT created_by_api_user_id,created_by_staff_user_id FROM bcmd_batch_export '
                'WHERE batch_id=%s', (stored['id'],))
    assert cur.fetchall() == [{'created_by_api_user_id': None, 'created_by_staff_user_id': 11}]
    cur.execute('SELECT actor_api_user_id,actor_staff_user_id,actor_agency,event_type '
                'FROM bcmd_audit_event WHERE batch_id=%s ORDER BY id', (stored['id'],))
    events = cur.fetchall()
    assert [row['event_type'] for row in events] == [
        'BATCH_CREATED', 'DQA_COMPLETED', 'DQA_RUN_QUEUED', 'DQA_RUN_COMPLETED', 'NAPA_HANDOFF_RECORDED',
        'EXPORT_STARTED', 'EXPORT_COMPLETED',
        'DOWNLOAD_ADMITTED', 'DOWNLOAD_UNCERTAIN']
    events = [row for row in events if row['event_type'] != 'DQA_RUN_COMPLETED']  # recorded by the worker
    assert all(row['actor_api_user_id'] is None and row['actor_staff_user_id'] == 11
               and row['actor_agency'] == agency for row in events)
    db.rollback(); cur.close()


def test_same_numeric_ids_and_retry_keys_never_share_a_creator(real_stack):
    client, db, app = real_stack
    key = str(uuid4())
    native = create(client, key=key, name=f'Native {key}')
    assert native.status_code == 201
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity()
    sso = create(client, key=key, name=f'SSO {key}')
    assert sso.status_code == 201 and sso.json()['public_id'] != native.json()['public_id']
    assert create(client, key=key, name=f'SSO {key}').json() == sso.json()
    assert create(client, key=key, name='Changed request').status_code == 409
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity('api')
    assert create(client, key=key, name=f'Native {key}').json() == native.json()
    # Export retry namespace includes both the batch and the identity kind.
    approve(db)
    batch = native.json()['public_id']
    complete_dqa(client, db, batch)
    native_export = new_export(client, batch, key)
    assert native_export.status_code == 201
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity()
    sso_export = new_export(client, batch, key, 'SUMMARY')
    assert sso_export.status_code == 201
    assert sso_export.json()['export_no'] != native_export.json()['export_no']
    assert new_export(client, batch, key, 'SUMMARY').json() == sso_export.json()
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity('api')
    assert new_export(client, batch, key).json() == native_export.json()


def test_missing_sso_identity_never_falls_back_to_native_account(real_stack):
    client, db, app = real_stack
    cur = db.cursor()
    cur.execute('INSERT INTO api_users VALUES (999)'); db.commit()
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity(identifier=999)
    cur.execute('SELECT COUNT(*) FROM bcmd_batch'); before = cur.fetchone()[0]; db.rollback()
    assert create(client).status_code >= 500
    cur.execute('SELECT COUNT(*) FROM bcmd_batch'); assert cur.fetchone()[0] == before
    db.rollback(); cur.close()


@pytest.mark.parametrize('agency', ['CHED', 'TESDA'])
def test_sso_partner_affiliation_cannot_create_or_export(real_stack, agency):
    client, db, app = real_stack
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity(agency=agency)
    assert create(client).status_code == 403
    assert new_export(client, str(uuid4())).status_code == 403


def test_foreign_keys_and_exactly_one_actor_are_enforced(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    approve(db)
    complete_dqa(client, db, batch)
    assert new_export(client, batch).status_code == 201
    cur = db.cursor()
    for table, prefix in [('bcmd_batch', 'created_by'), ('bcmd_batch_export', 'created_by'),
                          ('bcmd_audit_event', 'actor')]:
        for native, staff in [(11, 11), (None, 999)]:
            with pytest.raises(mysql.connector.Error):
                cur.execute(f'UPDATE {table} SET {prefix}_api_user_id=%s,{prefix}_staff_user_id=%s',
                            (native, staff))
            db.rollback()
    for table in ('bcmd_batch', 'bcmd_batch_export'):
        with pytest.raises(mysql.connector.Error):
            cur.execute(f'UPDATE {table} SET created_by_api_user_id=NULL,created_by_staff_user_id=NULL')
        db.rollback()
    with pytest.raises(mysql.connector.Error):
        cur.execute('UPDATE bcmd_audit_event SET actor_api_user_id=NULL,actor_staff_user_id=NULL,actor_label=NULL')
    db.rollback(); cur.close()


def test_sso_audit_failure_rolls_back_the_batch(real_stack):
    client, db, app = real_stack
    app.dependency_overrides[get_beneficiary_principal] = lambda: identity()
    cur = db.cursor()
    cur.execute('SELECT COUNT(*) FROM bcmd_batch'); before = cur.fetchone()[0]; db.rollback()
    cur.execute("CREATE TRIGGER synthetic_staff_audit_failure BEFORE INSERT ON bcmd_audit_event "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Synthetic audit failure'")
    try:
        assert create(client).status_code >= 500
        cur.execute('SELECT COUNT(*) FROM bcmd_batch'); assert cur.fetchone()[0] == before
        db.rollback()
    finally:
        cur.execute('DROP TRIGGER synthetic_staff_audit_failure'); db.commit(); cur.close()


@pytest.mark.parametrize('kind,identifier', [('other', 11), ('staff', 0), ('api', True)])
def test_unknown_or_invalid_actor_is_refused(kind, identifier):
    with pytest.raises(ApiError):
        actor_column(identity(kind, identifier=identifier))


def test_actor_values_keep_colliding_numeric_ids_distinct():
    assert actor_values(identity('api')) == {'created_by_api_user_id': 11, 'created_by_staff_user_id': None}
    assert actor_values(identity()) == {'created_by_api_user_id': None, 'created_by_staff_user_id': 11}


def test_upgrade_keeps_historical_native_creators_and_audit(real_stack, mysql_candidate):
    """Copy native historical rows to a pre-upgrade schema, then apply 0014-0016."""
    from scripts.migrate import discover, statements
    client, db, app = real_stack
    public_id = create(client).json()['public_id']
    approve(db)
    legacy_export(db, public_id)  # history predates DQA runs and their audit events
    source = db.cursor(dictionary=True)
    source.execute('SELECT * FROM bcmd_batch WHERE public_id=%s', (public_id,))
    batch = source.fetchone()
    source.execute('SELECT * FROM bcmd_batch_export WHERE batch_id=%s', (batch['id'],))
    exports = source.fetchall()
    source.execute('SELECT * FROM bcmd_audit_event WHERE batch_id=%s ORDER BY id', (batch['id'],))
    audit = source.fetchall()
    db.rollback(); source.close()
    args = {k: mysql_candidate[k] for k in ('host', 'port', 'user', 'password')}
    assert args['host'] == '127.0.0.1' and args['port'] == 13498
    upgrade_db = 'tuloy_napa_test_' + uuid4().hex[:12]
    admin = mysql.connector.connect(**args, autocommit=True)
    cur = admin.cursor(dictionary=True)
    try:
        cur.execute(f'CREATE DATABASE `{upgrade_db}`')
        cur.execute(f'USE `{upgrade_db}`')
        for version, name, checksum, sql in discover():
            if version > '0013':
                break
            for statement in statements(sql):
                cur.execute(statement)
        cur.execute("INSERT INTO api_users(id,username,hashed_password) VALUES (11,'synthetic','not-a-login')")
        for table, rows in [('bcmd_batch', [batch]), ('bcmd_batch_export', exports),
                            ('bcmd_audit_event', audit)]:
            for row in rows:
                old = {key: val for key, val in row.items()
                       if not key.endswith('_staff_user_id') and key not in ('dqa_run_id', 'claim_epoch', 'payment_excluded_count')}
                columns = ','.join('`'+key+'`' for key in old)
                cur.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join(["%s"]*len(old))})', tuple(old.values()))
        for version, name, checksum, sql in discover():
            if version >= '0014':
                for statement in statements(sql):
                    cur.execute(statement)
        for table, rows, native, staff in [
            ('bcmd_batch', [batch], 'created_by_api_user_id', 'created_by_staff_user_id'),
            ('bcmd_batch_export', exports, 'created_by_api_user_id', 'created_by_staff_user_id'),
            ('bcmd_audit_event', audit, 'actor_api_user_id', 'actor_staff_user_id')]:
            cur.execute(f'SELECT * FROM {table} ORDER BY id')
            actual = cur.fetchall()
            assert len(actual) == len(rows)
            assert all(row[native] == 11 and row[staff] is None for row in actual)
            for old, upgraded in zip(rows, actual):
                assert upgraded == old
    finally:
        assert upgrade_db.startswith('tuloy_napa_test_') and len(upgrade_db) == len('tuloy_napa_test_')+12
        cur.execute(f'DROP DATABASE IF EXISTS `{upgrade_db}`')
        cur.close(); admin.close()
