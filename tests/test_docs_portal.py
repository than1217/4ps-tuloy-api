import pytest

from conftest import bearer
from scopes import DOCS_INTERNAL

PARTNER_PREFIX = "/v1/api/tuloy"
INTERNAL_PATHS = (
    "/VerifyMemberBene",
    "/GetEligibleBene",
    "/PostEnrolleeMatchedCHED",
    "/GetMatchedChedBene",
    "/GetUnifiedMatchedBene",
    "/GetAppSettings",
    "/PostAppSettings",
)
PAYMENT_OPERATIONS = {
    "/api/payments/rates": "get",
    "/api/payments/beneficiaries": "get",
    "/api/payments/compute": "post",
    "/api/payments/generate": "post",
    "/api/payments/export/csv": "get",
    "/api/payments/export/summary": "get",
    "/api/payments/export/pdf": "get",
}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_default_docs_are_off(client, path):
    assert client.get(path).status_code == 404


def test_partner_docs_page_is_public_and_self_hosted(client):
    r = client.get("/tuloy/docs")
    assert r.status_code == 200 and "<elements-api" in r.text and "/tuloy/openapi.json" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "unpkg.com" not in r.text and "jsdelivr" not in r.text


def test_partner_spec_has_only_authentication_and_partner_api(client):
    spec = client.get("/tuloy/openapi.json").json()
    paths = set(spec["paths"])
    assert "/token" in paths and "/signup" not in paths
    assert {p for p in paths if p != "/token"} and all(p.startswith(PARTNER_PREFIX) for p in paths - {"/token"})
    assert not paths & set(INTERNAL_PATHS)
    assert not paths & set(PAYMENT_OPERATIONS)
    text = str(spec)
    for leak in ("UserInput", "ChedResponsePayload", "VerifyResult", "Beneficiaries", "tuloy:legacy",
                 "PaymentComputeRequest", "PaymentGenerateRequest"):
        assert leak not in text, leak


def test_internal_docs_page_is_a_shell_without_api_content(client):
    r = client.get("/tuloydswd/docs")
    assert r.status_code == 200
    for leak in INTERNAL_PATHS + tuple(PAYMENT_OPERATIONS) + ("apiDescriptionUrl", "Beneficiaries"):
        assert leak not in r.text, leak


def test_internal_spec_requires_sign_in(client):
    assert client.get("/tuloydswd/openapi.json").status_code == 401


def test_internal_spec_requires_docs_grant(client, fake_db):
    # partner-a holds the legacy route scopes but not the docs grant.
    assert client.get("/tuloydswd/openapi.json", headers=bearer()).status_code == 403


def test_internal_spec_with_docs_grant(client, fake_db):
    fake_db.scopes[4] = [(DOCS_INTERNAL, "DSWD")]
    spec = client.get("/tuloydswd/openapi.json", headers=bearer("no-grants")).json()
    paths = set(spec["paths"])
    assert {"/token", *INTERNAL_PATHS} <= paths and "/signup" not in paths
    assert any(p.startswith(PARTNER_PREFIX) for p in paths)
    assert spec["paths"]["/GetAppSettings"]["get"]["tags"] == ["System"]
    assert spec["paths"]["/PostAppSettings"]["post"]["tags"] == ["System"]
    for path, method in PAYMENT_OPERATIONS.items():
        assert spec["paths"][path][method]["tags"] == ["Payments"]
    schemas = spec["components"]["schemas"]
    for name in ("PaymentComputeRequest", "PaymentGenerateRequest"):
        assert name in schemas


