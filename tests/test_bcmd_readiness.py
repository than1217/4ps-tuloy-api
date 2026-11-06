from dataclasses import replace

import pytest

from bcmd_readiness import (READ_TABLES, REFERENCE_TABLES, WRITE_TABLES, UPDATE_TABLES,
                            validate_grants, validate_existing_account_grants)
from config import ConfigError, get_settings
from errors import ApiError
from routers.bcmd import validate_local_candidate


def direct_grants(database='synthetic_db'):
    return ["GRANT USAGE ON *.* TO `synthetic_bcmd`@`%`"] + [
        f"GRANT SELECT{', INSERT' if table in WRITE_TABLES or table == 'bcmd_batch_export' else ''}{', UPDATE' if table in UPDATE_TABLES else ''} ON `{database}`.`{table}` TO `synthetic_bcmd`@`%`"
        for table in READ_TABLES]


def test_exact_runtime_grants_are_accepted():
    validate_grants(direct_grants(), 'synthetic_db')


def test_table_case_follows_server_setting_instead_of_assuming_linux():
    grants = [grant.replace('enroll_matched_CHED_tbl', 'enroll_matched_ched_tbl') for grant in direct_grants()]
    with pytest.raises(ApiError):
        validate_grants(grants, 'synthetic_db')
    validate_grants(grants, 'synthetic_db', case_insensitive=True)


@pytest.mark.parametrize('grant', [
    'GRANT ALL PRIVILEGES ON *.* TO `u`@`%`',
    'GRANT SELECT, INSERT ON `synthetic_db`.* TO `u`@`%`',
    'GRANT UPDATE ON `synthetic_db`.`bcmd_batch_member` TO `u`@`%`',
    'GRANT INSERT ON `synthetic_db`.`schema_migrations` TO `u`@`%`',
    'GRANT INSERT ON `synthetic_db`.`bcmd_approval` TO `u`@`%`',
    'GRANT SELECT ON `synthetic_db`.`bcmd_batch` TO `u`@`%` WITH GRANT OPTION',
    'GRANT `operator_role`@`%` TO `u`@`%`',
    'GRANT SELECT (`id`) ON `synthetic_db`.`bcmd_batch` TO `u`@`%`',
])
def test_excessive_or_indirect_grants_fail_closed(grant):
    with pytest.raises(ApiError):
        validate_grants(direct_grants() + [grant], 'synthetic_db')


def test_missing_required_privilege_fails_closed():
    with pytest.raises(ApiError):
        validate_grants(direct_grants()[:-1], 'synthetic_db')


def test_shared_account_accepts_existing_broader_rights_without_new_grants():
    grants = ['GRANT SELECT, INSERT, UPDATE, CREATE, ALTER ON `synthetic_db`.* TO `u`@`%`']
    validate_existing_account_grants(grants, 'synthetic_db')
    validate_existing_account_grants(['GRANT ALL PRIVILEGES ON *.* TO `u`@`%` WITH GRANT OPTION'], 'synthetic_db')
    with pytest.raises(ApiError):
        validate_grants(grants, 'synthetic_db')


@pytest.mark.parametrize('grants', [
    ['GRANT SELECT ON *.* TO `u`@`%`'],
    ['GRANT ALL PRIVILEGES ON `other_db`.* TO `u`@`%`'],
    ['GRANT ALL PRIVILEGES ON *.* TO `u`@`%`', 'REVOKE SELECT ON `synthetic_db`.* FROM `u`@`%`'],
    ['GRANT `role`@`%` TO `u`@`%`'],
])
def test_shared_account_missing_or_ambiguous_rights_still_fail(grants):
    with pytest.raises(ApiError):
        validate_existing_account_grants(grants, 'synthetic_db')


def test_shared_schema_grants_respect_escaped_underscores():
    grants = [r'GRANT ALL PRIVILEGES ON `synthetic\_db`.* TO `u`@`%`']
    validate_existing_account_grants(grants, 'synthetic_db')
    with pytest.raises(ApiError):
        validate_existing_account_grants(grants, 'syntheticXdb')


