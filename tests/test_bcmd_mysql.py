"""Real MySQL acceptance in the dedicated synthetic container, opt-in only."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
from uuid import uuid4

import mysql.connector
import pytest
from fastapi.testclient import TestClient

from config import DbTarget, get_settings
from main import create_app
from routers.bcmd import get_bcmd_db
from scopes import LEGACY_CHED_READ
from security import Grant, Principal, get_beneficiary_principal


@pytest.fixture(scope='module')
def mysql_candidate():
    location = os.getenv('NAPA_TEST_CONFIG')
    if not location:
        pytest.skip('Set NAPA_TEST_CONFIG to the isolated synthetic container configuration')
    config = json.loads(Path(location).read_text(encoding='utf8'))
    assert config['host'] == '127.0.0.1' and config['port'] == 13498
    database = 'tuloy_napa_test_' + uuid4().hex[:12]
    args = {k: config[k] for k in ('host', 'port', 'user', 'password')}
    admin = mysql.connector.connect(**args)
    cur = admin.cursor()
    cur.execute(f'CREATE DATABASE `{database}`')
    cur.execute(f'USE `{database}`')
    ddl = '''
    CREATE TABLE api_users (id int PRIMARY KEY);
    CREATE TABLE staff_users (id bigint unsigned PRIMARY KEY);
    CREATE TABLE tbl_person (person_id int PRIMARY KEY,first_name varchar(50),middle_name varchar(50),last_name varchar(50),date_of_birth date,sex_id int,ext_name_id int,ip_group_id int);
    CREATE TABLE enroll_matched_CHED_tbl (id int PRIMARY KEY,tuloy_id varchar(50),member_id int,agency_id int,degree_academic_year varchar(20),degree_semester int,degree_year_level varchar(10),enrollment_status varchar(30),degree_program_name varchar(100));
    CREATE TABLE lib_partner_agency (agency_id int PRIMARY KEY,agency_alias varchar(20));
    CREATE TABLE enroll_matched_TESDA_tbl (id int PRIMARY KEY,tuloy_id varchar(50),member_id int,diploma JSON,short_course JSON);
    CREATE TABLE tbl_hh_roster (hh_roster_id int PRIMARY KEY,person_id int,household_id int,KEY(person_id));
    CREATE TABLE tbl_household (household_id int PRIMARY KEY,barangay_id int);
    CREATE TABLE lib_psgc_barangay (barangay_id int PRIMARY KEY,name varchar(100),psa_code char(10));
    CREATE TABLE lib_psgc_denorm (barangay_id int PRIMARY KEY,region_name varchar(100),region_psa_code char(10),province_name varchar(100),province_psa_code char(10),city_name varchar(100),city_psa_name char(10));
    CREATE TABLE tbl_family_selected (household_id int,family_id varchar(30),KEY(household_id));
    CREATE TABLE tbl_family_assoc_ops (fam_assoc_id int PRIMARY KEY,family_id varchar(30),person_id int,hh_roster_id int,archived int,KEY(family_id));
    CREATE VIEW vw_verify_member_bene AS SELECT person_id AS member_id FROM tbl_person;
    CREATE TABLE payment_transaction (payment_transaction_id int AUTO_INCREMENT PRIMARY KEY,batch_id bigint unsigned,member_id int,academic_year_term int,assistance_type int,amount decimal(12,2),status int,tuloy_payroll_type int,payment_mode_id int,sex varchar(20),KEY(member_id));
    '''
    from scripts.migrate import statements
    for sql in statements(ddl):
        cur.execute(sql)
    for sql in statements(Path('docs/schema/bcmd_candidate.sql').read_text(encoding='utf8')):
        cur.execute(sql)
    # Every BCMD migration from 0014 (0017, COR review, is not needed here).
    for path in sorted(Path('migrations').glob('*.sql')):
        if int(path.name[:4]) < 14 or path.name.startswith('0017_'):
            continue
        for sql in statements(path.read_text(encoding='utf8')):
            cur.execute(sql)
    cur.execute('INSERT INTO api_users VALUES (11)')
    cur.execute('INSERT INTO staff_users VALUES (11)')
    cur.execute("INSERT INTO lib_partner_agency VALUES (1,'CHED')")
    # Lookups from 0027, in the application database (staging copies them with docs/schema/bcmd_lookup_copy.sql).
    cur.execute("INSERT INTO lib_sex VALUES (1,'MALE','M',0),(2,'FEMALE','F',0),(3,'NONE','NA',0)")
    cur.execute("INSERT INTO lib_ext_name VALUES (2,'Jr.',0)")
    cur.execute("INSERT INTO lib_ip_group VALUES (7,'Synthetic group',NULL,1,0)")
    cur.execute("INSERT INTO lib_psgc_barangay VALUES (1,'Synthetic Barangay','0101010001')")
    cur.execute("INSERT INTO lib_psgc_denorm VALUES (1,'Synthetic Region','0100000000','Synthetic Province','0101000000','Synthetic Municipality','0101010000')")
    for number in range(1, 5):
        cur.execute("INSERT INTO tbl_person VALUES (%s,'Synthetic','Example','Student','2001-02-03',%s,2,%s)",
                    (number, 2 if number == 3 else 1, None if number == 1 else 0))
        cur.execute('INSERT INTO tbl_hh_roster VALUES (%s,%s,%s)', (number, number, number+100))
        cur.execute('INSERT INTO tbl_household VALUES (%s,1)', (number+100,))
        cur.execute("INSERT INTO enroll_matched_CHED_tbl VALUES (%s,%s,%s,1,'2026-2027',1,'1','ENROLLED','Synthetic course')", (number, f'SYN-{number}', number))
        if number != 1:
            cur.execute('INSERT INTO tbl_family_selected VALUES (%s,%s)', (number+100, f'SYN-FAMILY-{number}'))
            cur.execute('INSERT INTO tbl_family_assoc_ops VALUES (%s,%s,%s,%s,0)', (number, f'SYN-FAMILY-{number}', number, number))
    admin.commit()
    cur.close()
    yield {**args, 'database': database}
    # Only the uniquely named database created by this fixture can be dropped.
    assert database.startswith('tuloy_napa_test_') and len(database) == len('tuloy_napa_test_')+12
    admin.rollback()
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE `{database}`')
    cur.close()
    admin.close()


@pytest.fixture
def real_stack(mysql_candidate):
    db = mysql.connector.connect(**mysql_candidate)
    actor = Principal(11, 'synthetic-dswd', frozenset({Grant(LEGACY_CHED_READ, 'DSWD', 'DSWD')}))
    settings = replace(get_settings(), bcmd_enabled=True, bcmd_user='synthetic_bcmd', bcmd_password='synthetic',
        ods=DbTarget('127.0.0.1', 13498, mysql_candidate['database'], 'synthetic_read', 'synthetic'),
        rw_user='synthetic_write')
    app = create_app(settings)
    def connection():
        yield db
        db.rollback()
    app.dependency_overrides[get_beneficiary_principal] = lambda: actor
    app.dependency_overrides[get_bcmd_db] = connection
    client = TestClient(app)
    yield client, db, app
    client.close()
    db.close()


BASE = '/v1/api/tuloy/staff/bcmd'


def create(client, *, key=None, ids=None, name=None, category='TK'):
    # Batch names are unique; the default name follows the retry key so a replay matches.
    key = key or str(uuid4())
    return client.post(BASE+'/batches', headers={'Idempotency-Key': key},
                       json={'name': name or f'Synthetic batch {key}', 'category': category, 'record_ids': ids or [1, 2]})


def isolate(db, batch):
    """Retire every other batch so cross-batch duplicate checks do not apply to this test's batch."""
    cur = db.cursor()
    cur.execute("UPDATE bcmd_batch SET status='PURGED',purged_at=UTC_TIMESTAMP(3),purged_by='synthetic test isolation' "
                "WHERE public_id<>%s AND status<>'PURGED'", (batch,))
    db.commit()
    cur.close()


