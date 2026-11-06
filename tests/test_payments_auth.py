"""Payment access policy; all records and tokens are synthetic, with no network/database writes."""
import pytest

from conftest import bearer, db_error
from scopes import SCOPE_VERIFY


OPERATIONS = [
    ("GET", "/api/payments/rates", None),
    ("GET", "/api/payments/beneficiaries", None),
    ("POST", "/api/payments/compute", {"months": 1}),
    ("POST", "/api/payments/generate", {"months": 1, "academic_year_term": 1, "assistance_type": 1}),
    ("GET", "/api/payments/export/csv?months=1", None),
    ("GET", "/api/payments/export/summary?months=1", None),
    ("GET", "/api/payments/export/pdf?months=1", None),
]


@pytest.mark.parametrize("method,path,body", OPERATIONS)
@pytest.mark.parametrize("credential", [None, "Bearer invalid-token", "disabled"])
def test_all_payment_operations_require_valid_active_account(client, fake_db, method, path, body, credential):
    headers = bearer("disabled-user") if credential == "disabled" else (
        {"Authorization": credential} if credential else {})
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == (403 if credential == "disabled" else 401)
    assert not fake_db.queries("tuloy_beneficiary_tbl")
    assert not fake_db.queries("payment_transaction")
    assert fake_db.commits == 0


@pytest.mark.parametrize("method,path,body", OPERATIONS)
@pytest.mark.parametrize("grants", [
    [],
    [(SCOPE_VERIFY, "CHED", "CHED", "active")],
    [(SCOPE_VERIFY, "TESDA", "TESDA", "active")],
    [(SCOPE_VERIFY, "DSWD", "TEST", "active")],
    [(SCOPE_VERIFY, "CHED", "DSWD", "active")],
    [(SCOPE_VERIFY, "DSWD", "DSWD", "expired")],
    [(SCOPE_VERIFY, "TEST", "TEST", "revoked")],
    [("unknown:scope", "DSWD", "DSWD", "active")],
])
def test_other_or_inactive_bindings_cannot_reach_payment_data(client, fake_db, method, path, body, grants):
    fake_db.scopes[4] = grants
    response = client.request(method, path, json=body, headers=bearer("no-grants"))
    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    assert not fake_db.queries("tuloy_beneficiary_tbl")
    assert not fake_db.queries("payment_transaction")
    assert fake_db.commits == 0


@pytest.mark.parametrize("agency", ["DSWD", "TEST"])
@pytest.mark.parametrize("state", ["active", "never"])
def test_matching_active_agency_can_read_rates(client, fake_db, agency, state):
    fake_db.scopes[4] = [(SCOPE_VERIFY, agency, agency, state)]
    response = client.get("/api/payments/rates", headers=bearer("no-grants"))
    assert response.status_code == 200
    assert set(response.json()) == {"CHED", "TESDA"}


@pytest.mark.parametrize("agency", ["DSWD", "TEST"])
@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_grant_changes_take_effect_with_same_token(client, fake_db, agency, state):
    headers = bearer("no-grants")
    fake_db.scopes[4] = [(SCOPE_VERIFY, agency, agency, "active")]
    assert client.get("/api/payments/rates", headers=headers).status_code == 200
    fake_db.scopes[4] = [(SCOPE_VERIFY, agency, agency, state)]
    assert client.get("/api/payments/rates", headers=headers).status_code == 403


def test_token_agency_claims_cannot_replace_database_grants(client):
    response = client.get("/api/payments/rates", headers=bearer("no-grants", agency="DSWD", partner_id="DSWD"))
    assert response.status_code == 403


def test_grant_lookup_failure_denies_access(client, fake_db):
    fake_db.fail(db_error(2006), "FROM api_user_scopes")
    assert client.get("/api/payments/rates", headers=bearer()).status_code == 503


def test_payments_openapi_declares_bearer_auth(client, fake_db):
    from scopes import DOCS_INTERNAL
    fake_db.scopes[4] = [(DOCS_INTERNAL, "DSWD")]
    spec = client.get("/tuloydswd/openapi.json", headers=bearer("no-grants")).json()
    for method, path, _ in OPERATIONS:
        operation = spec["paths"][path.split("?")[0]][method.lower()]
        assert {"BearerAuth": []} in operation["security"]
