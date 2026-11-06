"""Integration tests against a throwaway LOCAL MySQL with synthetic data.

Skipped unless TEST_MYSQL_HOST is a loopback address. Never point these at a shared database:
they create stand-in views as tables, insert synthetic rows and truncate application tables.

    TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_PORT=3407 TEST_MYSQL_DATABASE=tuloy_test
    TEST_MYSQL_USER=<runtime> TEST_MYSQL_PASSWORD=...           (SELECT on the schema; INSERT on
        api_users, matching_transaction_CHED_tbl, enroll_matched_CHED_tbl - see docs/LOCAL_SETUP.md)
    TEST_MYSQL_MIGRATION_USER=<ddl> TEST_MYSQL_MIGRATION_PASSWORD=...
"""
import concurrent.futures
import dataclasses
import ipaddress
import os

import mysql.connector
import pytest

import database
from config import get_settings
from scopes import LEGACY_MEMBER_VERIFY, LEGACY_SCOPES
from scripts import migrate

HOST = os.getenv("TEST_MYSQL_HOST", "")


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


pytestmark = pytest.mark.skipif(not _loopback(HOST), reason="local test MySQL not configured")

STANDINS = [
    "DROP TABLE IF EXISTS vw_verify_member_bene",
    """CREATE TABLE vw_verify_member_bene (
        first_name varchar(100), middle_name varchar(100), last_name varchar(100), date_of_birth varchar(10),
        sex_id varchar(5), member_id int, listahanan_id varchar(50), lrn varchar(12), philsys varchar(20),
        grade_level varchar(5), m_maiden_name varchar(100), account_no varchar(30), bgy_psgc_code varchar(12),
        region_name varchar(50), province_name varchar(50), city_name varchar(50), barangay_name varchar(50),
        client_status varchar(5), roster_status varchar(20), tuloy_id varchar(50), match_keys binary(16))""",
    "DROP TABLE IF EXISTS vw_get_matched_CHED",
    "CREATE TABLE vw_get_matched_CHED AS SELECT e.*, '' first_name, '' last_name, '' middle_name, '' region_name,"
    " '' province_name, '' city_name, '' barangay_name FROM enroll_matched_CHED_tbl e WHERE 1=0",
]


def admin_conn():
    return mysql.connector.connect(host=HOST, port=int(os.environ["TEST_MYSQL_PORT"]),
                                   database=os.environ["TEST_MYSQL_DATABASE"],
                                   user=os.environ["TEST_MYSQL_MIGRATION_USER"],
                                   password=os.environ["TEST_MYSQL_MIGRATION_PASSWORD"], autocommit=True)