def complete_dqa(client, db, batch, *, isolated=True):
    """Queue a DQA run and drain the worker until it completes; returns the run."""
    import bcmd_dqa as dqa
    if isolated:
        isolate(db, batch)
    started = client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())})
    assert started.status_code == 202, started.text
    for _ in range(20):
        if not dqa.work_once(db):
            break
    run = client.get(f"{BASE}/batches/{batch}/dqa-runs/{started.json()['public_id']}").json()
    assert run['status'] == 'COMPLETED', run
    return run


def test_create_persist_reload_and_null_rules(real_stack, mysql_candidate):
    client, db, app = real_stack
    response = create(client)
    assert response.status_code == 201, response.text
    batch = response.json()
    assert batch['member_count'] == 2 and batch['dqa_outcome'] == 'CLEAN'
    public_id = batch['public_id']
    # A new DB connection / client reads the persisted snapshot.
    with mysql.connector.connect(**mysql_candidate) as another:
        app.dependency_overrides[get_bcmd_db] = lambda: another
        members = client.get(f'{BASE}/batches/{public_id}/members').json()['items']
        assert members[0]['hhid'] == '101' and members[0]['family_id'] is None
        assert members[0]['ip_indicator'] is None
        assert members[1]['hhid'] == members[1]['family_id'] == '102'
        assert members[1]['ip_indicator'] == 'NO'
        assert members[0]['region_psgc'] == '0100000000'
        events = client.get(f'{BASE}/batches/{public_id}/audit').json()['items']
        assert [r['event_type'] for r in events] == ['BATCH_CREATED', 'DQA_COMPLETED']
        assert client.get(f'{BASE}/batches/{public_id}').json()['export_ready'] is False


def test_idempotent_retry_and_conflicting_payload(real_stack):
    client, db, app = real_stack
    key = str(uuid4())
    first = create(client, key=key)
    second = create(client, key=key)
    assert first.status_code == second.status_code == 201
    assert first.json()['public_id'] == second.json()['public_id']
    assert first.json() == second.json()
    conflict = create(client, key=key, name='Different request')
    assert conflict.status_code == 409
    cur = db.cursor()
    cur.execute('SELECT COUNT(*) FROM bcmd_batch WHERE idempotency_key=%s', (key,))
    assert cur.fetchone()[0] == 1
    cur.close()
    db.rollback()


