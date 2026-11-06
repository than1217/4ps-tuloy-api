import uuid

import pytest

import main
from conftest import bearer
from partner.router import PARTNER_OPERATIONS, PREFIX

VERIFY = {"first_name": "Juan", "last_name": "Dela Cruz", "date_of_birth": "2000-01-01"}
IDEM = {"Idempotency-Key": str(uuid.uuid4())}
SUBMIT_CHED = {
    "tuloy_id": "synthetic-tuloy-1", "student_id": "synthetic-student-1", "enrollment_status": 1,
    "date_encoded": "2026-01-31T08:30:00+08:00",
    "degree": {"program_name": "Synthetic Program", "date_enrolled": "2026-01-15", "program_duration": 4,
               "academic_year": "2026-2027", "year_level": 1, "semester": 1},
}
OPS = {
    ("POST", f"{PREFIX}/members/verify"): ("tuloy:member:verify", {"json": VERIFY}),
    ("POST", f"{PREFIX}/enrollments"): ("tuloy:enrollment:submit", {"headers": IDEM, "json": SUBMIT_CHED}),
    ("GET", f"{PREFIX}/enrollments"): ("tuloy:enrollment:read", {}),
    ("GET", f"{PREFIX}/enrollments/txn-1"): ("tuloy:enrollment:read", {}),
    ("POST", f"{PREFIX}/enrollments/txn-1/events"): ("tuloy:enrollment:update", {"headers": IDEM}),
    ("GET", f"{PREFIX}/members/4ps-0001/enrollment-history"): ("tuloy:history:read", {}),
}


def call(client, method, path, headers=None, extra=None):
    extra = dict(extra or {})
    merged = {**(headers or {}), **extra.pop("headers", {})}
    return client.request(method, path, headers=merged, **extra)


def test_openapi_has_exactly_the_six_partner_operations():
    spec = main.app.openapi()
    partner = sorted((m.upper(), p) for p, ops in spec["paths"].items() for m in ops if p.startswith(PREFIX))
    assert partner == sorted(PARTNER_OPERATIONS)
    assert len(partner) == 6
    ids = {op["operationId"] for p, ops in spec["paths"].items() if p.startswith(PREFIX) for op in ops.values()}
    assert len(ids) == 6


def test_legacy_routes_still_published():
    paths = main.app.openapi()["paths"]
    for legacy in ("/token", "/VerifyMemberBene", "/GetEligibleBene", "/PostEnrolleeMatchedCHED", "/GetMatchedChedBene"):
        assert legacy in paths
    # /signup still answers (404 unless ALLOW_SIGNUP) but is left out of the docs while disabled.
    assert "/signup" not in paths


@pytest.mark.parametrize("op", list(OPS))
def test_unauthenticated_is_401(client, op):
    method, path = op
    assert call(client, method, path, extra=OPS[op][1]).status_code == 401


@pytest.mark.parametrize("op", list(OPS))
def test_missing_scope_is_403(client, fake_db, op):
    method, path = op
    fake_db.scopes[1] = [("tuloy:unrelated", "CHED")]
    r = call(client, method, path, headers=bearer(), extra=OPS[op][1])
    assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN"


@pytest.mark.parametrize("op", list(OPS))
def test_scope_from_another_user_does_not_apply(client, fake_db, op):
    method, path = op
    fake_db.scopes[2] = [(OPS[op][0], "TESDA")]  # partner-b holds it, partner-a does not
    assert call(client, method, path, headers=bearer("partner-a"), extra=OPS[op][1]).status_code == 403


@pytest.mark.parametrize("op", [op for op in OPS if not op[1].endswith("/members/verify")])
def test_with_scope_returns_501_until_contract_approved(client, fake_db, op):
    method, path = op
    fake_db.scopes[1] = [(OPS[op][0], "CHED", "CHED", "active")]  # G04: partner_id equals agency
    r = call(client, method, path, headers=bearer(), extra=OPS[op][1])
    assert r.status_code == 501 and r.json()["error_code"] == "NOT_IMPLEMENTED"
    assert not fake_db.queries("INSERT")