def test_staging_config_requires_explicit_identity_and_separate_account():
    settings = replace(get_settings(), app_env='staging', bcmd_enabled=True,
        bcmd_use_existing_account=False,
        bcmd_user='synthetic_bcmd', bcmd_password='synthetic',
        bcmd_expected_database='synthetic_db', bcmd_expected_hostname='synthetic-host')
    validate_local_candidate(settings)
    for change in ({'bcmd_expected_hostname': ''}, {'bcmd_expected_hostname': ' , '}, {'bcmd_expected_database': 'other'},
                   {'bcmd_password': ''}, {'bcmd_user': settings.ods.user},
                   {'migration_user': 'synthetic_bcmd'}, {'rw_user': 'synthetic_bcmd'}):
        with pytest.raises(ConfigError):
            validate_local_candidate(replace(settings, **change))


def test_bad_bcmd_login_does_not_discard_other_application_pools(monkeypatch):
    import database
    from mysql.connector import Error
    ro, rw = object(), object()
    pools = {'ro': ro, 'rw': rw}
    monkeypatch.setattr(database, '_pools', pools)
    monkeypatch.setattr(database, '_down_until', 0)
    monkeypatch.setattr(database, '_build_pools', lambda settings: None)
    monkeypatch.setattr(database, '_endpoint', lambda settings: ('127.0.0.1', 13498))
    def fail(**kwargs):
        raise Error('synthetic rejected BCMD login', errno=1045)
    monkeypatch.setattr(database, 'MySQLConnectionPool', fail)
    with pytest.raises(ApiError):
        database._checkout('bcmd', replace(get_settings(), bcmd_user='synthetic_bcmd',
                                           bcmd_password='synthetic'))
    assert pools == {'ro': ro, 'rw': rw}


def readiness_fixture(fake_db):
    import json
    import bcmd_readiness as readiness

    schema = json.loads((readiness.ROOT / 'docs/schema/dbgate-apply/expected_schema.json').read_text())
    state = {'schema': schema, 'grants': direct_grants(), 'filled': set(REFERENCE_TABLES)}
    fake_db.on(r'^SELECT DATABASE\(\)', lambda sql, p: [{
        'db': 'synthetic_db', 'host': 'synthetic-host',
        'account': 'synthetic_bcmd@localhost', 'roles': 'NONE', 'table_case': 0,
    }])
    fake_db.on(r'^SHOW GRANTS', lambda sql, p: [{'grant': grant} for grant in state['grants']])
    fake_db.on(r'^SELECT version,name,checksum', lambda sql, p: [
        {'version': v, 'name': name, 'checksum': checksum}
        for v, name, checksum, _ in readiness.discover()])

    def catalog(part):
        def read(sql, tables):
            return [({'_table': table} if 'AS _table' in sql else {}) | dict(row)
                    for table in tables for row in state['schema'][table][part]]
        return read

    for source, part in [('TABLES', 'table'), ('COLUMNS', 'columns'), ('STATISTICS', 'indexes'),
                         ('KEY_COLUMN_USAGE', 'fks'), ('REFERENTIAL_CONSTRAINTS', 'fk_rules'),
                         ('TABLE_CONSTRAINTS', 'checks')]:
        fake_db.on('FROM information_schema.' + source, catalog(part))
    fake_db.on(r'^SELECT IS_NULLABLE', lambda sql, p: [{'IS_NULLABLE': 'YES'}])
    fake_db.on(r'^SELECT EXISTS\(SELECT 1 FROM `lib_sex`\)', lambda sql, p: [
        {table: int(table in state['filled']) for table in REFERENCE_TABLES}])
    settings = replace(get_settings(), bcmd_user='synthetic_bcmd',
                       bcmd_expected_database='synthetic_db', bcmd_expected_hostname='synthetic-host',
                       bcmd_use_existing_account=False)
    return readiness, settings, state