def test_missing_source_has_no_partial_batch(real_stack):
    client, db, app = real_stack
    response = create(client, ids=[1, 999999])
    assert response.status_code == 422
    assert response.json()['error_code'] == 'SOURCE_NOT_ELIGIBLE'


def test_pagination_detail_and_export_stays_disabled(real_stack):
    client, db, app = real_stack
    batch = create(client).json()['public_id']
    page = client.get(f'{BASE}/batches/{batch}/members?limit=1').json()
    assert len(page['items']) == 1 and page['has_more'] is True
    assert client.get(f'{BASE}/batches/{batch}/members?limit=201').status_code == 422
    assert client.get(f'{BASE}/batches/{uuid4()}').status_code == 404
    assert client.get(f'{BASE}/batches/{batch}/export').status_code == 409
    assert client.get(f'{BASE}/batches?limit=1').status_code == 200


def test_dqa_failure_is_persisted_not_disguised_as_clean(real_stack):
    client, db, app = real_stack
    cur = db.cursor()
    cur.execute('UPDATE tbl_person SET sex_id=3 WHERE person_id=3')
    db.commit()
    response = create(client, ids=[3])
    assert response.status_code == 201, response.text
    assert response.json()['dqa_outcome'] == 'BLOCKED'
    public_id = response.json()['public_id']
    issues = client.get(f'{BASE}/batches/{public_id}/issues').json()['items']
    assert any(r['rule_code'] == 'SEX_NOT_EXPORTABLE' for r in issues)
    cur.close()


def test_failed_audit_rolls_back_batch_and_members(real_stack):
    client, db, app = real_stack
    cur = db.cursor()
    cur.execute("CREATE TRIGGER synthetic_reject_audit BEFORE INSERT ON bcmd_audit_event FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic failure'")
    key = str(uuid4())
    try:
        response = create(client, key=key)
        assert response.status_code == 500
        cur.execute('SELECT COUNT(*) FROM bcmd_batch WHERE idempotency_key=%s', (key,))
        assert cur.fetchone()[0] == 0
    finally:
        db.rollback()
        cur.execute('DROP TRIGGER synthetic_reject_audit')
        cur.close()


def test_auth_denial_happens_before_batch_queries(real_stack):
    client, db, app = real_stack
    for agency in ('CHED', 'TESDA'):
        app.dependency_overrides[get_beneficiary_principal] = lambda a=agency: Principal(11, 'synthetic-other', frozenset({Grant(LEGACY_CHED_READ, a, a)}))
        assert client.get(BASE+'/status').status_code == 403
        assert create(client).status_code == 403


def test_concurrent_same_key_commits_only_one_batch(mysql_candidate):
    key = str(uuid4())
    actor = Principal(11, 'synthetic-dswd', frozenset({Grant(LEGACY_CHED_READ, 'DSWD', 'DSWD')}))
    def submit():
        db = mysql.connector.connect(**mysql_candidate)
        settings = replace(get_settings(), bcmd_enabled=True, bcmd_user='synthetic_bcmd', bcmd_password='synthetic',
            ods=DbTarget('127.0.0.1', 13498, mysql_candidate['database'], 'synthetic_read', 'synthetic'),
            rw_user='synthetic_write')
        app = create_app(settings)
        app.dependency_overrides[get_beneficiary_principal] = lambda: actor
        app.dependency_overrides[get_bcmd_db] = lambda: db
        client = TestClient(app)
        try:
            response = create(client, key=key)
            return response.status_code, response.json()
        finally:
            client.close()
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert any(code == 201 for code, _ in results)
    assert all(code in (201, 409) for code, _ in results)
    with mysql.connector.connect(**mysql_candidate) as db:
        cur = db.cursor()
        cur.execute('SELECT COUNT(*),MIN(id) FROM bcmd_batch WHERE idempotency_key=%s', (key,))
        count, batch_id = cur.fetchone()
        assert count == 1
        cur.execute('SELECT COUNT(*) FROM bcmd_audit_event WHERE batch_id=%s', (batch_id,))
        assert cur.fetchone()[0] == 2
        cur.close()


