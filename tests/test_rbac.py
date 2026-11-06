"""RBAC: active (scope, partner_id, agency) grants, read per request (ADR 2026-10-26 RBAC)."""
import logging

import pytest

from conftest import PASSWORD, bearer, db_error
from scopes import (
    LEGACY_BENEFICIARY_READ,
    LEGACY_CHED_READ,
    LEGACY_CHED_WRITE,
    LEGACY_MEMBER_VERIFY,
    SCOPE_ENROLL_READ,
    SCOPE_VERIFY,
)
from security import Grant, Principal, require_scope

VERIFY_BODY = {"first_name": "Zzsample", "last_name": "Synthetic", "date_of_birth": "1900-01-01"}
CHED_BODY = {"status": False, "message": "NOT FOUND"}

LEGACY_ROUTES = {
    LEGACY_MEMBER_VERIFY: ("post", "/VerifyMemberBene", VERIFY_BODY),
    LEGACY_BENEFICIARY_READ: ("get", "/GetEligibleBene?limit=1", None),
    LEGACY_CHED_WRITE: ("post", "/PostEnrolleeMatchedCHED", CHED_BODY),
    LEGACY_CHED_READ: ("get", "/GetMatchedChedBene?limit=1", None),
}


def call(client, scope, user):
    method, path, body = LEGACY_ROUTES[scope]
    kwargs = {"headers": bearer(user)}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("scope", sorted(LEGACY_ROUTES))
def test_legacy_route_denied_without_grant(client, scope):
    r = call(client, scope, "no-grants")
    assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN"


@pytest.mark.parametrize("scope", sorted(LEGACY_ROUTES))
def test_legacy_route_allowed_with_its_grant_only(client, fake_db, scope):
    fake_db.scopes[4] = [(scope, "DSWD")]
    assert call(client, scope, "no-grants").status_code == 200
    for other in LEGACY_ROUTES:
        if other != scope:
            assert call(client, other, "no-grants").status_code == 403, other


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_inactive_grant_is_denied(client, fake_db, state):
    fake_db.scopes[4] = [(LEGACY_MEMBER_VERIFY, "P-DSWD", "DSWD", state)]
    assert call(client, LEGACY_MEMBER_VERIFY, "no-grants").status_code == 403


@pytest.mark.parametrize("row", [
    ("tuloy:legacy:everything", "P-DSWD", "DSWD", "active"),   # unknown scope name
    (LEGACY_MEMBER_VERIFY, "", "TEST", "active"),              # no partner binding
    (LEGACY_MEMBER_VERIFY, "P-TEST", "", "active"),            # no agency binding
])
def test_unenforceable_grant_is_denied(client, fake_db, row):
    fake_db.scopes[4] = [row]
    assert call(client, LEGACY_MEMBER_VERIFY, "no-grants").status_code == 403


def test_partner_scope_does_not_open_legacy_route(client, fake_db):
    fake_db.scopes[4] = [(SCOPE_VERIFY, "CHED")]
    assert call(client, LEGACY_MEMBER_VERIFY, "no-grants").status_code == 403


def test_jwt_scope_claims_confer_nothing(client):
    headers = bearer("no-grants", scope=LEGACY_MEMBER_VERIFY, scopes=[LEGACY_MEMBER_VERIFY])
    assert client.post("/VerifyMemberBene", json=VERIFY_BODY, headers=headers).status_code == 403


def test_missing_grant_table_denies(client, fake_db):
    fake_db.fail(db_error(1146, "Table doesn't exist"), match="api_user_scopes")
    assert call(client, LEGACY_MEMBER_VERIFY, "partner-a").status_code == 403


def test_grant_store_failure_fails_closed(client, fake_db):
    fake_db.fail(db_error(2013, "Lost connection"), match="api_user_scopes")
    r = call(client, LEGACY_MEMBER_VERIFY, "partner-a")
    assert r.status_code == 503 and "Lost connection" not in r.text


def test_revocation_applies_from_next_request(client, fake_db):
    fake_db.scopes[4] = [(LEGACY_CHED_READ, "DSWD")]
    assert call(client, LEGACY_CHED_READ, "no-grants").status_code == 200
    fake_db.scopes[4] = [(LEGACY_CHED_READ, "P-DSWD", "DSWD", "revoked")]
    assert call(client, LEGACY_CHED_READ, "no-grants").status_code == 403


def test_ched_write_rechecks_grant_inside_transaction(client, fake_db):
    """Revoked between authorisation and the write: denied, nothing inserted."""
    original = fake_db._active_grants

    def revoke_after_first_read(sql, params):
        rows = original(sql, params)
        if fake_db.grant_reads >= 2:
            return []
        return rows
    fake_db.on(r"FROM api_user_scopes", revoke_after_first_read)
    r = client.post("/PostEnrolleeMatchedCHED", json=CHED_BODY, headers=bearer("partner-a"))
    assert r.status_code == 403
    assert fake_db.grant_reads == 2 and not fake_db.queries("INSERT INTO")


def test_ched_write_recheck_is_first_statement(client, fake_db):
    r = client.post("/PostEnrolleeMatchedCHED", json=CHED_BODY, headers=bearer("partner-a"))
    assert r.status_code == 200
    statements = [s for s, _ in fake_db.executed]
    first_write = next(i for i, s in enumerate(statements) if s.lstrip().upper().startswith("INSERT"))
    assert "api_user_scopes" in statements[first_write - 1]