def test_readiness_batches_metadata_without_dropping_checks(fake_db):
    readiness, settings, _ = readiness_fixture(fake_db)
    readiness.verify(fake_db, settings)
    assert len(fake_db.executed) == 13
    assert len(fake_db.queries('information_schema.')) == 7  # Six catalogs and the expiry probe.
    assert fake_db.commits == 0 and fake_db.rollbacks == 1


def test_schema_drift_is_rejected_on_next_checkout(fake_db):
    readiness, settings, state = readiness_fixture(fake_db)
    readiness.verify(fake_db, settings)
    state['schema']['bcmd_batch_member']['columns'][0]['COLUMN_TYPE'] = 'unexpected'
    with pytest.raises(ApiError) as exc:
        readiness.verify(fake_db, settings)
    assert exc.value.error_code == 'BCMD_NOT_READY'
    assert fake_db.commits == 0 and fake_db.rollbacks == 2


def test_grant_revocation_is_rejected_on_next_checkout(fake_db):
    readiness, settings, state = readiness_fixture(fake_db)
    readiness.verify(fake_db, settings)
    state['grants'] = state['grants'][:-1]
    with pytest.raises(ApiError) as exc:
        readiness.verify(fake_db, settings)
    assert exc.value.error_code == 'BCMD_NOT_READY'
    assert len(fake_db.queries('SHOW GRANTS')) == 2
    assert fake_db.commits == 0 and fake_db.rollbacks == 2


@pytest.mark.parametrize('part', ['table', 'columns', 'indexes', 'fks', 'fk_rules', 'checks'])
def test_missing_metadata_is_rejected(fake_db, part):
    readiness, settings, state = readiness_fixture(fake_db)
    assert state['schema']['bcmd_batch'][part]
    state['schema']['bcmd_batch'][part] = []
    with pytest.raises(ApiError) as exc:
        readiness.verify(fake_db, settings)
    assert exc.value.error_code == 'BCMD_NOT_READY'


def test_batched_metadata_preserves_complete_shapes(fake_db):
    from scripts.build_napa_dbgate_package import metadata_many
    _, _, state = readiness_fixture(fake_db)
    tables = ('bcmd_batch', 'bcmd_batch_member')
    assert metadata_many(fake_db.cursor(dictionary=True), tables) == {
        table: state['schema'][table] for table in tables}
    assert len(fake_db.executed) == 6


# --- Which readiness check failed is logged; account, role and grant text never are ---

def _not_ready(fake_db, settings):
    import bcmd_readiness as readiness
    with pytest.raises(ApiError) as exc:
        readiness.verify(fake_db, settings)
    assert exc.value.error_code == 'BCMD_NOT_READY'
    assert exc.value.detail == 'Batch service readiness checks did not pass'  # public text unchanged
    return readiness.failure_fields(exc.value)


def test_missing_migrations_are_named(fake_db):
    readiness, settings, _ = readiness_fixture(fake_db)
    applied = [m for m in readiness.discover() if m[0] <= '0022']
    fake_db.on(r'^SELECT version,name,checksum', lambda sql, p: [
        {'version': v, 'name': n, 'checksum': h} for v, n, h, _ in applied])
    fields = _not_ready(fake_db, settings)
    assert fields['readiness_check'] == 'migration_ledger'
    assert fields['missing'] == ['0023', '0024', '0025', '0026', '0027']
    assert fields['different'] == [] and fields['extra'] == [] and fields['latest_applied'] == '0022'


def test_staging_ledger_without_lookup_tables_names_0027(fake_db):
    # Staging today: 0001-0026 recorded. BCMD stays not ready until the 0027 package is applied.
    readiness, settings, _ = readiness_fixture(fake_db)
    fake_db.on(r'^SELECT version,name,checksum', lambda sql, p: [
        {'version': v, 'name': n, 'checksum': h} for v, n, h, _ in readiness.discover() if v <= '0026'])
    fields = _not_ready(fake_db, settings)
    assert fields == {'readiness_check': 'migration_ledger', 'missing': ['0027'], 'different': [],
                      'extra': [], 'latest_applied': '0026'}