def test_numbered_migrations_rehearse_staging_gap_and_preserve_settings(mysql_candidate, monkeypatch):
    from scripts import migrate
    database = 'tuloy_napa_test_' + uuid4().hex[:12]
    args = {k: mysql_candidate[k] for k in ('host', 'port', 'user', 'password')}
    db = mysql.connector.connect(**args, autocommit=True)
    cur = db.cursor(dictionary=True)
    created_connections = []
    try:
        cur.execute(f'CREATE DATABASE `{database}`')
        cur.execute(f'USE `{database}`')
        migrate.ensure_ledger(cur)
        files = {version: (name, checksum, sql) for version, name, checksum, sql in migrate.discover()}
        # Reproduce exactly the versions in Martin's ledger, including the gap.
        for version in ('0001', '0002', '0004', '0005'):
            name, checksum, sql = files[version]
            for stmt in migrate.statements(sql):
                cur.execute(stmt)
            cur.execute('INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)', (version, name, checksum))
        for stmt in migrate.statements(Path('docs/schema/app_settings_existing.sql').read_text(encoding='utf8')):
            cur.execute(stmt)
        cur.execute("INSERT INTO app_settings(id,app_name,bg_type) VALUES (1,'Synthetic existing app','color')")
        cur.execute('CREATE INDEX idx_member_id ON enroll_matched_CHED_tbl(member_id)')
        cur.execute('SHOW CREATE TABLE app_settings')
        before = cur.fetchone()['Create Table']
        settings = replace(get_settings(), ods=replace(get_settings().ods, host='127.0.0.1',
            port=13498, database=database), migration_user=args['user'], migration_password=args['password'])
        monkeypatch.setattr(migrate, 'load_settings', lambda: settings)
        original_connect = migrate.connect
        def connect(s, holders):
            connection = original_connect(s, holders)
            created_connections.append(connection)
            return connection
        monkeypatch.setattr(migrate, 'connect', connect)
        assert migrate.main(['status']) == 6  # old 0003 gap must be explicitly reconciled
        assert migrate.main(['apply', '--backfill', '0003', '--yes']) == 0
        for number in range(6, 17):
            assert migrate.main(['apply', '--to', f'{number:04}', '--yes']) == 0
        assert migrate.main(['status']) == 0
        cur.execute('SHOW CREATE TABLE app_settings')
        assert cur.fetchone()['Create Table'] == before
        # Also validate the baseline's definitions on a fresh table, rather than
        # relying only on IF NOT EXISTS preserving the existing table.
        import re
        for stmt in migrate.statements(files['0007'][2]):
            cur.execute(stmt.replace('`app_settings`', '`app_settings_expected`', 1))
        cur.execute('SHOW CREATE TABLE app_settings_expected')
        fresh = cur.fetchone()['Create Table'].replace('`app_settings_expected`', '`app_settings`', 1)
        assert re.sub(r' AUTO_INCREMENT=\d+', '', fresh) == re.sub(r' AUTO_INCREMENT=\d+', '', before)
        cur.execute('SELECT id,app_name,bg_type FROM app_settings')
        assert cur.fetchall() == [{'id': 1, 'app_name': 'Synthetic existing app', 'bg_type': 'color'}]
        cur.execute('SELECT version,name,checksum FROM schema_migrations ORDER BY version')
        ledger = cur.fetchall()
        assert len(ledger) == 16
        assert all((row['name'], row['checksum']) == files[row['version']][:2] for row in ledger)
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'bcmd_%'")
        assert cur.fetchone()['n'] == 6
        cur.execute("SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='api_user_scopes' AND COLUMN_NAME='expires_at'")
        assert cur.fetchone()['IS_NULLABLE'] == 'YES'
        cur.execute("SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl'")
        assert {'idx_member_id', 'idx_enroll_matched_tuloy_id'} <= {row['INDEX_NAME'] for row in cur.fetchall()}
    finally:
        for connection in created_connections:
            connection.close()
        assert database.startswith('tuloy_napa_test_') and len(database) == len('tuloy_napa_test_')+12
        cur.execute(f'DROP DATABASE IF EXISTS `{database}`')
        cur.close()
        db.close()


