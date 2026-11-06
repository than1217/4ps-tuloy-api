"""docs/schema/bcmd_lookup_copy.sql on real MySQL, in the dedicated synthetic container, opt-in only."""
import json
import os
from pathlib import Path
from uuid import uuid4

import mysql.connector
import pytest

from scripts import migrate

COPY = Path('docs/schema/bcmd_lookup_copy.sql')
LOOKUPS = {'lib_sex': ('lib_sex_v3', 'sex_id'), 'lib_ext_name': ('lib_ext_name_v3', 'ext_name_id'),
           'lib_ip_group': ('lib_ip_group_v3', 'ip_group_id')}
# PPIS v3 shape (docs/napa-source-probe.json, external_lookup_metadata), audit columns included.
AUDIT = ('dt_inserted datetime,inserted_by int unsigned,dt_updated datetime,last_updated_by int unsigned,'
         'archived tinyint unsigned,dt_archived datetime,last_archived_by int unsigned,dt_restored datetime,'
         'last_restored_by int unsigned,dt_last_modified datetime')
SOURCE_DDL = (
    f'CREATE TABLE lib_sex_v3 (sex_id int unsigned PRIMARY KEY,name varchar(50),code varchar(15),{AUDIT}) CHARSET=utf8mb3',
    f'CREATE TABLE lib_ext_name_v3 (ext_name_id int PRIMARY KEY,name varchar(50),{AUDIT}) CHARSET=latin1',
    f'CREATE TABLE lib_ip_group_v3 (ip_group_id int unsigned PRIMARY KEY,name varchar(50),code varchar(15),'
    f'status int unsigned,{AUDIT})',
)


def _unique():
    name = 'tuloy_napa_test_' + uuid4().hex[:12]
    assert name.startswith('tuloy_napa_test_') and len(name) == len('tuloy_napa_test_') + 12
    return name


@pytest.fixture
def copy_env():
    location = os.getenv('NAPA_TEST_CONFIG')
    if not location:
        pytest.skip('Set NAPA_TEST_CONFIG to the isolated synthetic container configuration')
    config = json.loads(Path(location).read_text(encoding='utf8'))
    assert config['host'] == '127.0.0.1' and config['port'] == 13498
    target, source = _unique(), _unique()
    db = mysql.connector.connect(**{k: config[k] for k in ('host', 'port', 'user', 'password')}, autocommit=True)
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(f'CREATE DATABASE `{source}`')
        cur.execute(f'USE `{source}`')
        for ddl in SOURCE_DDL:
            cur.execute(ddl)
        cur.execute("INSERT INTO lib_sex_v3 (sex_id,name,code,archived,dt_inserted) VALUES "
                    "(1,'MALE','M',0,'2020-01-01'),(2,'FEMALE','F',0,NULL),(3,'NONE','NA',0,NULL)")
        cur.execute("INSERT INTO lib_ext_name_v3 (ext_name_id,name,archived) VALUES (1,'Sr.',0),(2,'Jr.',0),(18,'JRA',1)")
        cur.execute("INSERT INTO lib_ip_group_v3 (ip_group_id,name,code,status,archived) VALUES "
                    "(1,'AETA/AYTA-ABELLING/ABELLEN (SUB-GROUP)',NULL,1,0),(7,'Ñ synthetic',NULL,1,0),(9,NULL,'X',0,NULL)")
        cur.execute(f'CREATE DATABASE `{target}`')
        cur.execute(f'USE `{target}`')
        cur.execute('SELECT @@hostname AS host')
        hostname = cur.fetchone()['host']
        migrate.ensure_ledger(cur)
        yield {'cur': cur, 'target': target, 'source': source, 'hostname': hostname}
    finally:
        cur.execute(f'DROP DATABASE IF EXISTS `{target}`')
        cur.execute(f'DROP DATABASE IF EXISTS `{source}`')
        cur.close()
        db.close()


def _run(env, *, target=None, source=None):
    sql = COPY.read_text(encoding='utf8')
    sql = sql.replace("'dswd_4ps_tuloy_dev'", f"'{target or env['target']}'")
    sql = sql.replace("'ppisv2-v3Master'", f"'{env['hostname']}'")
    sql = sql.replace('`consultant_ods`', f"`{source or env['source']}`")
    cur, outcome = env['cur'], None
    for statement in migrate.statements(sql):
        cur.execute(statement)
        if cur.with_rows:
            rows = cur.fetchall()
            outcome = next((row for row in rows if 'copy_status' in row), outcome)
    return outcome['copy_status']


