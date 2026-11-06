"""GET /v1/api/tuloy/staff/me: staff identity read. Any valid token, no scope."""
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from config import get_settings
from conftest import bearer, db_error
import database
from main import create_app
from scopes import DOCS_INTERNAL, LEGACY_CHED_READ, SCOPE_VERIFY, STAFF_ADMIN
from security import ALGORITHM

PATH = "/v1/api/tuloy/staff/me"


def test_returns_identity_active_grants_and_token_expiry(client, fake_db):
    fake_db.scopes[4] = [(SCOPE_VERIFY, "CHED"), (LEGACY_CHED_READ, "DSWD")]
    headers = bearer("no-grants")
    exp = jwt.decode(headers["Authorization"].split()[1], options={"verify_signature": False})["exp"]
    r = client.get(PATH, headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "user": {"username": "no-grants"},
        "active_grants": [
            {"scope": LEGACY_CHED_READ, "agency": "DSWD", "expires_at": "2099-01-02T03:04:05Z"},
            {"scope": SCOPE_VERIFY, "agency": "CHED", "expires_at": "2099-01-02T03:04:05Z"},
        ],
        "token": {"expires_at": datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
    }


def test_no_grants_gives_empty_list(client):
    r = client.get(PATH, headers=bearer("no-grants"))
    assert r.status_code == 200 and r.json()["active_grants"] == []


def test_expired_and_revoked_grants_are_excluded(client, fake_db):
    fake_db.scopes[4] = [(SCOPE_VERIFY, "P-A", "A", "expired"), (LEGACY_CHED_READ, "P-B", "B", "revoked"),
                         (DOCS_INTERNAL, "P-C", "C", "active")]
    grants = client.get(PATH, headers=bearer("no-grants")).json()["active_grants"]
    assert [g["scope"] for g in grants] == [DOCS_INTERNAL]


def test_unknown_scope_is_excluded(client, fake_db):
    fake_db.scopes[4] = [("tuloy:made:up", "P-A", "A", "active"), (SCOPE_VERIFY, "P-A", "A", "active")]
    grants = client.get(PATH, headers=bearer("no-grants")).json()["active_grants"]
    assert [g["scope"] for g in grants] == [SCOPE_VERIFY]


def test_staff_admin_grant_is_reported_for_dswd_only(client, fake_db):
    fake_db.scopes[4] = [(STAFF_ADMIN, "DSWD", "DSWD", "active"), (STAFF_ADMIN, "CHED", "CHED", "active")]
    grants = client.get(PATH, headers=bearer("no-grants")).json()["active_grants"]
    assert [(g["scope"], g["agency"]) for g in grants] == [(STAFF_ADMIN, "DSWD")]


def test_staff_admin_opens_no_api_route(client, fake_db):
    fake_db.scopes[4] = [(STAFF_ADMIN, "DSWD", "DSWD", "active")]
    r = client.post("/VerifyMemberBene", headers=bearer("no-grants"),
                    json={"first_name": "A", "last_name": "B", "date_of_birth": "2000-01-01"})
    assert r.status_code == 403


def test_body_has_no_other_fields(client):
    r = client.get(PATH, headers=bearer("partner-a"))
    body = r.json()
    assert set(body) == {"user", "active_grants", "token"} and set(body["user"]) == {"username"}
    assert all(set(g) == {"scope", "agency", "expires_at"} for g in body["active_grants"]) and body["active_grants"]
    assert "hashed_password" not in r.text


def test_missing_token_is_401(client):
    r = client.get(PATH)
    assert r.status_code == 401 and r.json()["error_code"] == "UNAUTHENTICATED" and r.json()["request_id"]


def test_invalid_token_is_401(client):
    r = client.get(PATH, headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401 and r.json()["error_code"] == "UNAUTHENTICATED"


def test_expired_token_is_401(client):
    s = get_settings()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token = jwt.encode({"sub": "partner-a", "iss": s.jwt_issuer, "aud": s.jwt_audience,
                        "iat": past - timedelta(hours=1), "exp": past}, s.secret_key, algorithm=ALGORITHM)
    r = client.get(PATH, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["error_code"] == "UNAUTHENTICATED"


def test_unknown_user_is_401(client):
    assert client.get(PATH, headers=bearer("ghost")).status_code == 401


def test_disabled_account_is_403(client):
    r = client.get(PATH, headers=bearer("disabled-user"))
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_DISABLED"


def test_grant_store_failure_is_503_without_detail(client, fake_db):
    fake_db.fail(db_error(2013, "Lost connection"), match="api_user_scopes")
    r = client.get(PATH, headers=bearer("partner-a"))
    assert r.status_code == 503 and "Lost connection" not in r.text


def test_route_present_when_partner_api_disabled(monkeypatch, fake_db):
    monkeypatch.setenv("PARTNER_API_ENABLED", "false")
    get_settings.cache_clear()
    try:
        app = create_app()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    def _dep():
        yield fake_db
    app.dependency_overrides[database.get_readonly_connection] = _dep
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get(PATH, headers=bearer("partner-a")).status_code == 200
        assert c.get("/v1/api/tuloy/members/verify", headers=bearer("partner-a")).status_code in (404, 405)


def test_not_in_partner_or_internal_docs(client, fake_db):
    assert PATH not in client.get("/tuloy/openapi.json").json()["paths"]
    fake_db.scopes[4] = [(DOCS_INTERNAL, "DSWD")]
    assert PATH not in client.get("/tuloydswd/openapi.json", headers=bearer("no-grants")).json()["paths"]


def test_success_is_audited_without_pii(client, caplog):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    client.get(PATH, headers=bearer("partner-a"))
    events = [r for r in caplog.records if r.name == "tuloy.audit" and getattr(r, "action", "") == "staff.me"]
    assert events and events[-1].actor == "user:1" and "partner-a" not in str(events[-1].__dict__)


def test_non_expiring_grant_has_null_expires_at(client, fake_db):
    fake_db.scopes[4] = [(SCOPE_VERIFY, "TESDA", "TESDA", "never"), (LEGACY_CHED_READ, "DSWD")]
    grants = {g["scope"]: g for g in client.get(PATH, headers=bearer("no-grants")).json()["active_grants"]}
    assert grants[SCOPE_VERIFY]["expires_at"] is None
    assert grants[LEGACY_CHED_READ]["expires_at"] == "2099-01-02T03:04:05Z"