def test_dbgate_package_guards_target_drift_and_records_only_verified_schema(mysql_candidate):
    from scripts import migrate
    from scripts.build_napa_dbgate_package import TARGET_DB, TARGET_HOST
    database = 'tuloy_napa_test_' + uuid4().hex[:12]
    args = {k: mysql_candidate[k] for k in ('host', 'port', 'user', 'password')}
    db = mysql.connector.connect(**args, autocommit=True)
    cur = db.cursor(dictionary=True)
    files = {v:(n,c,s) for v,n,c,s in migrate.discover()}
    def execute_file(version, *, local_target=True):
        sql = (Path('docs/schema/dbgate-apply')/files[version][0]).read_text(encoding='utf8')
        if local_target:
            sql = sql.replace("'"+TARGET_DB+"'", "'"+database+"'").replace("'"+TARGET_HOST+"'", "'"+hostname+"'")
        outcomes = []
        for statement in migrate.statements(sql):
            cur.execute(statement)
            if cur.with_rows:
                outcomes.extend(cur.fetchall())
        return next(row for row in outcomes if 'step_status' in row)
    try:
        cur.execute(f'CREATE DATABASE `{database}`')
        cur.execute(f'USE `{database}`')
        cur.execute('SELECT @@hostname AS host')
        hostname = cur.fetchone()['host']
        migrate.ensure_ledger(cur)
        for version in ('0001','0002','0004','0005'):
            name,checksum,sql = files[version]
            for statement in migrate.statements(sql):
                cur.execute(statement)
            cur.execute('INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)', (version,name,checksum))
        for statement in migrate.statements(Path('docs/schema/app_settings_existing.sql').read_text(encoding='utf8')):
            cur.execute(statement)
        cur.execute("INSERT INTO app_settings(id,app_name,bg_type) VALUES (1,'Synthetic existing settings','color')")
        # Unmodified staging-target script must not act on the synthetic target.
        assert execute_file('0003', local_target=False)['step_status'] == 'STOP'
        cur.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE version='0003'")
        assert cur.fetchone()['n'] == 0
        assert execute_file('0003')['step_status'] == 'VERIFIED'
        assert execute_file('0006')['step_status'] == 'VERIFIED'
        # Baseline adoption may not conceal a drifted existing settings table.
        cur.execute('ALTER TABLE app_settings MODIFY app_name varchar(254) NOT NULL')
        assert execute_file('0007')['step_status'] == 'STOP'
        cur.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE version='0007'")
        assert cur.fetchone()['n'] == 0
        cur.execute('ALTER TABLE app_settings MODIFY app_name varchar(255) NOT NULL')
        for number in range(7,17):
            if number == 8:
                # Rehearse DDL committed just before a crash / absent ledger row.
                for statement in migrate.statements(files['0008'][2]):
                    cur.execute(statement)
            assert execute_file(f'{number:04}')['step_status'] == 'VERIFIED'
        assert execute_file('0016')['step_status'] == 'ALREADY_RECORDED'
        cur.execute('SELECT COUNT(*) AS n FROM schema_migrations')
        assert cur.fetchone()['n'] == 16
        cur.execute('SELECT app_name FROM app_settings WHERE id=1')
        assert cur.fetchone()['app_name'] == 'Synthetic existing settings'
        # 0017 (COR review) is outside the package: staging recorded it through the runner.
        assert not (Path('docs/schema/dbgate-apply')/files['0017'][0]).exists()
        assert execute_file('0018')['step_status'] == 'STOP'
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'bcmd_dqa_r%'")
        assert cur.fetchone()['n'] == 0
        name, checksum, sql = files['0017']
        for statement in migrate.statements(sql):
            cur.execute(statement)
        cur.execute('INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)', ('0017', name, checksum))
        # Out of order: 0019 may not act before 0018 is recorded.
        assert execute_file('0019')['step_status'] == 'STOP'
        # Interrupted after the first statement: the guarded retry runs only the remaining one.
        for version in ('0018', '0019'):
            cur.execute(migrate.statements(files[version][2])[0])
            assert execute_file(version)['step_status'] == 'VERIFIED'
        # Staging already holds batches: legacy names that collide under the column collation
        # are renamed by 0020's own UPDATE before the unique index is added.
        duplicate = 'Legacy duplicate batch'
        cur.execute('SET SESSION foreign_key_checks=0')  # synthetic creator id only
        for variant in (duplicate, duplicate.upper(), duplicate.lower()):
            cur.execute('''INSERT INTO bcmd_batch (public_id,name,category,criteria,policy_version,
                entitlement_def_version,dqa_ruleset_id,dqa_ruleset_version,dqa_ruleset_sha256,dqa_config_sha256,
                dqa_config_canonical,dqa_evaluation_date,snapshot_started_at,source_input_sha256,dqa_completed_at,
                candidate_count,member_count,excluded_not_eligible,excluded_no_tuloy_id,excluded_duplicate_episode,
                dqa_pass_count,dqa_warn_count,dqa_block_count,dqa_warn_issue_count,dqa_block_issue_count,dqa_outcome,
                created_by_agency,request_id,idempotency_key,request_hash,created_by_api_user_id)
                VALUES (%s,%s,'TK','{}','p','e','r','v',%s,SHA2(%s,256),%s,'2026-11-04','2026-11-04 00:00:00',%s,
                '2026-11-04 00:00:00',0,0,0,0,0,0,0,0,1,0,'WARN','DSWD','synthetic',%s,%s,1)''',
                (str(uuid4()), variant, '0'*64, '{}', '{}', '0'*64, str(uuid4()), '0'*64))
        cur.execute('SET SESSION foreign_key_checks=1')
        assert execute_file('0020')['step_status'] == 'VERIFIED'
        cur.execute('SELECT id,name FROM bcmd_batch ORDER BY id')
        rows = cur.fetchall()
        assert [r['name'] for r in rows] == [duplicate, f"{duplicate.upper()} #{rows[1]['id']}",
                                             f"{duplicate.lower()} #{rows[2]['id']}"]
        for version in ('0021', '0022'):
            assert execute_file(version)['step_status'] == 'VERIFIED'
        assert execute_file('0022')['step_status'] == 'ALREADY_RECORDED'
        # 0023 (duplicate-selection review): interrupted after its first CREATE TABLE, the guarded
        # retry applies only the remaining statements over the existing batches.
        cur.execute(migrate.statements(files['0023'][2])[0])
        assert execute_file('0023')['step_status'] == 'VERIFIED'
        assert execute_file('0023')['step_status'] == 'ALREADY_RECORDED'
        cur.execute('SELECT DISTINCT claim_epoch FROM bcmd_batch')
        assert cur.fetchall() == [{'claim_epoch': 0}]
        # 0024 (payment history), 0025 (unified sources) and 0026 (NAPA handoff): interrupted after the first statement,
        # the guarded retry applies only the remaining one; existing batches stay CHED and uncounted.
        for version in ('0024', '0025', '0026'):
            cur.execute(migrate.statements(files[version][2])[0])
            assert execute_file(version)['step_status'] == 'VERIFIED'
            assert execute_file(version)['step_status'] == 'ALREADY_RECORDED'
        cur.execute('SELECT DISTINCT source_agency,payment_excluded_count FROM bcmd_batch')
        assert cur.fetchall() == [{'source_agency': 'CHED', 'payment_excluded_count': None}]
        # 0027 (lookup tables): a same-named table with another shape is never adopted; interrupted
        # after the first table, the retry creates only the other two.
        cur.execute('CREATE TABLE lib_sex (sex_id int PRIMARY KEY)')
        assert execute_file('0027')['step_status'] == 'STOP'
        cur.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE version='0027'")
        assert cur.fetchone()['n'] == 0
        cur.execute('DROP TABLE lib_sex')
        cur.execute(migrate.statements(files['0027'][2])[0])
        assert execute_file('0027')['step_status'] == 'VERIFIED'
        assert execute_file('0027')['step_status'] == 'ALREADY_RECORDED'
        # The result is exactly what the API readiness check requires.
        cur.execute('SELECT version,name,checksum FROM schema_migrations ORDER BY version')
        assert cur.fetchall() == [{'version': v, 'name': n, 'checksum': h} for v, n, h, _ in migrate.discover()]
        from scripts.build_napa_dbgate_package import metadata
        expected = json.loads(Path('docs/schema/dbgate-apply/expected_schema.json').read_text(encoding='utf8'))
        for table in ('app_settings', 'bcmd_batch', 'bcmd_batch_member', 'bcmd_dqa_issue', 'bcmd_batch_export',
                      'bcmd_audit_event', 'bcmd_approval', 'bcmd_dqa_run', 'bcmd_dqa_result',
                      'bcmd_claim_conflict', 'bcmd_claim_resolution', 'bcmd_claim_resolution_member',
                      'bcmd_payment_member_lock', 'bcmd_payment_exclusion', 'bcmd_napa_handoff',
                      'lib_sex', 'lib_ext_name', 'lib_ip_group'):
            assert json.loads(json.dumps(metadata(cur, table), default=str)) == expected[table], table
    finally:
        cur.execute("SELECT RELEASE_LOCK('tuloy_schema_migrate')")
        cur.fetchall()
        assert database.startswith('tuloy_napa_test_') and len(database)==len('tuloy_napa_test_')+12
        cur.execute(f'DROP DATABASE IF EXISTS `{database}`')
        cur.close()
        db.close()


