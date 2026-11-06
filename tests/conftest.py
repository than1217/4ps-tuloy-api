"""Unit-test setup: synthetic settings, an in-memory fake database, no network.

Environment is set before any app module is imported so .env values never apply.
"""
import os

SYNTHETIC_ENV = {
    "TULOY_ENV_FILE": "",  # never read the developer .env in tests
    "APP_ENV": "local",
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "1",  # nothing listens here; unit tests replace the DB dependencies
    "DB_DATABASE": "synthetic_db",
    "DB_USER": "synthetic_user",
    "DB_PASSWORD": "synthetic-password",
    "SSH_HOST": "",
    "SECRET_KEY": "unit-test-secret-key-0123456789abcdef-xyz",
    "PARTNER_API_ENABLED": "true",
    "ALLOW_SIGNUP": "false",
    "STAFF_AUTH_MODE": "legacy",
    "STAFF_FULL_ACCESS_ENABLED": "false",
    "KEYCLOAK_ISSUER": "",
    "KEYCLOAK_API_AUDIENCE": "",
    "REDIS_URL": "",
    "RATE_LIMIT_PER_MINUTE": "1000",
    "TRUSTED_HOSTS": "testserver,127.0.0.1,localhost",
    "DB_CONNECT_TIMEOUT": "1",
    "BCMD_ENABLED": "false",
    "BCMD_DB_USER": "",
    "BCMD_DB_PASSWORD": "",
    "BCMD_EXPECT_DATABASE": "",
    "BCMD_EXPECT_HOSTNAME": "",
    "BCMD_USE_EXISTING_ACCOUNT": "false",
}
os.environ.update(SYNTHETIC_ENV)

import re  # noqa: E402
from datetime import datetime  # noqa: E402

import bcrypt  # noqa: E402
import pytest  # noqa: E402
from fastapi_cache import FastAPICache  # noqa: E402
from fastapi_cache.backends.inmemory import InMemoryBackend  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mysql.connector import Error  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
from ratelimit import login_limiter  # noqa: E402
from scopes import LEGACY_SCOPES  # noqa: E402
from security import create_access_token  # noqa: E402
from utils import cache_key_builder  # noqa: E402

GRANT_EXPIRY = datetime(2099, 1, 2, 3, 4, 5)  # naive, as MySQL TIMESTAMP columns arrive
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


class FakeCursor:
    def __init__(self, db, dictionary=False):
        self.db = db
        self.dictionary = dictionary
        self._rows = []
        self.lastrowid = None

    def execute(self, sql, params=()):
        self.db.executed.append((sql, tuple(params or ())))
        if self.db.fail_with is not None and self.db.fail_match in sql:
            raise self.db.fail_with
        for pattern, handler in self.db.handlers:
            if re.search(pattern, sql, re.S):
                rows = handler(sql, tuple(params or ()))
                self._rows = list(rows or [])
                break
        else:
            self._rows = []
        if sql.lstrip().upper().startswith("INSERT"):
            self.db.next_id += 1
            self.lastrowid = self.db.next_id

    def fetchall(self):
        rows = self._rows if self.dictionary else [tuple(r.values()) if isinstance(r, dict) else r for r in self._rows]
        self._rows = []
        return rows

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def close(self):
        pass