@pytest.mark.parametrize("rows,status", [
    ([], "Not Found"),
    ([{"tuloy_id": "synthetic-1", "member_id": 123}], "Matched"),
    ([{"tuloy_id": "synthetic-1", "member_id": 123},
      {"tuloy_id": "synthetic-2", "member_id": 456}], "Multiple Match Found"),
])
def test_partner_verify_matches_without_legacy_scope(client, fake_db, rows, status):
    fake_db.scopes[1] = [("tuloy:member:verify", "CHED")]
    fake_db.on(r"FROM vw_verify_member_bene", lambda sql, p: rows)
    response = client.post(f"{PREFIX}/members/verify", headers=bearer(), json={**VERIFY, "household_id": "synthetic-household"})
    assert response.status_code == 200
    assert response.json() == {"status": status,
                               "tuloy_id": "synthetic-1" if status == "Matched" else None,
                               "member_id": "123" if status == "Matched" else None}  # partners always get text
    sql, params = fake_db.queries("FROM vw_verify_member_bene")[0]
    assert "`listahanan_id` = %s" in sql and "LIMIT 2" in sql
    assert params == (*VERIFY.values(), "synthetic-household")
    assert not fake_db.queries("INSERT")


def test_partner_verify_does_not_ignore_suffix(client, fake_db):
    fake_db.scopes[1] = [("tuloy:member:verify", "CHED")]
    response = client.post(f"{PREFIX}/members/verify", headers=bearer(), json={**VERIFY, "suffix": "Jr."})
    assert response.status_code == 422
    assert not fake_db.queries("FROM vw_verify_member_bene")


def test_partner_verify_database_failure(client, fake_db):
    from conftest import db_error
    fake_db.scopes[1] = [("tuloy:member:verify", "CHED")]
    fake_db.fail(db_error(3024), "vw_verify_member_bene")
    response = client.post(f"{PREFIX}/members/verify", headers=bearer(), json=VERIFY)
    assert response.status_code == 503


def test_writes_require_idempotency_key(client, fake_db):
    fake_db.scopes[1] = [("tuloy:enrollment:submit", "CHED"), ("tuloy:enrollment:update", "CHED")]
    assert client.post(f"{PREFIX}/enrollments", headers=bearer()).status_code == 422
    assert client.post(f"{PREFIX}/enrollments", headers={**bearer(), "Idempotency-Key": "not-a-uuid"}).status_code == 422
    assert client.post(f"{PREFIX}/enrollments/txn-1/events", headers=bearer()).status_code == 422


def test_verify_input_validation(client, fake_db):
    fake_db.scopes[1] = [("tuloy:member:verify", "CHED")]
    bad_bodies = [
        {**VERIFY, "unexpected": "x"},          # unknown fields rejected
        {**VERIFY, "date_of_birth": "2020-02-30"},
        {**VERIFY, "lrn": "12345"},
        {"first_name": "Juan"},
    ]
    for body in bad_bodies:
        r = client.post(f"{PREFIX}/members/verify", json=body, headers=bearer())
        assert r.status_code == 422, body
        assert "Dela Cruz" not in r.text


def test_path_and_query_bounds(client, fake_db):
    fake_db.scopes[1] = [("tuloy:enrollment:read", "CHED"), ("tuloy:history:read", "CHED")]
    assert client.get(f"{PREFIX}/enrollments/{'a' * 65}", headers=bearer()).status_code == 422
    assert client.get(f"{PREFIX}/enrollments/abc%27%20OR%201=1", headers=bearer()).status_code == 422
    assert client.get(f"{PREFIX}/enrollments?limit=101", headers=bearer()).status_code == 422
    assert client.get(f"{PREFIX}/enrollments?after=%3Cscript%3E", headers=bearer()).status_code == 422


def test_members_verify_with_non_expiring_grant_and_without(client, fake_db):
    """Partner API switched on (PARTNER_API_ENABLED=true in conftest): a grant opens the route, none is 403.

    members/verify is implemented in this tree (existing beneficiary lookup), so it answers 200 here,
    not 501; the other five operations still answer 501 (test above). Synthetic input only.
    """
    path = f"{PREFIX}/members/verify"
    assert client.post(path, headers=bearer("no-grants"), json=VERIFY).status_code == 403
    fake_db.scopes[4] = [("tuloy:member:verify", "TESDA", "TESDA", "never")]
    assert client.post(path, headers=bearer("no-grants"), json=VERIFY).status_code == 200
    fake_db.scopes[4] = [("tuloy:member:verify", "TESDA", "TESDA", "expired")]
    assert client.post(path, headers=bearer("no-grants"), json=VERIFY).status_code == 403