@pytest.mark.parametrize('column,value', [
    ('enrollment_status', 'GRADUATED'), ('enrollment_status', None),
    ('degree_year_level', '2'), ('degree_year_level', None),
    ('degree_semester', 2), ('degree_semester', None),
])
def test_tk_rules_apply_to_discovery_and_direct_create(real_stack, column, value):
    client, db, app = real_stack
    cur = db.cursor(dictionary=True)
    cur.execute(f'SELECT {column} AS original FROM enroll_matched_CHED_tbl WHERE id=4')
    original = cur.fetchone()['original']
    cur.execute('SELECT COUNT(*) AS n FROM bcmd_batch')
    before = cur.fetchone()['n']
    try:
        cur.execute(f'UPDATE enroll_matched_CHED_tbl SET {column}=%s WHERE id=4', (value,))
        db.commit()
        listed = client.get(BASE+'/candidates')
        assert listed.status_code == 200
        assert 4 not in [r['source_record_id'] for r in listed.json()['items']]
        response = create(client, ids=[4])
        assert response.status_code == 422, response.text
        cur.execute('SELECT COUNT(*) AS n FROM bcmd_batch')
        assert cur.fetchone()['n'] == before
    finally:
        db.rollback()
        cur.execute(f'UPDATE enroll_matched_CHED_tbl SET {column}=%s WHERE id=4', (original,))
        db.commit()
        cur.close()


def test_optional_names_and_indefinite_policy_are_frozen(real_stack):
    client, db, app = real_stack
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT middle_name,ext_name_id FROM tbl_person WHERE person_id=4')
    original = cur.fetchone()
    try:
        cur.execute('UPDATE tbl_person SET middle_name=NULL,ext_name_id=NULL WHERE person_id=4')
        db.commit()
        response = create(client, ids=[4])
        assert response.status_code == 201, response.text
        assert response.json()['dqa_outcome'] == 'CLEAN'
        cur.execute('SELECT dqa_config_canonical FROM bcmd_batch WHERE public_id=%s',
                    (response.json()['public_id'],))
        policy = json.loads(cur.fetchone()['dqa_config_canonical'])
        assert policy['eligibility'] == 'tk-unified-enrolled-y1-s1-age-v3' and policy['age']['maximum'] == 30
        assert policy['source'] == 'CHED_DEGREE_OR_TESDA_DIPLOMA_WITHOUT_CHED_MATCH'
        assert policy['payment_history']['paid_statuses'] == [] and policy['short_course'] == 'not_selectable_policy_pending'
        assert policy['retention'] == {'automatic_purge': False, 'period_days': None,
                                      'mode': 'indefinite_until_manual_purge'}
    finally:
        db.rollback()
        cur.execute('UPDATE tbl_person SET middle_name=%s,ext_name_id=%s WHERE person_id=4',
                    (original['middle_name'], original['ext_name_id']))
        db.commit()
        cur.close()


