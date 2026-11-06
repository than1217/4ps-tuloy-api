"""Real synthetic RSA signatures; only discovery/JWKS transport and DB are replaced."""
import json
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import security
from config import get_settings
from conftest import PASSWORD, bearer
from scopes import LEGACY_MEMBER_VERIFY, SCOPE_VERIFY

ISSUER = "https://identity.example.invalid/realms/test"
AUDIENCE = "synthetic-api"
ME = "/v1/api/tuloy/staff/me"
VERIFY = {"first_name": "Zzsample", "last_name": "Synthetic", "date_of_birth": "1900-01-01"}


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def jwk(key, kid="first", **extra):
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
            "kid": kid, "alg": "RS256", "use": "sig", **extra}


def token(key, kid="first", **claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "synthetic-subject", "typ": "Bearer",
               "iat": now, "exp": now + 300, **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def keycloak(monkeypatch, fake_db, keys):
    import keycloak_auth
    settings = replace(get_settings(), staff_auth_mode="keycloak", keycloak_issuer=ISSUER,
                       keycloak_api_audience=AUDIENCE)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    import routers.auth
    monkeypatch.setattr(routers.auth, "get_settings", lambda: settings)
    state = {"keys": [jwk(keys[0])], "calls": [], "offline": False, "discovery": {}}

    def fetch(url, timeout):
        state["calls"].append(url)
        if state["offline"]:
            raise OSError("sensitive transport detail")
        if url.endswith("openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": ISSUER + "/protocol/openid-connect/certs",
                    **state["discovery"]}
        return {"keys": state["keys"]}

    monkeypatch.setattr(keycloak_auth, "_fetch_json", fetch)
    keycloak_auth.get_validator.cache_clear()
    fake_db.staff_users[(ISSUER, "synthetic-subject")] = {"id": 1, "disabled": 0}
    fake_db.staff_scopes[1] = [(LEGACY_MEMBER_VERIFY, "DSWD")]
    yield state
    keycloak_auth.get_validator.cache_clear()


def headers(value):
    return {"Authorization": "Bearer " + value}


def test_valid_keycloak_and_no_claim_permissions(client, keycloak, keys, fake_db):
    response = client.get(ME, headers=headers(token(keys[0], preferred_username="admin", roles=["admin"])))
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "admin"
    assert set(response.json()["user"]) == {"username"}
    fake_db.staff_scopes[1] = []
    response = client.get(ME, headers=headers(token(keys[0], scope=LEGACY_MEMBER_VERIFY)))
    assert response.status_code == 200
    assert response.json()["active_grants"] == []


@pytest.mark.parametrize("claims", [
    {"iss": "https://wrong.invalid"}, {"aud": "wrong"}, {"exp": 1}, {"sub": None},
    {"sub": ""}, {"sub": ["x"]}, {"typ": "ID"}, {"typ": None}, {"iat": "1"},
    {"exp": True}, {"aud": [AUDIENCE, 12]}, {"nbf": "0"},
])
def test_invalid_claims(client, keycloak, keys, claims):
    response = client.get(ME, headers=headers(token(keys[0], **claims)))
    assert response.status_code == 401
    assert "synthetic-subject" not in response.text


@pytest.mark.parametrize("claim", ["sub", "iat", "exp", "iss", "aud", "typ"])
def test_missing_claims(client, keycloak, keys, claim):
    payload = jwt.decode(token(keys[0]), options={"verify_signature": False})
    del payload[claim]
    value = jwt.encode(payload, keys[0], algorithm="RS256", headers={"kid": "first"})
    assert client.get(ME, headers=headers(value)).status_code == 401


def test_invalid_signature_and_unsigned_token(client, keycloak, keys):
    assert client.get(ME, headers=headers(token(keys[1]))).status_code == 401
    unsigned = jwt.encode({"sub": "synthetic-subject"}, key=None, algorithm="none")
    assert client.get(ME, headers=headers(unsigned)).status_code == 401


def test_rotation_caching_and_unknown_key_throttling(client, keycloak, keys):
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 200
    first_count = len(keycloak["calls"])
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 200
    assert len(keycloak["calls"]) == first_count
    keycloak["keys"] = [jwk(keys[1], "second")]
    assert client.get(ME, headers=headers(token(keys[1], "second"))).status_code == 200
    count = len(keycloak["calls"])
    for kid in ("unknown1", "unknown2", "unknown3"):
        assert client.get(ME, headers=headers(token(keys[1], kid))).status_code == 401
    assert len(keycloak["calls"]) == count


def test_jwks_expiry_fails_closed(client, keycloak, keys, monkeypatch):
    import keycloak_auth
    clock = [1000.0]
    monkeypatch.setattr(keycloak_auth.time, "monotonic", lambda: clock[0])
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 200
    keycloak["offline"] = True
    clock[0] += 301
    response = client.get(ME, headers=headers(token(keys[0])))
    assert response.status_code == 401
    assert "sensitive" not in response.text


@pytest.mark.parametrize("discovery", [
    {"issuer": "https://wrong.invalid"}, {"jwks_uri": "http://127.0.0.1/keys"},
    {"jwks_uri": "https://attacker.invalid/keys"},
])
def test_untrusted_discovery_denied(client, keycloak, keys, discovery):
    keycloak["discovery"] = discovery
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 401
    assert len(keycloak["calls"]) == 1


@pytest.mark.parametrize("change", [{"use": "enc"}, {"alg": "HS256"}, {"kty": "oct"}, {"key_ops": ["sign"]}])
def test_invalid_jwk_denied(client, keycloak, keys, change):
    keycloak["keys"] = [jwk(keys[0], **change)]
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 401


def test_unmapped_disabled_and_revoked_staff(client, keycloak, keys, fake_db):
    assert client.get(ME, headers=headers(token(keys[0], sub="unmapped", email="partner-a"))).status_code == 403
    fake_db.staff_users[(ISSUER, "synthetic-subject")]["disabled"] = 1
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 403
    fake_db.staff_users[(ISSUER, "synthetic-subject")]["disabled"] = 0
    auth = headers(token(keys[0]))
    assert client.post("/VerifyMemberBene", json=VERIFY, headers=auth).status_code == 200
    fake_db.staff_scopes[1] = [(LEGACY_MEMBER_VERIFY, "P-DSWD", "DSWD", "revoked")]
    assert client.post("/VerifyMemberBene", json=VERIFY, headers=auth).status_code == 403


def test_staff_service_boundary(client, keycloak, keys, fake_db):
    assert client.get(ME, headers=bearer()).status_code == 401
    assert client.post("/VerifyMemberBene", json=VERIFY, headers=bearer()).status_code == 200
    assert client.post("/token", data={"username": "partner-a", "password": PASSWORD}).status_code == 200
    fake_db.scopes[1] = [(SCOPE_VERIFY, "CHED")]
    # Explicit partner boundary still uses local service JWTs, not staff SSO tokens.
    assert client.post("/v1/api/tuloy/members/verify", json=VERIFY, headers=bearer()).status_code == 200
    assert client.post("/v1/api/tuloy/members/verify", json=VERIFY,
                       headers=headers(token(keys[0]))).status_code == 401


def test_explicit_service_token_issuance_never_opens_staff_endpoints(client, keycloak, fake_db, monkeypatch):
    import routers.auth
    settings = replace(security.get_settings())
    monkeypatch.setattr(routers.auth, "get_settings", lambda: settings)
    response = client.post("/token", data={"username": "partner-a", "password": PASSWORD})
    assert response.status_code == 200
    assert client.get(ME, headers=headers(response.json()["access_token"])).status_code == 401


def test_keycloak_errors_do_not_disclose_tokens_or_subjects(client, keycloak, keys, caplog):
    value = token(keys[0], sub="private-subject", aud="wrong")
    response = client.get(ME, headers=headers(value))
    assert response.status_code == 401
    assert value not in caplog.text + response.text
    assert "private-subject" not in caplog.text + response.text


def test_explicit_local_validator_reads_http_discovery_and_jwks(monkeypatch, keys):
    import keycloak_auth
    issuer = "http://127.0.0.1:8080/realms/test"
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if url.endswith("openid-configuration"):
            return {"issuer": issuer, "jwks_uri": issuer + "/certs"}
        return {"keys": [jwk(keys[0])]}

    monkeypatch.setattr(keycloak_auth, "_fetch_json", fetch)
    validator = keycloak_auth.KeycloakValidator(issuer, AUDIENCE, 300, 5, allow_local_http=True)
    assert validator.decode(token(keys[0], iss=issuer))["sub"] == "synthetic-subject"
    assert calls == [issuer + "/.well-known/openid-configuration", issuer + "/certs"]


@pytest.mark.parametrize("issuer,local", [
    ("http://127.0.0.1:8080/realms/test", False),
    ("http://remote.invalid/realms/test", True),
    ("http://localhost.evil.invalid/realms/test", True),
])
def test_validator_refuses_unsafe_http_before_network(monkeypatch, issuer, local):
    import keycloak_auth
    monkeypatch.setattr(keycloak_auth, "_fetch_json", lambda *args: pytest.fail("Must not fetch"))
    with pytest.raises(jwt.InvalidTokenError):
        keycloak_auth.KeycloakValidator(issuer, AUDIENCE, 300, 5, allow_local_http=local)


def test_staff_settings_read_but_never_write_without_policy(client, keycloak, keys, fake_db):
    auth = headers(token(keys[0]))
    assert client.get('/GetAppSettings', headers=auth).status_code == 200
    from scopes import STAFF_ADMIN
    fake_db.staff_scopes[1] = [(STAFF_ADMIN, 'DSWD')]
    before = fake_db.commits
    response = client.post('/PostAppSettings', headers=auth, json={'app_name': 'Synthetic'})
    assert response.status_code == 403
    assert fake_db.commits == before
    assert not fake_db.queries('INSERT INTO app_settings')


def test_staff_cache_owner_and_live_revocation_are_separate_from_service(client, keycloak, keys, fake_db):
    from scopes import LEGACY_BENEFICIARY_READ
    fake_db.staff_scopes[1] = [(LEGACY_BENEFICIARY_READ, 'DSWD')]
    calls = []
    def rows(sql, params):
        calls.append(params)
        return [{'tuloy_id': f'synthetic-{len(calls)}'}]
    fake_db.on(r'FROM vw_unmatched_member_bene WHERE `member_id` NOT IN', rows)
    path = '/GetEligibleBene?limit=1'
    service = client.get(path, headers=bearer()).json()
    auth = headers(token(keys[0]))
    staff = client.get(path, headers=auth)
    assert staff.status_code == 200 and staff.json()['items'] != service['items']
    assert client.get(path, headers=auth).json() == staff.json()
    assert len(calls) == 2
    fake_db.staff_scopes[1] = [(LEGACY_BENEFICIARY_READ, 'DSWD', 'DSWD', 'expired')]
    assert client.get(path, headers=auth).status_code == 403
    assert client.get(path, headers=bearer()).status_code == 200


def test_staff_profile_preserves_native_privacy_and_agency_rulings(client, keycloak, keys, fake_db):
    fake_db.staff_scopes[1] = [(LEGACY_MEMBER_VERIFY, 'CHED')]
    auth = headers(token(keys[0], realm_access={'roles': ['admin']}, email='private@example.invalid'))
    profile = client.get(ME, headers=auth).json()
    assert set(profile) == {'user', 'active_grants', 'token'}
    assert set(profile['user']) == {'username'}
    assert profile['active_grants'] == []
    assert client.post('/VerifyMemberBene', json=VERIFY, headers=auth).status_code == 403

@pytest.fixture
def full_staff_access(keycloak, monkeypatch):
    settings = replace(security.get_settings(), staff_full_access_enabled=True)
    monkeypatch.setattr(security, 'get_settings', lambda: settings)
    return settings


@pytest.mark.parametrize('subject,user_id', [('synthetic-subject', 1), ('another-enabled-staff', 9)])
def test_full_staff_access_for_every_enabled_identity_without_grant_rows(client, full_staff_access, keys, fake_db, subject, user_id):
    from scopes import LEGACY_BENEFICIARY_READ, LEGACY_CHED_READ, STAFF_ADMIN
    fake_db.staff_users[(ISSUER, subject)] = {'id': user_id, 'disabled': 0}
    fake_db.staff_scopes.clear()
    auth = headers(token(keys[0], sub=subject))
    profile = client.get(ME, headers=auth)
    assert profile.status_code == 200
    data = profile.json()
    assert {g['scope'] for g in data['active_grants']} == {
        LEGACY_MEMBER_VERIFY, LEGACY_BENEFICIARY_READ, LEGACY_CHED_READ, STAFF_ADMIN,
    }
    assert all(g['agency'] == 'DSWD' and g['expires_at'] == data['token']['expires_at'] for g in data['active_grants'])
    assert client.get('/GetEligibleBene', headers=auth).status_code == 200
    assert client.get('/GetMatchedChedBene', headers=auth).status_code == 200
    assert client.post('/VerifyMemberBene', headers=auth, json=VERIFY).status_code == 200
    assert not fake_db.queries('staff_user_scopes')
    assert not fake_db.queries('INSERT INTO api_user_scopes')


def test_full_staff_access_allows_settings_write_but_never_service_routes(client, full_staff_access, keys, fake_db, monkeypatch):
    from routers import system
    monkeypatch.setattr(system, "get_settings", lambda: replace(full_staff_access, settings_write_enabled=True))
    auth = headers(token(keys[0]))
    assert client.post('/PostAppSettings', headers=auth, json={'app_name': 'Synthetic staff portal'}).status_code == 200
    assert fake_db.commits == 1
    assert not fake_db.queries('INSERT INTO staff_user_scopes')
    assert client.post('/PostEnrolleeMatchedCHED', headers=auth,
                       json={'status': False, 'message': 'NOT FOUND'}).status_code == 401
    assert client.post('/v1/api/tuloy/members/verify', headers=auth, json=VERIFY).status_code == 401
    fake_db.scopes[1] = []
    assert client.get('/GetEligibleBene', headers=bearer()).status_code == 403


@pytest.mark.parametrize('claims', [{'aud': 'wrong'}, {'iss': 'https://wrong.invalid'}, {'typ': 'ID'}, {'exp': 1}])
def test_full_staff_access_still_requires_valid_access_token(client, full_staff_access, keys, claims):
    assert client.get('/GetEligibleBene', headers=headers(token(keys[0], **claims))).status_code == 401


def test_full_staff_access_still_requires_registered_enabled_identity(client, full_staff_access, keys, fake_db):
    assert client.get('/GetEligibleBene', headers=headers(token(keys[0], sub='unregistered'))).status_code == 403
    fake_db.staff_users[(ISSUER, 'synthetic-subject')]['disabled'] = 1
    assert client.get('/GetEligibleBene', headers=headers(token(keys[0]))).status_code == 403


def test_full_staff_access_never_applies_in_production_even_if_settings_constructed_directly(client, full_staff_access, monkeypatch, keys, fake_db):
    monkeypatch.setattr(security, 'get_settings', lambda: replace(full_staff_access, app_env='prod'))
    fake_db.staff_scopes.clear()
    auth = headers(token(keys[0]))
    assert client.get(ME, headers=auth).json()['active_grants'] == []
    assert client.get('/GetEligibleBene', headers=auth).status_code == 403
    assert client.post('/PostAppSettings', headers=auth, json={'app_name': 'Synthetic'}).status_code == 403


def test_full_staff_access_first_login_creates_no_persistent_grants(client, full_staff_access, keys, fake_db):
    fake_db.staff_users.clear()
    auth = headers(token(keys[0]))
    assert client.post('/v1/api/tuloy/staff/session', headers=auth).status_code == 200
    assert client.get(ME, headers=auth).json()['active_grants']
    assert not fake_db.queries('INSERT INTO staff_user_scopes')
    assert not fake_db.queries('api_users')


def test_disabling_full_staff_access_restores_grant_checks_even_with_warm_cache(client, full_staff_access, monkeypatch, keys, fake_db):
    auth = headers(token(keys[0]))
    fake_db.staff_scopes.clear()
    assert client.get('/GetEligibleBene', headers=auth).status_code == 200
    monkeypatch.setattr(security, 'get_settings', lambda: replace(full_staff_access, staff_full_access_enabled=False))
    assert client.get('/GetEligibleBene', headers=auth).status_code == 403
    assert client.get(ME, headers=auth).json()['active_grants'] == []


@pytest.mark.parametrize('path', ['eligible-beneficiaries', 'matched-ched-beneficiaries', 'settings', 'overview'])
def test_full_staff_current_web_reads(client, full_staff_access, keys, fake_db, path):
    fake_db.on(r'COUNT\(\*\) AS `n`', lambda sql, p: [{'n': 0}])
    assert client.get('/v1/api/tuloy/staff/' + path, headers=headers(token(keys[0]))).status_code == 200
    assert fake_db.commits == 0


def test_full_staff_settings_write_gate_stays_closed(client, full_staff_access, keys, fake_db):
    from routers import system
    assert not system.get_settings().settings_write_enabled
    response = client.post('/PostAppSettings', headers=headers(token(keys[0])), json={'app_name': 'Synthetic'})
    assert response.status_code == 403
    assert response.json()['error_code'] == 'SETTINGS_WRITE_DISABLED'
    assert fake_db.commits == 0


@pytest.mark.parametrize('full', [False, True])
def test_staff_payments_use_staff_prefix_only(client, full_staff_access, keys, fake_db, monkeypatch, full):
    monkeypatch.setattr(security, 'get_settings', lambda: replace(full_staff_access, staff_full_access_enabled=full))
    fake_db.staff_scopes[1] = [(LEGACY_MEMBER_VERIFY, 'DSWD', 'DSWD', 'active')]
    auth = headers(token(keys[0]))
    assert client.get('/v1/api/tuloy/staff/payments/rates', headers=auth).status_code == 200
    assert client.get('/api/payments/rates', headers=auth).status_code == 401
    assert fake_db.commits == 0


def test_staff_module_affiliation_is_still_checked(client, keycloak, keys, fake_db):
    auth = headers(token(keys[0]))
    fake_db.staff_scopes[1] = []
    # A service grant for the same numeric ID must not authorize staff.
    for path in ('payments/rates', 'bcmd/status'):
        assert client.get('/v1/api/tuloy/staff/' + path, headers=auth).status_code == 403
    fake_db.staff_scopes[1] = [(LEGACY_MEMBER_VERIFY, 'CHED', 'DSWD', 'active')]
    for path in ('payments/rates', 'bcmd/status'):
        assert client.get('/v1/api/tuloy/staff/' + path, headers=auth).status_code == 403


def test_full_staff_bcmd_preserves_readiness_before_creator_write(client, full_staff_access, keys, fake_db):
    auth = headers(token(keys[0]))
    response = client.get('/v1/api/tuloy/staff/bcmd/status', headers=auth)
    assert response.status_code == 200
    assert response.json()['available'] is False
    assert client.get('/v1/api/tuloy/staff/bcmd/batches', headers=auth).status_code == 503
    response = client.post(
        '/v1/api/tuloy/staff/bcmd/batches', headers=auth,
        json={'name': 'Synthetic', 'category': 'TK', 'record_ids': [1]})
    assert response.status_code == 503
    assert response.json()['error_code'] == 'BCMD_UNAVAILABLE'
    assert not fake_db.queries('INSERT INTO bcmd_')
    assert fake_db.commits == 0