def test_internal_docs_include_actual_bcmd_napa_routes_without_leaking_to_partner(client, fake_db):
    import main
    prefix = '/v1/api/tuloy/staff/bcmd'
    before = client.get('/tuloy/openapi.json').json()
    fake_db.scopes[4] = [(DOCS_INTERNAL, 'DSWD')]
    response = client.get('/tuloydswd/openapi.json', headers=bearer('no-grants'))
    assert response.status_code == 200
    spec = response.json()
    operations = {prefix+'/status': ['get'], prefix+'/candidates': ['get'],
                  prefix+'/batches': ['get', 'post'], prefix+'/batches/{public_id}': ['get'],
                  prefix+'/batches/{public_id}/{section}': ['get'],
                  prefix+'/batches/{public_id}/export': ['get']}
    for path, methods in operations.items():
        for method in methods:
            operation = spec['paths'][path][method]
            assert operation['tags'] == ['BCMD/NAPA']
            assert operation['security'] == [{'BearerAuth': []}]
            assert operation['summary'] and operation['description']
            assert {'401', '403', '503'} <= operation['responses'].keys()
    create = spec['paths'][prefix+'/batches']['post']
    key = next(p for p in create['parameters'] if p['name'].lower() == 'idempotency-key')
    assert key['required'] and key['schema']['format'] == 'uuid'
    assert {'201', '409', '422'} <= create['responses'].keys()
    assert spec['components']['schemas']['CreateBatch']['examples'][0]['category'] == 'TK'
    export = spec['paths'][prefix+'/batches/{public_id}/export']['get']['responses']
    assert '200' in export and '501' not in export
    assert prefix+'/batches/{public_id}/exports/{export_no}/download' in spec['paths']
    section = spec['paths'][prefix+'/batches/{public_id}/{section}']['get']
    choices = next(p for p in section['parameters'] if p['name'] == 'section')['schema']['enum']
    assert choices == ['members', 'issues', 'audit']
    member = section['responses']['200']['content']['application/json']['examples']['members']['value']['items'][0]
    assert member['family_id'] is None and member['ip_indicator'] is None
    assert client.get('/tuloy/openapi.json').json() == before
    assert not any(path.startswith(prefix) for path in main.app.openapi()['paths'])
    assert not fake_db.queries('bcmd_batch')  # Generating docs executes no business operation.


def test_internal_navigation_follows_requested_order_without_changing_partner_docs(client, fake_db):
    fake_db.scopes[4] = [(DOCS_INTERNAL, 'DSWD')]
    partner = client.get('/tuloy/openapi.json').json()
    spec = client.get('/tuloydswd/openapi.json', headers=bearer('no-grants')).json()
    expected = ['Authentication', 'Partner API', 'Beneficiary', 'Payments', 'BCMD/NAPA', 'System']
    assert [tag['name'] for tag in spec['tags']] == expected
    groups = []
    for operations in spec['paths'].values():
        group = next(iter(operations.values()))['tags'][0]
        if not groups or group != groups[-1]:
            groups.append(group)
    assert groups == expected
    assert spec['info']['title'] == 'Tuloy API (DSWD internal)'  # Built-in Overview remains the info page.
    assert client.get('/tuloy/openapi.json').json() == partner


def test_app_settings_get_and_post(client, fake_db):
    r_get = client.get("/GetAppSettings", headers=bearer())
    assert r_get.status_code == 200
    data = r_get.json()
    assert data["app_name"] == "Tuloy Kulehiyo"
    assert len(data["logos"]) == 2

    payload = {
        **data,
        "app_name": "Tuloy Custom Portal",
        "app_tagline": "DSWD · Pantawid",
    }
    # Writes are off by default: refused after authentication, before any database statement.
    r_post = client.post("/PostAppSettings", json=payload, headers=bearer())
    assert r_post.status_code == 403 and r_post.json()["error_code"] == "SETTINGS_WRITE_DISABLED"
    assert fake_db.commits == 0 and not fake_db.queries("UPDATE app_settings")
    assert client.post("/PostAppSettings", json=payload).status_code == 401


def test_app_settings_post_when_writes_enabled(client, fake_db, monkeypatch):
    import dataclasses
    from config import get_settings
    from routers import system
    monkeypatch.setattr(system, "get_settings", lambda: dataclasses.replace(get_settings(), settings_write_enabled=True))
    payload = {**client.get("/GetAppSettings", headers=bearer()).json(), "app_name": "Tuloy Custom Portal"}
    r_post = client.post("/PostAppSettings", json=payload, headers=bearer())
    assert r_post.status_code == 200
    assert r_post.json()["app_name"] == "Tuloy Custom Portal"
    assert fake_db.commits == 1



def test_docs_grant_opens_no_api_route(client, fake_db):
    fake_db.scopes[4] = [(DOCS_INTERNAL, "DSWD")]
    r = client.post("/VerifyMemberBene", headers=bearer("no-grants"),
                    json={"first_name": "Zz", "last_name": "Synthetic", "date_of_birth": "1900-01-01"})
    assert r.status_code == 403


def test_vendor_assets_served(client):
    assert client.get("/docs-assets/vendor/elements-9.0.28/web-components.min.js").status_code == 200
    assert client.get("/docs-assets/internal.js").status_code == 200
    assert client.get("/docs-assets/../main.py").status_code == 404