@pytest.fixture(scope="module")
def local_db():
    env = {"DB_HOST": HOST, "DB_PORT": os.environ["TEST_MYSQL_PORT"], "DB_DATABASE": os.environ["TEST_MYSQL_DATABASE"],
           "DB_USER": os.environ["TEST_MYSQL_USER"], "DB_PASSWORD": os.environ["TEST_MYSQL_PASSWORD"],
           "MIGRATION_DB_USER": os.environ["TEST_MYSQL_MIGRATION_USER"],
           "MIGRATION_DB_PASSWORD": os.environ["TEST_MYSQL_MIGRATION_PASSWORD"], "SSH_HOST": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        assert migrate.main(["apply", "--yes"]) == 0
        conn = admin_conn()
        cur = conn.cursor()
        for stmt in STANDINS:
            cur.execute(stmt)
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in ("enroll_matched_CHED_tbl", "matching_transaction_CHED_tbl", "api_user_scopes", "api_users"):
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        cur.execute(
            "INSERT INTO vw_verify_member_bene (first_name, last_name, date_of_birth, member_id, lrn, tuloy_id, match_keys)"
            " VALUES ('JUAN','DELA CRUZ','2000-01-01',1001,'100000000001','4ps-0001', UNHEX(MD5('JUAN/DELA CRUZ/2000-01-01'))),"
            " ('MARIA','SANTOS','2001-02-03',1002,NULL,'4ps-0002', UNHEX(MD5('MARIA/SANTOS/2001-02-03'))),"
            " ('MARIA','SANTOS','2001-02-03',1003,NULL,'4ps-0003', UNHEX(MD5('MARIA/SANTOS/2001-02-03')))")
        # GetEligibleBene reads vw_unmatched_member_bene (74904f5); same synthetic people.
        cur.execute("DROP TABLE IF EXISTS vw_unmatched_member_bene")
        cur.execute("CREATE TABLE vw_unmatched_member_bene AS SELECT * FROM vw_verify_member_bene")
        conn.close()
        settings = dataclasses.replace(get_settings(), ods=dataclasses.replace(
            get_settings().ods, host=HOST, port=int(env["DB_PORT"]), database=env["DB_DATABASE"],
            user=env["DB_USER"], password=env["DB_PASSWORD"]), ssh_host=None,
            rw_user=env["DB_USER"], rw_password=env["DB_PASSWORD"])
        database.close()
        yield settings
    finally:
        database.close()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def real_client(local_db, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    from fastapi_cache import FastAPICache
    from fastapi_cache.backends.inmemory import InMemoryBackend
    from utils import cache_key_builder
    import security

    monkeypatch.setattr(database, "get_settings", lambda: local_db)
    monkeypatch.setattr(database, "init", lambda settings=None: None)
    FastAPICache.init(InMemoryBackend(), prefix="it", expire=1, key_builder=cache_key_builder)
    conn = admin_conn()
    cur = conn.cursor()
    cur.execute("INSERT IGNORE INTO api_users (username, hashed_password) VALUES ('it-user', %s)",
                (security.get_password_hash("integration-password"),))
    for scope in sorted(LEGACY_SCOPES):
        cur.execute("INSERT IGNORE INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                    "SELECT id, %s, 'DSWD', 'DSWD', 'integration-test', CURRENT_TIMESTAMP + INTERVAL 1 DAY "
                    "FROM api_users WHERE username = 'it-user'", (scope,))
    conn.close()
    with TestClient(main.app, raise_server_exceptions=False) as c:
        token = c.post("/token", data={"username": "it-user", "password": "integration-password"}).json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_readonly_session_blocks_writes(local_db, monkeypatch):
    monkeypatch.setattr(database, "get_settings", lambda: local_db)
    conn = database._checkout("ro", local_db)
    try:
        cur = conn.cursor()
        with pytest.raises(mysql.connector.Error) as err:
            cur.execute("INSERT INTO matching_transaction_CHED_tbl (tuloy_id, status) VALUES ('x','y')")
        assert err.value.errno == 1792  # cannot execute statement in a READ ONLY transaction
    finally:
        database._release(conn)


def test_runtime_account_cannot_run_ddl(local_db, monkeypatch):
    monkeypatch.setattr(database, "get_settings", lambda: local_db)
    conn = database._checkout("rw", local_db)
    try:
        with pytest.raises(mysql.connector.Error) as err:
            conn.cursor().execute("CREATE TABLE should_not_exist (id int)")
        assert err.value.errno == 1142  # command denied
    finally:
        database._release(conn)


def test_verify_against_real_mysql(real_client):
    ok = real_client.post("/VerifyMemberBene", json={"first_name": " juan ", "last_name": "dela  cruz", "date_of_birth": "2000-01-01"})
    assert ok.json() == {"status": "Matched", "tuloy_id": "4ps-0001", "member_id": 1001}
    many = real_client.post("/VerifyMemberBene", json={"first_name": "Maria", "last_name": "Santos", "date_of_birth": "2001-02-03"})
    assert many.json()["status"] == "Multiple Match Found"
    narrowed = real_client.post("/VerifyMemberBene", json={"first_name": "Maria", "last_name": "Santos",
                                                           "date_of_birth": "2001-02-03", "member_id": "1003"})
    assert narrowed.json()["tuloy_id"] == "4ps-0003"
    injected = real_client.post("/VerifyMemberBene", json={"first_name": "x' OR '1'='1", "last_name": "y",
                                                           "date_of_birth": "2000-01-01", "lrn": "' OR 1=1 --"})
    assert injected.json()["status"] == "Not Found"


def test_ched_write_and_list(real_client):
    payload = {"status": True, "message": "FOUND", "data": {"id": "4ps-0001", "member_id": 1001,
               "degree": {"program_name": "BS Synthetic", "date_enrolled": "", "academic_year": "2026-2027"}}}
    assert real_client.post("/PostEnrolleeMatchedCHED", json=payload).status_code == 200
    eligible = real_client.get("/GetEligibleBene?limit=10").json()["items"]
    assert "4ps-0001" not in {r["tuloy_id"] for r in eligible}
    assert all("m_maiden_name" not in r and "account_no" not in r for r in eligible)
    assert all(sum(ch.isdigit() for ch in (r.get(k) or "")) <= 4 for r in eligible for k in ("lrn", "philsys"))


def test_concurrent_ched_writes_are_atomic_pairs(real_client):
    """Not idempotent today (no request key): N concurrent posts give N transaction rows, each
    paired with its own data row. Duplicate protection needs the Partner contract (backlog P2)."""
    payload = {"status": True, "message": "FOUND", "data": {"id": "4ps-0002", "member_id": 1002}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(lambda _: real_client.post("/PostEnrolleeMatchedCHED", json=payload).status_code, range(8)))
    assert codes == [200] * 8
    conn = admin_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT match_id) FROM enroll_matched_CHED_tbl WHERE tuloy_id = '4ps-0002'")
    rows, distinct_matches = cur.fetchone()
    conn.close()
    assert rows == distinct_matches == 8


def test_db_down_returns_503(local_db, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    down = dataclasses.replace(local_db, ods=dataclasses.replace(local_db.ods, port=1), db_connect_timeout=1)
    # The outage below starts the fail-fast cooldown; monkeypatch puts it back for later tests
    # (database.close is a no-op under the unit-test autouse fixture).
    monkeypatch.setattr(database, "_down_until", 0.0)
    database.close()
    monkeypatch.setattr(database, "get_settings", lambda: down)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.get("/readyz")
        assert r.status_code == 503
        r = c.post("/token", data={"username": "it-user", "password": "integration-password"})
        assert r.status_code == 503 and r.json()["error_code"] == "DB_UNAVAILABLE"
    database.close()


# ---- RBAC grants on real MySQL (migration 0002) ---------------------------------------------

def _user(cur, name):
    import security
    cur.execute("INSERT IGNORE INTO api_users (username, hashed_password) VALUES (%s, %s)",
                (name, security.get_password_hash("integration-password")))
    cur.execute("SELECT id FROM api_users WHERE username = %s", (name,))
    return cur.fetchone()[0]


def _grant(cur, uid, scope=LEGACY_MEMBER_VERIFY, partner="P-IT", agency="IT", expires="+ INTERVAL 1 DAY",
           granted="CURRENT_TIMESTAMP"):
    cur.execute(f"INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, granted_at, expires_at) "
                f"VALUES (%s, %s, %s, %s, 'integration-test', {granted}, CURRENT_TIMESTAMP {expires})",
                (uid, scope, partner, agency))
    return cur.lastrowid


def test_active_grant_filter_on_real_mysql(local_db):
    import security
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-grants")
    active = _grant(cur, uid)
    _grant(cur, uid, scope="tuloy:legacy:ched:read", granted="CURRENT_TIMESTAMP - INTERVAL 2 DAY",
           expires="- INTERVAL 1 DAY")                                    # expired
    revoked = _grant(cur, uid, scope="tuloy:legacy:ched:write")
    cur.execute("UPDATE api_user_scopes SET revoked_at = CURRENT_TIMESTAMP, revoked_by = 'it' WHERE grant_id = %s",
                (revoked,))
    conn.close()
    database.close()
    ro = database._checkout("ro", local_db)
    try:
        grants = security.fetch_grants(ro, uid)
    finally:
        database._release(ro)
    assert grants == {security.Grant(LEGACY_MEMBER_VERIFY, "P-IT", "IT")} and active


def test_one_active_grant_per_tuple_and_regrant_after_revoke(local_db):
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-unique")
    first = _grant(cur, uid)
    with pytest.raises(mysql.connector.Error) as err:
        _grant(cur, uid)
    assert err.value.errno == 1062                       # duplicate active tuple
    _grant(cur, uid, agency="OTHER")                     # same scope, other agency: separate grant
    cur.execute("UPDATE api_user_scopes SET revoked_at = CURRENT_TIMESTAMP, revoked_by = 'it' WHERE grant_id = %s",
                (first,))
    assert _grant(cur, uid) > first                      # regrant is a new row; history kept
    cur.execute("SELECT COUNT(*) FROM api_user_scopes WHERE user_id = %s", (uid,))
    assert cur.fetchone()[0] == 3
    conn.close()


def test_delimiter_tuples_do_not_collide(local_db):
    """('a|b', 'c') and ('a', 'b|c') are different grants (co-lead review finding)."""
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-delims")
    _grant(cur, uid, partner="a|b", agency="c")
    _grant(cur, uid, partner="a", agency="b|c")
    cur.execute("SELECT COUNT(*) FROM api_user_scopes WHERE user_id = %s AND revoked_at IS NULL", (uid,))
    assert cur.fetchone()[0] == 2
    conn.close()


@pytest.mark.parametrize("bad", [
    "INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
    "VALUES (%s, 'tuloy:legacy:member:verify', 'P', 'A', 'it', CURRENT_TIMESTAMP - INTERVAL 1 HOUR)",   # expiry in past
    "INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at, revoked_at) "
    "VALUES (%s, 'tuloy:legacy:member:verify', 'P', 'A', 'it', CURRENT_TIMESTAMP + INTERVAL 1 DAY, NOW())",  # no revoker
    "INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
    "VALUES (%s, 'tuloy:legacy:member:verify', '', 'A', 'it', CURRENT_TIMESTAMP + INTERVAL 1 DAY)",    # unbound
])
def test_grant_constraints_on_real_mysql(local_db, bad):
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-constraints")
    with pytest.raises(mysql.connector.Error) as err:
        cur.execute(bad, (uid,))
    assert err.value.errno == 3819                       # check constraint violated
    conn.close()


def test_runtime_account_cannot_change_grants(local_db):
    conn = mysql.connector.connect(host=HOST, port=int(os.environ["TEST_MYSQL_PORT"]),
                                   database=os.environ["TEST_MYSQL_DATABASE"], user=os.environ["TEST_MYSQL_USER"],
                                   password=os.environ["TEST_MYSQL_PASSWORD"], autocommit=True)
    cur = conn.cursor()
    for stmt in ("INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                 "VALUES (1, 'x', 'p', 'a', 'b', CURRENT_TIMESTAMP + INTERVAL 1 DAY)",
                 "UPDATE api_user_scopes SET revoked_at = NULL",
                 "DELETE FROM api_user_scopes"):
        with pytest.raises(mysql.connector.Error) as err:
            cur.execute(stmt)
        assert err.value.errno == 1142, stmt              # command denied
    conn.close()


def test_rbac_end_to_end_on_real_mysql(local_db, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-e2e")
    conn.close()
    monkeypatch.setattr(database, "get_settings", lambda: local_db)
    monkeypatch.setattr(database, "init", lambda settings=None: None)
    body = {"first_name": "Juan", "last_name": "Dela Cruz", "date_of_birth": "2000-01-01"}
    with TestClient(main.app, raise_server_exceptions=False) as c:
        token = c.post("/token", data={"username": "it-e2e", "password": "integration-password"}).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        assert c.post("/VerifyMemberBene", json=body, headers=h).status_code == 403
        conn = admin_conn()
        cur = conn.cursor()
        gid = _grant(cur, uid, partner="DSWD", agency="DSWD")                        # R8 is DSWD only (data-owner rulings)
        assert c.post("/VerifyMemberBene", json=body, headers=h).status_code == 200   # same token, new grant
        assert c.get("/GetEligibleBene?limit=1", headers=h).status_code == 403
        cur.execute("UPDATE api_user_scopes SET revoked_at = CURRENT_TIMESTAMP, revoked_by = 'it' WHERE grant_id = %s",
                    (gid,))
        conn.close()
        assert c.post("/VerifyMemberBene", json=body, headers=h).status_code == 403   # revoked: next request


# ---- Non-expiring grants (migration 0006) ----------------------------------------------------

def _grant_never(cur, uid, scope=LEGACY_MEMBER_VERIFY, partner="P-IT", agency="IT"):
    cur.execute("INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                "VALUES (%s, %s, %s, %s, 'integration-test', NULL)", (uid, scope, partner, agency))
    return cur.lastrowid


def test_non_expiring_grant_active_until_revoked_on_real_mysql(local_db):
    import security
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-never")
    gid = _grant_never(cur, uid)
    with pytest.raises(mysql.connector.Error) as err:      # a second active row for the tuple is still refused
        _grant_never(cur, uid)
    assert err.value.errno == 1062
    database.close()

    def read():
        ro = database._checkout("ro", local_db)
        try:
            return security.fetch_grants(ro, uid), security.fetch_grants_with_expiry(ro, uid)
        finally:
            database._release(ro)

    grants, with_expiry = read()
    assert grants == {security.Grant(LEGACY_MEMBER_VERIFY, "P-IT", "IT")} and with_expiry[0][1] is None
    cur.execute("UPDATE api_user_scopes SET revoked_at = CURRENT_TIMESTAMP, revoked_by = 'it' WHERE grant_id = %s", (gid,))
    assert read()[0] == frozenset()                          # revoked: gone on the next read
    conn.close()


def test_dated_grant_must_still_end_after_it_began(local_db):
    conn = admin_conn()
    cur = conn.cursor()
    uid = _user(cur, "it-dated")
    with pytest.raises(mysql.connector.Error) as err:
        _grant(cur, uid, expires="- INTERVAL 1 HOUR")
    assert err.value.errno == 3819
    conn.close()