def test_changed_and_unknown_migrations_are_named(fake_db):
    readiness, settings, _ = readiness_fixture(fake_db)
    rows = [{'version': v, 'name': n, 'checksum': h} for v, n, h, _ in readiness.discover()]
    rows[2]['checksum'] = '0' * 64
    rows.append({'version': '0099', 'name': '0099_future.sql', 'checksum': '1' * 64})
    fake_db.on(r'^SELECT version,name,checksum', lambda sql, p: rows)
    fields = _not_ready(fake_db, settings)
    assert fields['different'] == ['0003'] and fields['extra'] == ['0099'] and fields['missing'] == []


def test_wrong_db_server_names_hostname(fake_db):
    readiness, settings, _ = readiness_fixture(fake_db)
    settings = replace(settings, bcmd_expected_hostname='expected-host')
    fake_db.on(r'^SELECT DATABASE\(\)', lambda sql, p: [{
        'db': 'synthetic_db', 'host': 'synthetic-host', 'account': 'synthetic_bcmd@localhost',
        'roles': 'NONE', 'table_case': 0, 'server_id': 2213, 'read_only': 1}])
    fields = _not_ready(fake_db, settings)
    # The API's own DB target and the server's replication role, so a staff admin can tell a
    # replica or a wrong DB_HOST from a renamed primary without server access.
    assert fields == {'readiness_check': 'identity', 'problem': 'hostname',
                      'expected': 'expected-host', 'actual': 'synthetic-host', 'server_id': 2213,
                      'read_only': 1, 'target': f'{settings.ods.host}:{settings.ods.port}',
                      'via_tunnel': bool(settings.ssh_host)}
    assert 'SERVER_ID' in fake_db.executed[0][0].upper() and 'READ_ONLY' in fake_db.executed[0][0].upper()


def test_either_cluster_node_is_accepted_and_any_other_server_is_not(fake_db):
    readiness, settings, _ = readiness_fixture(fake_db)
    settings = replace(settings, bcmd_expected_hostname='ppisv2-v3Master,ppisv2-v3slaveG13')

    def answered_by(host):
        fake_db.on(r'^SELECT DATABASE\(\)', lambda sql, p: [{
            'db': 'synthetic_db', 'host': host, 'account': 'synthetic_bcmd@localhost',
            'roles': 'NONE', 'table_case': 0, 'server_id': 11302, 'read_only': 0}])
    for node in ('ppisv2-v3Master', 'ppisv2-v3slaveG13'):
        answered_by(node)
        readiness.verify(fake_db, settings)
    answered_by('ppisv2-v3slaveG14')
    fields = _not_ready(fake_db, settings)
    assert fields['readiness_check'] == 'identity' and fields['problem'] == 'hostname'
    assert fields['expected'] == 'ppisv2-v3Master,ppisv2-v3slaveG13' and fields['actual'] == 'ppisv2-v3slaveG14'


def test_wrong_account_or_roles_never_log_names(fake_db):
    readiness, settings, _ = readiness_fixture(fake_db)
    fields = _not_ready(fake_db, replace(settings, bcmd_user='someone_else'))
    assert fields == {'readiness_check': 'identity', 'problem': 'account'}
    fake_db.on(r'^SELECT DATABASE\(\)', lambda sql, p: [{
        'db': 'synthetic_db', 'host': 'synthetic-host', 'account': 'synthetic_bcmd@localhost',
        'roles': '`secret_role`@`%`', 'table_case': 0}])
    fields = _not_ready(fake_db, settings)
    assert fields == {'readiness_check': 'identity', 'problem': 'active_roles'}


def test_missing_grant_names_object_and_right(fake_db):
    readiness, settings, state = readiness_fixture(fake_db)
    state['grants'] = [grant for grant in state['grants'] if '`lib_ip_group`' not in grant]
    fields = _not_ready(fake_db, settings)
    assert fields['readiness_check'] == 'grants' and fields['problem'] == 'missing_rights'
    assert fields['missing'] == ['synthetic_db.lib_ip_group:SELECT'] and fields['missing_count'] == 1
    assert 'synthetic_bcmd' not in str(fields)


