from datetime import datetime, timedelta, timezone

import jwt

from config import get_settings
from conftest import PASSWORD, bearer, db_error

PROTECTED = ("/GetMatchedChedBene", "/GetEligibleBene")


def _token(**overrides):
    s = get_settings()
    now = datetime.now(timezone.utc)
    claims = {"sub": "partner-a", "iss": s.jwt_issuer, "aud": s.jwt_audience, "iat": now,
              "exp": now + timedelta(minutes=5)}
    claims.update(overrides)
    key = claims.pop("_key", s.secret_key)
    alg = claims.pop("_alg", "HS256")
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm=alg)


def test_missing_token_is_401(client):
    for path in PROTECTED:
        r = client.get(path)
        assert r.status_code == 401
        assert r.json()["error_code"] == "UNAUTHENTICATED"
        assert r.headers["WWW-Authenticate"] == "Bearer"


def test_rejected_tokens(client):
    bad = {
        "garbage": "not-a-jwt",
        "expired": _token(exp=datetime.now(timezone.utc) - timedelta(minutes=5)),
        "wrong_audience": _token(aud="someone-else"),
        "wrong_issuer": _token(iss="someone-else"),
        "wrong_key": _token(_key="x" * 40),
        "no_exp": _token(exp=None),
        "alg_none": jwt.encode({"sub": "partner-a"}, None, algorithm="none"),
        "unknown_user": _token(sub="nobody"),
    }
    for name, token in bad.items():
        r = client.get("/GetMatchedChedBene", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, name


def test_legacy_token_without_claims_rejected(client):
    """Old tokens carried only `sub` and never expired; they must stop working."""
    legacy = jwt.encode({"sub": "partner-a"}, get_settings().secret_key, algorithm="HS256")
    r = client.get("/GetMatchedChedBene", headers={"Authorization": f"Bearer {legacy}"})
    assert r.status_code == 401


def test_disabled_user_is_403(client):
    r = client.get("/GetMatchedChedBene", headers=bearer("disabled-user"))
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_DISABLED"


def test_login_success_returns_expiring_token(client):
    r = client.post("/token", data={"username": "partner-a", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == get_settings().access_token_expire_minutes * 60
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert {"exp", "iat", "iss", "aud", "jti"} <= set(claims)


def test_login_failures_are_generic(client):
    wrong_pw = client.post("/token", data={"username": "partner-a", "password": "nope"})
    no_user = client.post("/token", data={"username": "ghost", "password": "nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["detail"] == no_user.json()["detail"]


def test_disabled_user_cannot_log_in(client):
    r = client.post("/token", data={"username": "disabled-user", "password": PASSWORD})
    assert r.status_code == 403


def test_login_rate_limited(client):
    codes = [client.post("/token", data={"username": "partner-a", "password": "bad"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[-1] == 429


def test_login_db_error_does_not_leak(client, fake_db):
    fake_db.fail(db_error(1045), "api_users")
    r = client.post("/token", data={"username": "partner-a", "password": PASSWORD})
    assert r.status_code == 500 and "10.1.2.3" not in r.text and "tbl_person" not in r.text


def test_signup_disabled_by_default(client, fake_db):
    r = client.post("/signup", json={"username": "newuser", "password": "long-enough-password"})
    assert r.status_code == 404
    assert not fake_db.queries("INSERT")


def test_signup_when_enabled(client, fake_db, monkeypatch):
    import dataclasses
    import routers.auth as auth_router
    settings = dataclasses.replace(get_settings(), allow_signup=True)
    monkeypatch.setattr(auth_router, "get_settings", lambda: settings)
    weak = client.post("/signup", json={"username": "newuser", "password": "tiny-pw"})
    assert weak.status_code == 422
    assert "tiny-pw" not in weak.text  # submitted value not echoed
    ok = client.post("/signup", json={"username": "newuser", "password": "long-enough-password"})
    assert ok.status_code == 201
    insert_sql, params = fake_db.queries("INSERT INTO api_users")[0]
    assert params[0] == "newuser" and params[1].startswith("$2") and "long-enough-password" not in params
    taken = client.post("/signup", json={"username": "partner-a", "password": "long-enough-password"})
    assert taken.status_code == 400


def test_sign_in_docs_show_only_username_and_password(client):
    spec = client.app.openapi()
    ref = spec["paths"]["/token"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["$ref"]
    form = spec["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert set(form["properties"]) == {"username", "password"} and set(form["required"]) == {"username", "password"}
    assert form["properties"]["password"]["format"] == "password"
    ok = spec["paths"]["/token"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert set(spec["components"]["schemas"][ok.rsplit("/", 1)[1]]["properties"]) == {"access_token", "token_type", "expires_in"}
    assert spec["components"]["securitySchemes"] == {"BearerAuth": {
        "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
        "description": "Paste the `access_token` returned by `POST /token`. Access depends on the account's grants."}}


def test_sign_in_ignores_oauth2_extras(client):
    """Swagger's Authorize dialog and OAuth2 clients may still send grant_type, client_id, etc."""
    r = client.post("/token", data={"username": "partner-a", "password": PASSWORD, "grant_type": "password",
                                    "scope": "", "client_id": "x", "client_secret": "y"})
    assert r.status_code == 200 and set(r.json()) == {"access_token", "token_type", "expires_in"}


def test_token_from_sign_in_works_as_bearer(client):
    token = client.post("/token", data={"username": "partner-a", "password": PASSWORD}).json()["access_token"]
    r = client.post("/VerifyMemberBene", headers={"Authorization": f"Bearer {token}"},
                    json={"first_name": "Zz", "last_name": "Synthetic", "date_of_birth": "1900-01-01"})
    assert r.status_code == 200
    assert client.post("/VerifyMemberBene", headers={"Authorization": f"Basic {token}"},
                       json={"first_name": "Zz", "last_name": "Synthetic", "date_of_birth": "1900-01-01"}).status_code == 401