@pytest.mark.parametrize('reject_audit', [False, True])
def test_manual_purge_is_atomic_dry_run_and_replay_safe(real_stack, reject_audit):
    from bcmd_store import PERSONAL_COLUMNS
    from scripts.purge_napa_batch import purge_batch
    client, db, app = real_stack
    public_id = create(client).json()['public_id']
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT DATABASE() AS db,@@hostname AS host')
    identity = cur.fetchone()
    db.rollback()
    target = dict(expected_database=identity['db'], expected_hostname=identity['host'])
    with pytest.raises(ValueError, match='identity mismatch'):
        purge_batch(db, public_id, **{**target, 'expected_hostname': 'wrong-host'}, apply=True)
    preview = purge_batch(db, public_id, **target)
    assert preview['status'] == 'DRY_RUN' and preview['personal_columns'] == 18
    assert client.get(f'{BASE}/batches/{public_id}/members').status_code == 200
    if reject_audit:
        cur.execute("CREATE TRIGGER synthetic_reject_purge BEFORE INSERT ON bcmd_audit_event FOR EACH ROW BEGIN IF NEW.event_type='BATCH_PURGED' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic failure'; END IF; END")
        try:
            with pytest.raises(mysql.connector.Error):
                purge_batch(db, public_id, **target, apply=True)
            assert client.get(f'{BASE}/batches/{public_id}/members').json()['items'][0]['hhid'] == '101'
        finally:
            db.rollback()
            cur.execute('DROP TRIGGER synthetic_reject_purge')
    result = purge_batch(db, public_id, **target, apply=True)
    assert result['status'] == 'PURGED'
    assert purge_batch(db, public_id, **target, apply=True)['status'] == 'ALREADY_PURGED'
    assert client.get(f'{BASE}/batches/{public_id}/members').status_code == 410
    cur.execute('SELECT ' + ','.join('m.'+c for c in PERSONAL_COLUMNS) +
                ' FROM bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id WHERE b.public_id=%s', (public_id,))
    assert all(all(v is None for v in row.values()) for row in cur.fetchall())
    cur.execute("SELECT COUNT(*) AS n FROM bcmd_audit_event a JOIN bcmd_batch b ON b.id=a.batch_id WHERE b.public_id=%s AND a.event_type='BATCH_PURGED'", (public_id,))
    assert cur.fetchone()['n'] == 1
    cur.close()


def test_manual_purge_refuses_in_progress_export(real_stack):
    from scripts.purge_napa_batch import purge_batch
    client, db, app = real_stack
    public_id = create(client).json()['public_id']
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT DATABASE() AS db,@@hostname AS host')
    identity = cur.fetchone()
    cur.execute('SELECT id FROM bcmd_batch WHERE public_id=%s', (public_id,))
    batch_id = cur.fetchone()['id']
    cur.execute("""INSERT INTO bcmd_batch_export
        (batch_id,export_no,status,generator_version,serialization_version,header_set_sha256,
         idempotency_key,request_hash,created_by_api_user_id,created_by_agency,request_id)
        VALUES (%s,1,'STARTED','synthetic','synthetic',%s,%s,%s,11,'DSWD',%s)""",
        (batch_id, '0'*64, str(uuid4()), '1'*64, str(uuid4())))
    db.commit()
    with pytest.raises(ValueError, match='in progress'):
        purge_batch(db, public_id, expected_database=identity['db'],
                    expected_hostname=identity['host'], apply=True)
    assert client.get(f'{BASE}/batches/{public_id}/members').json()['items'][0]['hhid'] == '101'
    cur.close()



def test_staging_preflight_with_actual_least_privilege_mysql_account(mysql_candidate):
    import secrets
    from bcmd_readiness import READ_TABLES, WRITE_TABLES, verify
    from errors import ApiError
    from scripts import migrate
    from test_bcmd_readiness import direct_grants
    database = 'tuloy_napa_test_' + uuid4().hex[:12]
    user = 'napa_' + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    args = {k: mysql_candidate[k] for k in ('host', 'port', 'user', 'password')}
    admin = mysql.connector.connect(**args, autocommit=True)
    cur = admin.cursor(dictionary=True)
    runtime = None
    try:
        cur.execute(f'CREATE DATABASE `{database}`')
        cur.execute(f'USE `{database}`')
        migrate.ensure_ledger(cur)
        for version, name, checksum, sql in migrate.discover():
            for stmt in migrate.statements(sql):
                cur.execute(stmt)
            cur.execute('INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)', (version,name,checksum))
        for table in READ_TABLES:
            cur.execute(f'CREATE TABLE IF NOT EXISTS `{table}` (synthetic_id int)')
        # Readiness also needs the 0027 lookups filled (staging: docs/schema/bcmd_lookup_copy.sql).
        cur.execute("INSERT INTO lib_sex VALUES (1,'MALE','M',0)")
        cur.execute("INSERT INTO lib_ext_name VALUES (2,'Jr.',0)")
        cur.execute("INSERT INTO lib_ip_group VALUES (7,'Synthetic group',NULL,1,0)")
        cur.execute('SELECT @@hostname AS host')
        hostname = cur.fetchone()['host']
        cur.execute(f"CREATE USER `{user}`@'%' IDENTIFIED BY %s", (password,))
        from scripts.napa_runtime_grants import render
        # Wrong-target scripts must not give the newly created account any rights.
        for stmt in migrate.statements(render(user, '%', database, 'wrong-host')):
            cur.execute(stmt)
            if cur.with_rows:
                cur.fetchall()
        cur.execute(f"SHOW GRANTS FOR '{user}'@'%'")
        assert len(cur.fetchall()) == 1  # USAGE only
        for stmt in migrate.statements(render(user, '%', database, hostname)):
            cur.execute(stmt)
            if cur.with_rows:
                cur.fetchall()
        runtime = mysql.connector.connect(**{**args, 'user': user, 'password': password, 'database': database})
        settings = replace(get_settings(), app_env='staging', bcmd_enabled=True,
            bcmd_use_existing_account=False,
            bcmd_user=user, bcmd_password=password, ods=replace(get_settings().ods, database=database),
            bcmd_expected_database=database, bcmd_expected_hostname=hostname)
        verify(runtime, settings)
        with pytest.raises(ApiError):
            verify(runtime, replace(settings, bcmd_expected_hostname='wrong-host'))
        cur.execute(f"GRANT INSERT ON `{database}`.`bcmd_approval` TO `{user}`@'%'")
        with pytest.raises(ApiError):
            verify(runtime, settings)
        cur.execute(f"REVOKE INSERT ON `{database}`.`bcmd_approval` FROM `{user}`@'%'")
        verify(runtime, settings)
        cur.execute("UPDATE schema_migrations SET checksum=%s WHERE version='0013'", ('0'*64,))
        with pytest.raises(ApiError):
            verify(runtime, settings)
    finally:
        if runtime is not None:
            runtime.close()
        cur.execute(f"DROP USER IF EXISTS `{user}`@'%'")
        cur.execute(f'DROP DATABASE IF EXISTS `{database}`')
        cur.close()
        admin.close()


