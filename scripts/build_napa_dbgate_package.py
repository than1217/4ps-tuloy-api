"""Build guarded DBGate files from migrations using isolated synthetic MySQL.

Never connects to staging. The supplied config must target the dedicated local
test container. This creates and removes only its own unique reference schema.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mysql.connector
from scripts.migrate import discover, statements

TARGET_DB = 'dswd_4ps_tuloy_dev'
TARGET_HOST = 'ppisv2-v3Master'
# From 0018 a migration may hold several statements. Each one runs only while its table still
# has the exact shape it had before that statement, so a retry after a crash resumes at the next.
STEPPED_FROM = '0018'
# 0017 (COR review tables) is outside this package: staging recorded it through the migration
# runner. It is still applied to the reference schema so the ledger chain stays complete.
NOT_PACKAGED = {'0017'}
STATEMENT_TARGET = re.compile(r'(CREATE TABLE(?: IF NOT EXISTS)?|ALTER TABLE|UPDATE)\s+`?([A-Za-z0-9_]+)`?', re.I)


def literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, int):
        return str(value)
    # Hex conversion preserves metadata containing backslashes independently of
    # NO_BACKSLASH_ESCAPES; no SQL-mode-dependent quoting is introduced.
    if '\\' in str(value):
        return 'CONVERT(0x'+str(value).encode('utf8').hex()+' USING utf8mb4)'
    return "'" + str(value).replace("'", "''") + "'"


def rows(cur, sql, args):
    cur.execute(sql, args)
    return cur.fetchall()


_METADATA_SQL = {
        'table': 'SELECT ENGINE,TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',
        'columns': '''SELECT COLUMN_NAME,ORDINAL_POSITION,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,
            EXTRA,CHARACTER_SET_NAME,COLLATION_NAME,GENERATION_EXPRESSION,COLUMN_COMMENT
            FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION''',
        'indexes': '''SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART,
            COLLATION,INDEX_TYPE,IS_VISIBLE,EXPRESSION FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX''',
        'fks': '''SELECT CONSTRAINT_NAME,COLUMN_NAME,ORDINAL_POSITION,POSITION_IN_UNIQUE_CONSTRAINT,
            REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY CONSTRAINT_NAME,ORDINAL_POSITION''',
        'fk_rules': '''SELECT CONSTRAINT_NAME,UNIQUE_CONSTRAINT_NAME,MATCH_OPTION,UPDATE_RULE,DELETE_RULE
            FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE()
            AND TABLE_NAME=%s ORDER BY CONSTRAINT_NAME''',
        'checks': '''SELECT tc.CONSTRAINT_NAME,cc.CHECK_CLAUSE,tc.ENFORCED
            FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.CHECK_CONSTRAINTS cc
            ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME
            WHERE tc.TABLE_SCHEMA=DATABASE() AND tc.TABLE_NAME=%s AND tc.CONSTRAINT_TYPE='CHECK'
            ORDER BY tc.CONSTRAINT_NAME''',
}


def metadata(cur, table):
    return {key: rows(cur, sql, (table,)) for key, sql in _METADATA_SQL.items()}


def metadata_many(cur, tables):
    """Read the same metadata for several tables in six queries; never cache it."""
    tables = tuple(tables)
    result = {table: {key: [] for key in _METADATA_SQL} for table in tables}
    if not tables:
        return result
    names = {table.casefold(): table for table in tables}
    marks = ','.join(['%s'] * len(tables))
    for key, sql in _METADATA_SQL.items():
        column = 'tc.TABLE_NAME' if key == 'checks' else 'TABLE_NAME'
        # Only fixed templates are rewritten; table names remain bound values.
        sql = sql.replace('SELECT ', f'SELECT {column} AS _table,', 1)
        sql = sql.replace('TABLE_NAME=%s', f'TABLE_NAME IN ({marks})')
        for row in rows(cur, sql, tables):
            table = names[row.pop('_table').casefold()]
            result[table][key].append(row)
    return result


def equal_rows(source, where, expected):
    tests = [f'(SELECT COUNT(*) FROM {source} WHERE {where})={len(expected)}']
    for row in expected:
        terms = []
        for field, value in row.items():
            lhs = field
            rhs = literal(value)
            if isinstance(value, str):
                lhs, rhs = f'BINARY {lhs}', f'BINARY {rhs}'
            terms.append(f'{lhs} <=> {rhs}')
        tests.append(f'(SELECT COUNT(*) FROM {source} WHERE {where} AND ' + ' AND '.join(terms) + ')=1')
    return '(' + '\n AND '.join(tests) + ')'


def shape(table, expected):
    where = f'TABLE_SCHEMA=DATABASE() AND TABLE_NAME={literal(table)}'
    checks = [equal_rows('information_schema.TABLES', where, expected['table']),
        equal_rows('information_schema.COLUMNS', where, expected['columns']),
        equal_rows('information_schema.STATISTICS', where, expected['indexes']),
        equal_rows('information_schema.KEY_COLUMN_USAGE', where+' AND REFERENCED_TABLE_NAME IS NOT NULL', expected['fks']),
        equal_rows('information_schema.REFERENTIAL_CONSTRAINTS',
                   f'CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME={literal(table)}', expected['fk_rules']),
        equal_rows('information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.CHECK_CONSTRAINTS cc '
                   'ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME',
                   f'tc.TABLE_SCHEMA=DATABASE() AND tc.TABLE_NAME={literal(table)} AND tc.CONSTRAINT_TYPE=\'CHECK\'',
                   [{('tc.'+k if k in ('CONSTRAINT_NAME','ENFORCED') else 'cc.'+k): v for k,v in row.items()} for row in expected['checks']])]
    if expected['fks']:
        checks.append(f"(SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE WHERE {where} AND REFERENCED_TABLE_NAME IS NOT NULL AND REFERENCED_TABLE_SCHEMA<>DATABASE())=0")
    return '(' + '\n AND '.join(checks) + ')'


def ledger_condition(files, versions):
    return equal_rows('schema_migrations', '1=1', [dict(version=v, name=files[v][0], checksum=files[v][1]) for v in versions])


def script(version, file, pre, post, need, prior, files):
    name, checksum, sql = file
    body = statements(sql)
    if len(body) != 1:
        raise ValueError('Each package step must contain exactly one DDL statement')
    recorded = f"(SELECT COUNT(*) FROM schema_migrations WHERE version={literal(version)} AND name={literal(name)} AND checksum={literal(checksum)})=1"
    return f'''-- Guarded DBGate step {version}: {name}
-- DDL is from that migration; text checksum {checksum}.
-- Run this WHOLE file in a clean autocommit session. Stop on any SQL error or STOP result.
-- This file changes schema and INSERTs one ledger row only after matching postflight.
-- No existing ledger row is updated or deleted. AUTO_INCREMENT counters and row data are not reset.
SET @napa_pre_ok=0, @napa_post_ok=0, @napa_locked=0, @napa_need_ddl=0;
SET @napa_target_ok=(DATABASE()={literal(TARGET_DB)} AND @@hostname={literal(TARGET_HOST)} AND @@autocommit=1);
SET @napa_locked=IF(@napa_target_ok, GET_LOCK('tuloy_schema_migrate',0),0);
SET SESSION lock_wait_timeout=10;
SET SESSION max_execution_time=5000;
SET @napa_pre_ok=(@napa_target_ok AND @napa_locked=1 AND {ledger_condition(files, prior)} AND {pre});
SET @napa_need_ddl=({need});
SET @napa_sql=IF(@napa_pre_ok AND @napa_need_ddl,
 {literal(body[0])}, {literal("SELECT 'No DDL executed - inspect final step_status' AS ddl_note")});
PREPARE napa_step FROM @napa_sql;
EXECUTE napa_step;
DEALLOCATE PREPARE napa_step;
SET @napa_post_ok=({post});
INSERT INTO schema_migrations(version,name,checksum)
SELECT {literal(version)},{literal(name)},{literal(checksum)}
WHERE @napa_pre_ok AND @napa_post_ok AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version={literal(version)});
SELECT {literal(version)} AS version,
 CASE WHEN @napa_pre_ok AND @napa_post_ok AND {recorded} THEN 'VERIFIED'
      WHEN @napa_target_ok AND @napa_locked=1 AND @napa_post_ok AND {recorded} THEN 'ALREADY_RECORDED'
      ELSE 'STOP' END AS step_status,
 @napa_target_ok AS target_ok,@napa_pre_ok AS preflight_ok,@napa_post_ok AS postflight_ok;
SELECT IF(@napa_locked=1,RELEASE_LOCK('tuloy_schema_migrate'),0) AS migration_lock_released;
'''


def statement_target(statement):
    match = STATEMENT_TARGET.match(statement)
    if not match:
        raise ValueError('Unsupported package statement: ' + statement[:60])
    return match[1].split()[0].upper(), match[2]


def absent(table):
    return f'(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME={literal(table)})=0'


def stepped_script(version, file, steps, prior, files):
    """One guarded file for a migration with several statements.

    steps holds (kind, table, statement, before, after); before is None for a CREATE.
    Preflight accepts each table in any state of its sequence; postflight requires the last.
    """
    name, checksum, sql = file
    states = {}
    for kind, table, statement, before, after in steps:
        sequence = states.setdefault(table, [before])
        if after != sequence[-1]:
            sequence.append(after)
    def state(table, meta):
        return absent(table) if meta is None else shape(table, meta)
    pre = '(' + '\n AND '.join('(' + '\n OR '.join(state(t, m) for m in seq) + ')' for t, seq in states.items()) + ')'
    post = '(' + '\n AND '.join(shape(t, seq[-1]) for t, seq in states.items()) + ')'
    recorded = f"(SELECT COUNT(*) FROM schema_migrations WHERE version={literal(version)} AND name={literal(name)} AND checksum={literal(checksum)})=1"
    parts = []
    for number, (kind, table, statement, before, after) in enumerate(steps, 1):
        note = ' (changes rows; runs only before the next schema change exists)' if kind == 'UPDATE' else ''
        skipped = f"SELECT 'Statement {number} not executed - inspect final step_status' AS ddl_note"
        parts.append(f'''-- Statement {number} of {len(steps)}: {kind} {table}{note}
SET @napa_need_ddl=({state(table, before)});
SET @napa_sql=IF(@napa_pre_ok AND @napa_need_ddl,
 {literal(statement)}, {literal(skipped)});
PREPARE napa_step FROM @napa_sql;
EXECUTE napa_step;
DEALLOCATE PREPARE napa_step;''')
    body = '\n'.join(parts)
    return f'''-- Guarded DBGate step {version}: {name}
-- {len(steps)} statement(s) from that migration; text checksum {checksum}.
-- Run this WHOLE file in a clean autocommit session. Stop on any SQL error or STOP result.
-- Each statement runs only while its table has the exact shape from before that statement,
-- so a retry after an interrupted run continues with the next statement.
-- This file INSERTs one ledger row only after matching postflight.
-- No existing ledger row is updated or deleted. AUTO_INCREMENT counters are not reset.
SET @napa_pre_ok=0, @napa_post_ok=0, @napa_locked=0, @napa_need_ddl=0;
SET @napa_target_ok=(DATABASE()={literal(TARGET_DB)} AND @@hostname={literal(TARGET_HOST)} AND @@autocommit=1);
SET @napa_locked=IF(@napa_target_ok, GET_LOCK('tuloy_schema_migrate',0),0);
SET SESSION lock_wait_timeout=10;
SET SESSION max_execution_time=5000;
SET @napa_pre_ok=(@napa_target_ok AND @napa_locked=1 AND {ledger_condition(files, prior)} AND {pre});
{body}
SET @napa_post_ok=({post});
INSERT INTO schema_migrations(version,name,checksum)
SELECT {literal(version)},{literal(name)},{literal(checksum)}
WHERE @napa_pre_ok AND @napa_post_ok AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version={literal(version)});
SELECT {literal(version)} AS version,
 CASE WHEN @napa_pre_ok AND @napa_post_ok AND {recorded} THEN 'VERIFIED'
      WHEN @napa_target_ok AND @napa_locked=1 AND @napa_post_ok AND {recorded} THEN 'ALREADY_RECORDED'
      ELSE 'STOP' END AS step_status,
 @napa_target_ok AS target_ok,@napa_pre_ok AS preflight_ok,@napa_post_ok AS postflight_ok;
SELECT IF(@napa_locked=1,RELEASE_LOCK('tuloy_schema_migrate'),0) AS migration_lock_released;
'''


def build(config_file, output):
    config = json.loads(config_file.read_text(encoding='utf8'))
    if config.get('host') != '127.0.0.1' or config.get('port') != 13498:
        raise ValueError('Only the dedicated local synthetic test container is allowed')
    args = {key: config[key] for key in ('host','port','user','password')}
    db = mysql.connector.connect(**args, autocommit=True)
    cur = db.cursor(dictionary=True)
    reference = 'tuloy_napa_test_' + uuid4().hex[:12]
    files = {v:(n,c,s) for v,n,c,s in discover()}
    snapshots = {}
    creation_shapes = {}
    alterations = {}
    try:
        cur.execute(f'CREATE DATABASE `{reference}`')
        cur.execute(f'USE `{reference}`')
        stepped = {}
        for version, (name, checksum, sql) in files.items():
            if version in NOT_PACKAGED:
                for statement in statements(sql):
                    cur.execute(statement)
                continue
            if version >= STEPPED_FROM:
                steps = []
                for statement in statements(sql):
                    kind, table = statement_target(statement)
                    before = None if kind == 'CREATE' else metadata(cur, table)
                    cur.execute(statement)
                    steps.append((kind, table, statement, before, metadata(cur, table)))
                    snapshots[table] = steps[-1][4]
                stepped[version] = steps
                continue
            altered = re.search(r'ALTER TABLE `([^`]+)`', sql)
            before = metadata(cur, altered[1]) if version >= '0014' and altered else None
            for statement in statements(sql):
                cur.execute(statement)
            if version == '0002':
                snapshots['expiry_before'] = metadata(cur,'api_user_scopes')
            elif version == '0006':
                snapshots['expiry_after'] = metadata(cur,'api_user_scopes')
            elif version >= '0007':
                if before is not None:
                    table = altered[1]
                    after = metadata(cur, table)
                    alterations[version] = (table, before, after)
                    snapshots[table] = after
                else:
                    table = re.search(r'CREATE TABLE IF NOT EXISTS `([^`]+)`', sql).group(1)
                    snapshots[table] = metadata(cur,table)
                    creation_shapes[table] = snapshots[table]
        output.mkdir(parents=True, exist_ok=True)
        prior = ['0001','0002','0004','0005']
        for version in ['0003', *[v for v in files if v >= '0006']]:
            if version in NOT_PACKAGED or version in stepped:
                if version in stepped:
                    (output/files[version][0]).write_text(
                        stepped_script(version, files[version], stepped[version], prior, files), encoding='utf8')
                prior = sorted([*prior, version])
                continue
            if version == '0003':
                where = "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='enroll_matched_CHED_tbl' AND INDEX_NAME='idx_enroll_matched_tuloy_id'"
                post = equal_rows('information_schema.STATISTICS', where, [dict(INDEX_NAME='idx_enroll_matched_tuloy_id', NON_UNIQUE=1, SEQ_IN_INDEX=1, COLUMN_NAME='tuloy_id', SUB_PART=None, COLLATION='A', INDEX_TYPE='BTREE', IS_VISIBLE='YES', EXPRESSION=None)])
                need = f'(SELECT COUNT(*) FROM information_schema.STATISTICS WHERE {where})=0'
                pre = f'({need} OR {post})'
            elif version == '0006':
                def expiry(which):
                    meta = snapshots[which]
                    cols = [row for row in meta['columns'] if row['COLUMN_NAME']=='expires_at']
                    checks = [row for row in meta['checks'] if row['CONSTRAINT_NAME']=='ck_api_user_scopes_expiry']
                    return '(' + equal_rows('information_schema.COLUMNS', "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='api_user_scopes' AND COLUMN_NAME='expires_at'", cols) + ' AND ' + equal_rows('information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.CHECK_CONSTRAINTS cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME', "tc.TABLE_SCHEMA=DATABASE() AND tc.TABLE_NAME='api_user_scopes' AND tc.CONSTRAINT_NAME='ck_api_user_scopes_expiry'", [{('tc.'+k if k in ('CONSTRAINT_NAME','ENFORCED') else 'cc.'+k):v for k,v in row.items()} for row in checks]) + ')'
                need = expiry('expiry_before')
                post = expiry('expiry_after')
                pre = f'({need} OR {post})'
            elif version in alterations:
                table, before, after = alterations[version]
                need = shape(table, before)
                post = shape(table, after)
                pre = f'({need} OR {post})'
            else:
                table = re.search(r'CREATE TABLE IF NOT EXISTS `([^`]+)`', files[version][2]).group(1)
                post = shape(table,creation_shapes[table])
                need = f'(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME={literal(table)})=0'
                pre = post if version=='0007' else f'({need} OR {post})'
            (output/files[version][0]).write_text(script(version, files[version], pre, post, need, prior, files), encoding='utf8')
            prior = sorted([*prior,version])
        (output/'expected_schema.json').write_text(json.dumps(snapshots,indent=2,default=str),encoding='utf8')
        (output/'package_hashes.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.glob('*.sql'))},indent=2),encoding='utf8')
    finally:
        assert reference.startswith('tuloy_napa_test_') and len(reference)==len('tuloy_napa_test_')+12
        cur.execute(f'DROP DATABASE IF EXISTS `{reference}`')
        cur.close()
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-config', type=Path, default=Path('.run/napa-test.json'))
    parser.add_argument('--output', type=Path, default=Path('docs/schema/dbgate-apply'))
    options = parser.parse_args()
    build(options.test_config,options.output)
    print(f'Prepared guarded DBGate package: {options.output}; no staging connection was made')