def test_scope_denial_is_audited(client, caplog):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    call(client, LEGACY_CHED_READ, "no-grants")
    events = [r for r in caplog.records if r.name == "tuloy.audit" and getattr(r, "action", "") == "authz.scope"]
    assert events and events[-1].outcome == "denied" and events[-1].scope == LEGACY_CHED_READ


def test_grants_keep_whole_tuples():
    """Holding scope X for CHED and scope Y for TESDA never yields X for TESDA."""
    p = Principal(1, "u", grants=frozenset({Grant(SCOPE_VERIFY, "P-CHED", "CHED"),
                                            Grant(SCOPE_ENROLL_READ, "P-TESDA", "TESDA")}))
    assert p.grants_for(SCOPE_VERIFY) == {Grant(SCOPE_VERIFY, "P-CHED", "CHED")}
    assert {g.agency for g in p.grants_for(SCOPE_ENROLL_READ)} == {"TESDA"}
    assert p.scopes == {SCOPE_VERIFY, SCOPE_ENROLL_READ}


def test_unknown_scope_name_rejected_at_definition():
    with pytest.raises(ValueError):
        require_scope("tuloy:made:up")


def test_login_then_scoped_call(client, fake_db):
    fake_db.scopes[4] = [(LEGACY_MEMBER_VERIFY, "DSWD")]
    r = client.post("/token", data={"username": "no-grants", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    token = r.json()["access_token"]
    ok = client.post("/VerifyMemberBene", json=VERIFY_BODY, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    denied = client.get("/GetEligibleBene?limit=1", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403


def test_bad_password_is_401_and_audited(client, caplog):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    r = client.post("/token", data={"username": "no-grants", "password": "wrong-password"})
    assert r.status_code == 401
    events = [x for x in caplog.records if x.name == "tuloy.audit" and getattr(x, "action", "") == "auth.token"]
    assert events and events[-1].outcome == "denied" and events[-1].subject != "no-grants"


# ---- Non-expiring grants (migration 0006, amendment to the 2026-10-26 RBAC ADR) --------------

def test_non_expiring_grant_is_active(client, fake_db):
    fake_db.scopes[4] = [(LEGACY_MEMBER_VERIFY, "DSWD", "DSWD", "never")]
    assert call(client, LEGACY_MEMBER_VERIFY, "no-grants").status_code == 200
    assert call(client, LEGACY_CHED_READ, "no-grants").status_code == 403   # grants only its own scope


def test_revoked_non_expiring_grant_is_denied_from_next_request(client, fake_db):
    fake_db.scopes[4] = [(LEGACY_MEMBER_VERIFY, "DSWD", "DSWD", "never")]
    assert call(client, LEGACY_MEMBER_VERIFY, "no-grants").status_code == 200
    fake_db.scopes[4] = [(LEGACY_MEMBER_VERIFY, "DSWD", "DSWD", "revoked")]
    assert call(client, LEGACY_MEMBER_VERIFY, "no-grants").status_code == 403


def test_disabled_account_with_non_expiring_grant_is_denied(client, fake_db):
    fake_db.scopes[3] = [(LEGACY_MEMBER_VERIFY, "DSWD", "DSWD", "never")]
    r = call(client, LEGACY_MEMBER_VERIFY, "disabled-user")
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_DISABLED"


def test_active_grant_predicate_is_shared_and_admits_null_expiry():
    import security
    assert "(expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)" in security.ACTIVE_GRANTS_SQL
    assert "revoked_at IS NULL" in security.ACTIVE_GRANTS_SQL
    assert security.ACTIVE_GRANTS_SQL.split("FROM", 1)[1] == security.ACTIVE_GRANTS_WITH_EXPIRY_SQL.split("FROM", 1)[1]


def test_login_token_lifetime_stays_thirty_minutes(client):
    r = client.post("/token", data={"username": "partner-a", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["expires_in"] == 1800


# ---- S11: every rejected credential is audited (QA gate follow-up, fix 5) ----------------------

def _authn_events(caplog):
    return [r for r in caplog.records if r.name == "tuloy.audit" and getattr(r, "action", "") == "authn.token"]


@pytest.mark.parametrize("headers, reason", [
    ({}, "missing_token"),
    ({"Authorization": "Bearer "}, "missing_token"),
    ({"Authorization": "Bearer not-a-jwt"}, "invalid_token"),
    (bearer(""), "invalid_subject"),
    (bearer("nobody-here"), "unknown_user"),
])
def test_rejected_credentials_are_audited_without_the_token(client, caplog, headers, reason):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    r = client.get("/GetEligibleBene?limit=1", headers=headers)
    assert r.status_code == 401
    events = _authn_events(caplog)
    assert events and events[-1].outcome == "denied" and events[-1].reason == reason
    token = headers.get("Authorization", "").removeprefix("Bearer ").strip()
    assert not token or token not in caplog.text
    assert "nobody-here" not in caplog.text


def test_disabled_account_denial_is_audited(client, caplog):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    r = client.get("/v1/api/tuloy/staff/me", headers=bearer("disabled-user"))
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_DISABLED"
    events = _authn_events(caplog)
    assert events and events[-1].reason == "disabled" and events[-1].actor == "user:3"
