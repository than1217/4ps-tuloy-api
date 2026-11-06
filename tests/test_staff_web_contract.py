"""Staff web app reads under /v1/api/tuloy/staff: the prefix the staging web proxy forwards.

The list and settings routes reuse the root legacy handlers, so these tests check that the alias
behaves exactly like its root route (scope, rulings, paging, errors) rather than re-testing the handler.
"""
import re

from conftest import bearer, db_error
from scopes import LEGACY_BENEFICIARY_READ, LEGACY_CHED_READ, SCOPE_VERIFY

STAFF = "/v1/api/tuloy/staff"


def _rows(fake_db):
    full = {"id": 7, "first_name": "Juan", "tuloy_id": "4ps-0001", "lrn": "100000005678",
            "mm_first_name": "Reyes", "m_maiden_name": "Reyes", "account_no": "000111"}

    def like_mysql(sql, params):  # only the columns the SELECT asks for, `limit + 1` rows
        cols = re.search(r"SELECT (.*?) FROM", sql).group(1).replace("`", "").split(",")
        return [{c: full.get(c) for c in cols} for _ in range(params[0])]
    fake_db.on(r"FROM vw_unmatched_member_bene", like_mysql)
    fake_db.on(r"FROM vw_get_matched_CHED", like_mysql)


def test_eligible_alias_equals_root_route(client, fake_db):
    _rows(fake_db)
    alias = client.get(f"{STAFF}/eligible-beneficiaries?limit=2&offset=4", headers=bearer())
    root = client.get("/GetEligibleBene?limit=2&offset=4", headers=bearer())
    assert alias.status_code == root.status_code == 200
    assert alias.json() == root.json()
    body = alias.json()
    assert set(body) == {"items", "limit", "offset", "has_more", "next_offset"}
    assert body["has_more"] and body["next_offset"] == 6 and len(body["items"]) == 2
    item = body["items"][0]
    assert item["lrn"] == "********5678" and "m_maiden_name" not in item and "account_no" not in item


def test_matched_alias_equals_root_route_and_hides_maiden_name(client, fake_db):
    _rows(fake_db)
    alias = client.get(f"{STAFF}/matched-ched-beneficiaries?limit=1", headers=bearer())
    assert alias.status_code == 200
    assert alias.json() == client.get("/GetMatchedChedBene?limit=1", headers=bearer()).json()
    assert not any(k.startswith("mm_") for k in alias.json()["items"][0])


def test_alias_paging_bounds_are_the_same(client):
    for path in ("eligible-beneficiaries", "matched-ched-beneficiaries"):
        for bad in ("limit=0", "limit=1001", "offset=-1", "limit=abc"):
            assert client.get(f"{STAFF}/{path}?{bad}", headers=bearer()).status_code == 422


def test_aliases_need_the_same_scopes(client, fake_db):
    fake_db.scopes[4] = [(SCOPE_VERIFY, "CHED")]
    for path in ("eligible-beneficiaries", "matched-ched-beneficiaries", "overview"):
        r = client.get(f"{STAFF}/{path}", headers=bearer("no-grants"))
        assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN", path


def test_ched_bound_grant_opens_nothing(client, fake_db):
    # Data-owner rulings: the bulk reads are DSWD only, whatever the grant says.
    fake_db.scopes[4] = [(LEGACY_BENEFICIARY_READ, "CHED"), (LEGACY_CHED_READ, "CHED")]
    for path in ("eligible-beneficiaries", "matched-ched-beneficiaries", "overview"):
        assert client.get(f"{STAFF}/{path}", headers=bearer("no-grants")).status_code == 403, path


def test_unauthenticated_requests_are_401(client):
    for path in ("me", "eligible-beneficiaries", "matched-ched-beneficiaries", "overview", "settings"):
        r = client.get(f"{STAFF}/{path}")
        assert r.status_code == 401 and r.json()["error_code"] == "UNAUTHENTICATED", path
        assert client.get(f"{STAFF}/{path}", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_settings_alias_is_read_only(client):
    r = client.get(f"{STAFF}/settings", headers=bearer("no-grants"))
    assert r.status_code == 200 and r.json() == client.get("/GetAppSettings", headers=bearer("no-grants")).json()
    assert client.post(f"{STAFF}/settings", headers=bearer(), json={}).status_code == 405


def test_overview_counts_and_recent(client, fake_db):
    fake_db.on(r"COUNT\(\*\) AS `n` FROM enroll_matched_CHED_tbl", lambda sql, p: [{"n": 12}])
    fake_db.on(r"COUNT\(\*\) AS `n` FROM matching_transaction_CHED_tbl", lambda sql, p: [{"n": 30}])
    _rows(fake_db)
    r = client.get(f"{STAFF}/overview", headers=bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["matched_ched_count"] == 12 and body["matching_txn_count"] == 30
    assert body["eligible_beneficiaries_count"] is None
    assert len(body["recent_matched_ched"]) == 10
    assert not any(k.startswith("mm_") for k in body["recent_matched_ched"][0])
    sql, params = fake_db.queries("FROM vw_get_matched_CHED")[0]
    assert "ORDER BY `id` DESC LIMIT %s" in sql and params == (10,)
    assert not fake_db.queries("vw_unmatched_member_bene") and not fake_db.queries("vw_verify_member_bene")


def test_overview_db_errors_are_generic(client, fake_db):
    fake_db.fail(db_error(3024), "enroll_matched_CHED_tbl")
    r = client.get(f"{STAFF}/overview", headers=bearer())
    assert r.status_code == 503 and r.json()["error_code"] == "DB_TIMEOUT" and "tbl_person" not in r.text


def test_staff_routes_stay_out_of_the_openapi_document():
    import main
    paths = main.app.openapi()["paths"]
    assert "/GetEligibleBene" in paths  # the document is real, not empty
    assert not [p for p in paths if p.startswith(STAFF)]