def test_unsupported_or_excess_grant_is_classified_without_grant_text():
    import bcmd_readiness as readiness
    with pytest.raises(ApiError) as exc:
        validate_grants(direct_grants() + ['GRANT `operator_role`@`%` TO `u`@`%`'], 'synthetic_db')
    assert readiness.failure_fields(exc.value) == {'readiness_check': 'grants', 'problem': 'unsupported_grant_form'}
    with pytest.raises(ApiError) as exc:
        validate_grants(direct_grants() + ['GRANT DELETE ON `synthetic_db`.`bcmd_batch` TO `u`@`%`'], 'synthetic_db')
    assert readiness.failure_fields(exc.value) == {'readiness_check': 'grants', 'problem': 'rights_beyond_allowed',
                                                   'object': 'synthetic_db.bcmd_batch', 'rights': ['DELETE']}
    with pytest.raises(ApiError) as exc:
        validate_existing_account_grants(['GRANT ALL PRIVILEGES ON *.* TO `u`@`%`',
                                          'REVOKE SELECT ON `synthetic_db`.* FROM `u`@`%`'], 'synthetic_db')
    assert readiness.failure_fields(exc.value) == {'readiness_check': 'grants', 'problem': 'partial_revoke'}


def test_existing_account_missing_rights_are_counted():
    import bcmd_readiness as readiness
    with pytest.raises(ApiError) as exc:
        validate_existing_account_grants(['GRANT SELECT ON `synthetic_db`.* TO `u`@`%`'], 'synthetic_db')
    fields = readiness.failure_fields(exc.value)
    assert fields['problem'] == 'missing_rights' and fields['missing_count'] > len(fields['missing']) == 10
    assert all(':' in item for item in fields['missing'])


def test_table_drift_names_table_and_part(fake_db):
    readiness, settings, state = readiness_fixture(fake_db)
    state['schema']['bcmd_batch_member']['columns'][0]['COLUMN_TYPE'] = 'unexpected'
    fields = _not_ready(fake_db, settings)
    assert fields == {'readiness_check': 'table_shape', 'table': 'bcmd_batch_member', 'parts': ['columns'],
                      'table_missing': False}


def test_lookup_tables_are_shape_checked(fake_db):
    readiness, settings, state = readiness_fixture(fake_db)
    state['schema']['lib_sex']['columns'][0]['COLUMN_TYPE'] = 'int'
    fields = _not_ready(fake_db, settings)
    assert fields == {'readiness_check': 'table_shape', 'table': 'lib_sex', 'parts': ['columns'],
                      'table_missing': False}


def test_empty_lookup_tables_are_named(fake_db):
    # 0027 applied but docs/schema/bcmd_lookup_copy.sql not yet run: every member would be blocked.
    readiness, settings, state = readiness_fixture(fake_db)
    state['filled'] = {'lib_sex'}
    fields = _not_ready(fake_db, settings)
    assert fields == {'readiness_check': 'reference_data', 'problem': 'empty_lookup',
                      'tables': ['lib_ext_name', 'lib_ip_group']}
    (sql, _), = fake_db.queries('EXISTS(')
    assert 'consultant_ods' not in sql and '.' not in sql.split('FROM', 1)[1]


def test_query_error_during_check_names_the_step(fake_db):
    import bcmd_readiness as readiness
    from mysql.connector import Error
    _, settings, _ = readiness_fixture(fake_db)
    fake_db.fail(Error('synthetic unknown column', errno=1054), match='FROM bcmd_dqa_result LIMIT 0')
    with pytest.raises(Error) as exc:
        readiness.verify(fake_db, settings)
    assert readiness.failure_fields(exc.value) == {'readiness_check': 'dqa_columns', 'errno': 1054}