class FakeDB:
    """Scripted stand-in for a mysql-connector connection."""

    def __init__(self):
        self.executed = []
        self.handlers = []
        self.users = {
            "partner-a": {"id": 1, "username": "partner-a", "hashed_password": PASSWORD_HASH, "disabled": 0},
            "partner-b": {"id": 2, "username": "partner-b", "hashed_password": PASSWORD_HASH, "disabled": 0},
            "disabled-user": {"id": 3, "username": "disabled-user", "hashed_password": PASSWORD_HASH, "disabled": 1},
            "no-grants": {"id": 4, "username": "no-grants", "hashed_password": PASSWORD_HASH, "disabled": 0},
        }
        # user_id -> grants. Each is (scope, agency) = active grant for partner "P-<agency>", or
        # (scope, partner_id, agency, state) with state "active" | "never" (non-expiring) | "expired" | "revoked".
        # partner-a and partner-b hold the four legacy scopes, DSWD-bound (data-owner rulings), so legacy-route tests can run.
        self.scopes = {uid: [(s, "DSWD") for s in sorted(LEGACY_SCOPES)] for uid in (1, 2)}
        self.staff_users = {}
        self.staff_scopes = {}
        self.grant_reads = 0
        self.commits = 0
        self.rollbacks = 0
        self.next_id = 100
        self.fail_with = None
        self.fail_match = ""
        self.on(r"FROM api_users WHERE username", lambda sql, p: [self.users[p[0]]] if p[0] in self.users else [])
        self.on(r"FROM api_user_scopes", self._active_grants)
        self.on(r"FROM staff_users WHERE issuer", lambda sql, p: [self.staff_users[p]] if p in self.staff_users else [])
        self.on(r"INSERT INTO staff_users", self._register_staff)
        self.on(r"FROM staff_user_scopes", self._active_staff_grants)

    def _register_staff(self, sql, params):
        self.staff_users[params] = {"id": self.next_id + 1, "disabled": 0}
        return []

    def _active_staff_grants(self, sql, params):
        assert "revoked_at IS NULL" in sql and "expires_at > CURRENT_TIMESTAMP" in sql
        rows = []
        for grant in self.staff_scopes.get(params[0], []):
            scope, partner, agency, state = grant if len(grant) == 4 else (grant[0], f"P-{grant[1]}", grant[1], "active")
            if state == "active":
                row = {"scope": scope, "partner_id": partner, "agency": agency}
                if "agency, expires_at" in sql:
                    row["expires_at"] = GRANT_EXPIRY
                rows.append(row)
        return rows

    def _active_grants(self, sql, params):
        # Mirrors ACTIVE_GRANTS_SQL: only unrevoked, unexpired rows come back.
        assert "revoked_at IS NULL" in sql and "(expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)" in sql
        with_expiry = "agency, expires_at" in sql
        self.grant_reads += 1
        rows = []
        for g in self.scopes.get(params[0], []):
            scope, partner, agency, state = g if len(g) == 4 else (g[0], f"P-{g[1]}", g[1], "active")
            if state in ("active", "never"):
                row = {"scope": scope, "partner_id": partner, "agency": agency}
                if with_expiry:
                    row["expires_at"] = GRANT_EXPIRY if state == "active" else None
                rows.append(row)
        return rows

    def on(self, pattern, handler):
        self.handlers.insert(0, (pattern, handler))

    def fail(self, exc, match=""):
        self.fail_with, self.fail_match = exc, match

    def cursor(self, dictionary=False):
        return FakeCursor(self, dictionary)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def queries(self, contains):
        return [(s, p) for s, p in self.executed if contains in s]


def db_error(errno, msg="secret internal detail: table tbl_person host 10.1.2.3"):
    return Error(msg=msg, errno=errno)


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def client(fake_db):
    # Each test gets its own HTTP limiter; requests in other tests consume no quota.
    app = main.create_app()

    def _dep():
        yield fake_db

    app.dependency_overrides[database.get_ods_connection] = _dep
    app.dependency_overrides[database.get_app_connection] = _dep
    FastAPICache.init(InMemoryBackend(), prefix="test", expire=60, key_builder=cache_key_builder)
    FastAPICache.get_backend()._store.clear()
    login_limiter.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """Lifespan must never reach a real server in unit tests."""
    monkeypatch.setattr(database, "init", lambda settings=None: None)
    monkeypatch.setattr(database, "close", lambda: None)


def bearer(username="partner-a", **claims):
    return {"Authorization": f"Bearer {create_access_token({'sub': username, **claims})}"}
