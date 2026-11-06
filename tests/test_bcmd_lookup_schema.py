"""BCMD reference lookups live in the application database (0027); the API reads no other schema."""
import json
import re
from pathlib import Path

import pytest

import bcmd_readiness as readiness
import bcmd_store
from errors import ApiError
from scripts.migrate import discover, statements
from scripts.napa_runtime_grants import render

ROOT = Path(__file__).resolve().parents[1]
APP_DB = 'dswd_4ps_tuloy_dev'
LOOKUPS = ('lib_sex', 'lib_ext_name', 'lib_ip_group')
SOURCES = {'lib_sex': 'lib_sex_v3', 'lib_ext_name': 'lib_ext_name_v3', 'lib_ip_group': 'lib_ip_group_v3'}
COPY_SQL = (ROOT / 'docs/schema/bcmd_lookup_copy.sql').read_text(encoding='utf8')
# The batch source query reads these lookup columns (bcmd_store.SOURCE_SQL, then napa_mapping).
QUERY_COLUMNS = {'lib_sex': {'sex_id', 'name', 'archived'}, 'lib_ext_name': {'ext_name_id', 'name', 'archived'},
                 'lib_ip_group': {'ip_group_id', 'name', 'archived'}}


def _source_sql():
    sql, _ = bcmd_store.source_sql([('CHED', {'order': 1, 'course_type': 'degree', 'academic_year': 'NULL',
                                              'semester': 'NULL', 'from': 'enroll_matched_CHED_tbl m'},
                                     ['m.id IN (%s)'], [1])])
    return sql


def _expected_columns(table):
    schema = json.loads((ROOT / 'docs/schema/dbgate-apply/expected_schema.json').read_text(encoding='utf8'))
    return [c['COLUMN_NAME'] for c in sorted(schema[table]['columns'], key=lambda c: c['ORDINAL_POSITION'])]


def test_batch_query_joins_the_lookups_in_its_own_database():
    sql = _source_sql()
    assert 'consultant_ods' not in sql
    for table in LOOKUPS:
        assert re.search(rf'LEFT JOIN {table} \w ON', sql), table
    # Every table in the query is unqualified, so it resolves in the connection's configured database.
    assert not re.search(r'\b(?:FROM|JOIN)\s+`?\w+`?\.', sql)


def test_api_code_never_names_another_schema():
    # scripts/napa_source_probe.py is the read-only 10-29 evidence probe, run by hand, never by the API.
    offenders = [str(path.relative_to(ROOT)) for path in [*ROOT.glob('*.py'), *ROOT.glob('routers/*.py'),
                                                          *ROOT.glob('scripts/*.py')]
                 if path.name != 'napa_source_probe.py' and 'consultant_ods' in path.read_text(encoding='utf8')]
    assert offenders == []


def test_readiness_requires_select_on_the_app_db_lookups_only():
    with pytest.raises(ApiError) as exc:
        readiness.validate_existing_account_grants(['GRANT ALL PRIVILEGES ON `other_db`.* TO `u`@`%`'], APP_DB)
    missing = readiness.failure_fields(exc.value)['missing']
    assert all(item.startswith(APP_DB + '.') for item in missing)
    # One right on the application database covers the lookups; nothing in consultant_ods is needed.
    readiness.validate_existing_account_grants([f'GRANT SELECT, INSERT, UPDATE ON `{APP_DB}`.* TO `u`@`%`'], APP_DB)
    with pytest.raises(ApiError) as exc:
        readiness.validate_existing_account_grants(
            [f'GRANT SELECT, INSERT, UPDATE ON `{APP_DB}`.`{t}` TO `u`@`%`' for t in readiness.READ_TABLES
             if t != 'lib_sex'], APP_DB)
    assert readiness.failure_fields(exc.value)['missing'] == [f'{APP_DB}.lib_sex:SELECT']


def test_least_privilege_mode_rejects_consultant_ods_rights_and_grants_lookups_read_only():
    exact = [f"GRANT SELECT{', INSERT' if t in readiness.WRITE_TABLES or t == 'bcmd_batch_export' else ''}"
             f"{', UPDATE' if t in readiness.UPDATE_TABLES else ''} ON `{APP_DB}`.`{t}` TO `u`@`%`"
             for t in readiness.READ_TABLES]
    readiness.validate_grants(exact, APP_DB)
    with pytest.raises(ApiError) as exc:
        readiness.validate_grants(exact + ['GRANT SELECT ON `consultant_ods`.`lib_sex_v3` TO `u`@`%`'], APP_DB)
    assert readiness.failure_fields(exc.value)['problem'] == 'rights_beyond_allowed'
    with pytest.raises(ApiError):
        readiness.validate_grants(exact + [f'GRANT INSERT ON `{APP_DB}`.`lib_sex` TO `u`@`%`'], APP_DB)
    script = render('tuloy_bcmd_runtime', '10.0.0.%', APP_DB, 'ppisv2-v3Master')
    assert 'consultant_ods' not in script
    for table in LOOKUPS:
        assert f'GRANT SELECT ON `{APP_DB}`.`{table}` TO' in script


def test_migration_0027_creates_only_the_three_lookups_with_what_the_query_reads():
    (version, name, _, sql), = [m for m in discover() if m[0] == '0027']
    assert name == '0027_bcmd_lookup_tables.sql'
    created = [re.match(r'CREATE TABLE `(\w+)`', s)[1] for s in statements(sql)]
    assert created == list(LOOKUPS) and len(statements(sql)) == 3
    for table in LOOKUPS:
        assert QUERY_COLUMNS[table] <= set(_expected_columns(table)), table
    assert (ROOT / 'migrations/rollback/0027_bcmd_lookup_tables_down.sql').exists()


def test_copy_script_writes_only_the_lookups_and_copies_every_column():
    body = [s for s in statements(COPY_SQL)]
    writes = re.findall(r"INSERT INTO `(\w+)` \(([^)]*)\) SELECT \* FROM \(SELECT ([^)]*?) FROM `consultant_ods`\.`(\w+)`\)",
                        COPY_SQL)
    assert [w[0] for w in writes] == list(LOOKUPS)
    for table, insert_cols, select_cols, source in writes:
        assert source == SOURCES[table]
        assert insert_cols.replace('`', '').split(',') == _expected_columns(table) == select_cols.replace('`', '').split(',')
    # Nothing else is changed, here or in consultant_ods.
    assert COPY_SQL.count('ON DUPLICATE KEY UPDATE') == 3
    assert not re.search(r'\b(DELETE|DROP|TRUNCATE|ALTER|GRANT|REVOKE|RENAME|CREATE|REPLACE|UPDATE)\b',
                         COPY_SQL.replace('ON DUPLICATE KEY UPDATE', ''))
    assert all(not s.upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for s in body)  # only via guarded PREPARE
    assert ("DATABASE()='dswd_4ps_tuloy_dev' AND @@hostname IN ('ppisv2-v3Master','ppisv2-v3slaveG13')"
            in COPY_SQL)
    assert "version='0027'" in COPY_SQL and 'COPIED' in COPY_SQL