def _apply_0027(env):
    (version, name, checksum, sql), = [m for m in migrate.discover() if m[0] == '0027']
    for statement in migrate.statements(sql):
        env['cur'].execute(statement)
    env['cur'].execute('INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)', (version, name, checksum))


def _rows(cur, table, schema=None):
    columns = {'lib_sex': 'sex_id,name,code,archived', 'lib_ext_name': 'ext_name_id,name,archived',
               'lib_ip_group': 'ip_group_id,name,code,status,archived'}[table]
    source, key = LOOKUPS[table]
    cur.execute(f'SELECT {columns} FROM ' + (f'`{schema}`.`{source}`' if schema else f'`{table}`') + f' ORDER BY {key}')
    return cur.fetchall()


def _checksums(cur, schema):
    cur.execute(f"CHECKSUM TABLE `{schema}`.lib_sex_v3, `{schema}`.lib_ext_name_v3, `{schema}`.lib_ip_group_v3")
    return cur.fetchall()


def test_copy_needs_0027_and_the_staging_target(copy_env):
    cur = copy_env['cur']
    # Before 0027 is recorded nothing is written (the tables do not exist yet either).
    cur.execute('CREATE TABLE lib_sex (sex_id int unsigned PRIMARY KEY,name varchar(50),code varchar(15),archived tinyint unsigned)')
    cur.execute('CREATE TABLE lib_ext_name (ext_name_id int PRIMARY KEY,name varchar(50),archived tinyint unsigned)')
    cur.execute('CREATE TABLE lib_ip_group (ip_group_id int unsigned PRIMARY KEY,name varchar(50),code varchar(15),status int unsigned,archived tinyint unsigned)')
    assert _run(copy_env) == 'STOP'
    for table in LOOKUPS:
        assert _rows(cur, table) == []
    cur.execute('DROP TABLE lib_sex, lib_ext_name, lib_ip_group')
    _apply_0027(copy_env)
    # Any other database (or server) than the configured target: nothing is written.
    assert _run(copy_env, target='dswd_4ps_tuloy_elsewhere') == 'STOP'
    for table in LOOKUPS:
        assert _rows(cur, table) == []


def test_copy_makes_exact_copies_without_changing_the_source(copy_env):
    cur, source = copy_env['cur'], copy_env['source']
    _apply_0027(copy_env)
    before = _checksums(cur, source)
    assert _run(copy_env) == 'COPIED'
    for table in LOOKUPS:
        assert _rows(cur, table) == _rows(cur, table, source), table
    cur.execute("SELECT name FROM lib_ip_group WHERE ip_group_id=7")
    assert cur.fetchone()['name'] == 'Ñ synthetic'
    # Re-running is a no-op; then a renamed, an archived and a new source row are picked up.
    assert _run(copy_env) == 'COPIED'
    cur.execute(f"UPDATE `{source}`.lib_ext_name_v3 SET name='Jr' WHERE ext_name_id=2")
    cur.execute(f"UPDATE `{source}`.lib_sex_v3 SET archived=1 WHERE sex_id=3")
    cur.execute(f"INSERT INTO `{source}`.lib_ip_group_v3 (ip_group_id,name,status,archived) VALUES (200,'New group',1,0)")
    changed = _checksums(cur, source)
    assert _run(copy_env) == 'COPIED'
    for table in LOOKUPS:
        assert _rows(cur, table) == _rows(cur, table, source), table
    assert _checksums(cur, source) == changed != before


def test_a_lookup_removed_from_the_source_stops_instead_of_deleting(copy_env):
    cur, source = copy_env['cur'], copy_env['source']
    _apply_0027(copy_env)
    assert _run(copy_env) == 'COPIED'
    cur.execute(f'DELETE FROM `{source}`.lib_ext_name_v3 WHERE ext_name_id=18')
    assert _run(copy_env) == 'STOP'
    cur.execute('SELECT COUNT(*) AS n FROM lib_ext_name WHERE ext_name_id=18')
    assert cur.fetchone()['n'] == 1