@pytest.mark.skipif(os.getenv('NAPA_BROWSER_TEST') != '1', reason='Opt-in local browser/API/MySQL acceptance')
def test_browser_napa_real_auth_and_persistence(mysql_candidate):
    import bcrypt
    import subprocess
    import threading
    import time
    import urllib.request
    import uvicorn
    from fastapi_cache import FastAPICache
    from fastapi_cache.backends.inmemory import InMemoryBackend
    import database as connections
    from routers.bcmd import bcmd_available
    from scripts.migrate import statements
    admin = mysql.connector.connect(**mysql_candidate)
    cur = admin.cursor()
    cur.execute('ALTER TABLE api_users ADD username varchar(255),ADD hashed_password varchar(255),ADD disabled tinyint DEFAULT 0')
    password_hash = bcrypt.hashpw(b'synthetic-browser-password', bcrypt.gensalt(rounds=4)).decode()
    cur.execute("UPDATE api_users SET username='synthetic-browser-dswd',hashed_password=%s WHERE id=11", (password_hash,))
    cur.execute("INSERT INTO api_users(id,username,hashed_password) VALUES (12,'synthetic-browser-test',%s)", (password_hash,))
    cur.execute("INSERT INTO api_users(id,username,hashed_password) VALUES (13,'synthetic-browser-ched',%s)", (password_hash,))
    for filename in ('0002_api_user_scopes.sql', '0006_api_user_scopes_nullable_expiry.sql'):
        for sql in statements((Path('migrations')/filename).read_text(encoding='utf8')):
            cur.execute(sql)
    for uid, agency in ((11, 'DSWD'), (12, 'TEST'), (13, 'CHED')):
        cur.execute("INSERT INTO api_user_scopes(user_id,scope,partner_id,agency,granted_by,expires_at) VALUES (%s,%s,%s,%s,'synthetic-browser',NULL)",
                    (uid, 'tuloy:docs:internal', agency, agency))
    # Neither browser user holds the CHED-read scope. DSWD affiliation is sufficient
    # for NAPA under Martin's latest ruling; TEST is admitted and CHED denied.
    admin.commit()
    cur.close()
    admin.close()
    settings = replace(get_settings(), bcmd_enabled=True, bcmd_user='synthetic_bcmd',
        bcmd_password='synthetic', ods=DbTarget('127.0.0.1',13498,mysql_candidate['database'],'synthetic_read','synthetic'),
        rw_user='synthetic_write')
    app = create_app(settings)
    def connection():
        db = mysql.connector.connect(**mysql_candidate)
        try:
            yield db
        finally:
            db.rollback()
            db.close()
    # Keep the real password/JWT/live-grant authentication dependencies. Only the
    # isolated connection transport replaces pooling; production readiness has
    # separate real least-privilege account acceptance above.
    app.dependency_overrides[connections.get_readonly_connection] = connection
    app.dependency_overrides[connections.get_app_connection] = connection
    app.dependency_overrides[get_bcmd_db] = connection
    app.dependency_overrides[bcmd_available] = lambda: True
    FastAPICache.init(InMemoryBackend(), prefix='napa-browser')
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=13086,
        lifespan='off', log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started, 'Local API did not start'
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:13085', timeout=5) as response:
            assert response.status == 200, 'Start the local Vite server on 13085 first'
        frontend = Path('../_agency-web/frontend').resolve()
        result = subprocess.run(['npx.cmd','playwright','test','e2e/napa.e2e.mjs'], cwd=frontend,
            env={**os.environ, 'NAPA_BROWSER_TEST':'1', 'E2E_BASE_URL':'http://127.0.0.1:13085'},
            capture_output=True, text=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        print(result.stdout)
        if result.returncode:
            print(result.stderr)
        assert result.returncode == 0, 'Browser acceptance failed; see Playwright report'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