def test_not_ready_is_logged_once_per_minute_per_cause(monkeypatch, caplog):
    import logging
    import bcmd_readiness as readiness
    monkeypatch.setattr(readiness, '_last_logged', (None, 0.0))
    with caplog.at_level(logging.WARNING, logger='tuloy.bcmd'):
        for _ in range(3):
            readiness.log_not_ready(readiness.unavailable('migration_ledger', missing=['0023']))
        readiness.log_not_ready(readiness.unavailable('identity', problem='hostname'))
    records = [r for r in caplog.records if r.getMessage() == 'bcmd_not_ready']
    assert [r.readiness_check for r in records] == ['migration_ledger', 'identity']
    assert records[0].missing == ['0023']


def test_bcmd_dependency_logs_the_failed_check(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    import bcmd_readiness as readiness
    import routers.bcmd as bcmd_router
    monkeypatch.setattr(readiness, '_last_logged', (None, 0.0))
    settings = replace(get_settings(), bcmd_enabled=True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(bcmd_settings=settings)))

    def connections(settings):
        yield object()
    monkeypatch.setattr(bcmd_router, 'get_bcmd_connection', connections)

    def failing(db, settings):
        raise readiness.unavailable('migration_ledger', missing=['0026'])
    monkeypatch.setattr(bcmd_router, 'verify', failing)
    with caplog.at_level(logging.WARNING, logger='tuloy.bcmd'):
        with pytest.raises(ApiError) as exc:
            next(bcmd_router.get_bcmd_db(request, principal=None))
    assert exc.value.error_code == 'BCMD_NOT_READY'
    rec = next(r for r in caplog.records if r.getMessage() == 'bcmd_not_ready')
    assert rec.readiness_check == 'migration_ledger' and rec.missing == ['0026']


def _not_ready_for(monkeypatch, principal):
    from types import SimpleNamespace
    import bcmd_readiness as readiness
    import routers.bcmd as bcmd_router
    monkeypatch.setattr(readiness, '_last_logged', (None, 0.0))
    settings = replace(get_settings(), bcmd_enabled=True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(bcmd_settings=settings)))

    def connections(settings):
        yield object()
    monkeypatch.setattr(bcmd_router, 'get_bcmd_connection', connections)

    def failing(db, settings):
        raise readiness.unavailable('identity', problem='hostname', expected='ppisv2-v3Master', actual='other-host')
    monkeypatch.setattr(bcmd_router, 'verify', failing)
    with pytest.raises(ApiError) as exc:
        next(bcmd_router.get_bcmd_db(request, principal=principal))
    assert exc.value.status_code == 503 and exc.value.error_code == 'BCMD_NOT_READY'
    assert exc.value.detail == 'Batch service readiness checks did not pass'
    return exc.value


def _staff(*scopes):
    from datetime import datetime, timedelta, timezone
    from security import Grant, Principal
    return Principal(7, 'synthetic staff', frozenset(Grant(s, 'DSWD', 'DSWD') for s in scopes),
                     datetime.now(timezone.utc) + timedelta(minutes=5), kind='staff')


def test_staff_admin_sees_which_readiness_check_failed(monkeypatch):
    from scopes import STAFF_ADMIN
    exc = _not_ready_for(monkeypatch, _staff(STAFF_ADMIN))
    assert exc.extra == {'readiness': {'readiness_check': 'identity', 'problem': 'hostname',
                                       'expected': 'ppisv2-v3Master', 'actual': 'other-host'}}


def test_other_bcmd_users_get_the_plain_503(monkeypatch):
    assert not _not_ready_for(monkeypatch, _staff()).extra
    assert not _not_ready_for(monkeypatch, None).extra


def test_error_body_adds_extra_without_overriding_core_fields():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from errors import install_error_handlers
    app = FastAPI()
    install_error_handlers(app)

    @app.get('/boom')
    def boom():
        raise ApiError(503, 'BCMD_NOT_READY', 'Batch service readiness checks did not pass',
                       extra={'readiness': {'readiness_check': 'grants'}, 'detail': 'spoofed', 'error_code': 'X'})
    body = TestClient(app).get('/boom').json()
    assert body['detail'] == 'Batch service readiness checks did not pass' and body['error_code'] == 'BCMD_NOT_READY'
    assert body['readiness'] == {'readiness_check': 'grants'}
