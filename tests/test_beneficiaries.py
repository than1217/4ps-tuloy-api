import re

import pytest

from conftest import bearer, db_error
from scopes import LEGACY_MEMBER_VERIFY, SCOPE_VERIFY

VERIFY_BODY = {"first_name": "Juan", "last_name": "Dela Cruz", "date_of_birth": "2000-01-01"}


def test_verify_statuses(client, fake_db):
    rows = []
    fake_db.on(r"FROM vw_verify_member_bene", lambda sql, p: rows)
    assert client.post("/VerifyMemberBene", json=VERIFY_BODY, headers=bearer()).json() == {
        "status": "Not Found", "tuloy_id": None, "member_id": None}
    rows[:] = [{"tuloy_id": "4ps-0001", "member_id": 1001}]
    assert client.post("/VerifyMemberBene", json=VERIFY_BODY, headers=bearer()).json() == {
        "status": "Matched", "tuloy_id": "4ps-0001", "member_id": 1001}
    rows[:] = [{"tuloy_id": "a", "member_id": 1}, {"tuloy_id": "b", "member_id": 2}]
    assert client.post("/VerifyMemberBene", json=VERIFY_BODY, headers=bearer()).json()["status"] == "Multiple Match Found"


def test_verify_is_parameterized_and_bounded(client, fake_db):
    hostile = "x' OR '1'='1'; DROP TABLE api_users; --"
    body = {**VERIFY_BODY, "first_name": hostile, "lrn": "1 OR 1=1", "geocode": "`; SELECT"}
    r = client.post("/VerifyMemberBene", json=body, headers=bearer())
    assert r.status_code == 200
    sql, params = fake_db.queries("vw_verify_member_bene")[0]
    assert hostile not in sql and "1 OR 1=1" not in sql and "`; SELECT" not in sql
    assert hostile in params and "1 OR 1=1" in params
    assert "AND `lrn` = %s" in sql and "AND `bgy_psgc_code` = %s" in sql
    assert sql.rstrip().endswith("LIMIT 2") and "SELECT *" not in sql


def test_verify_rejects_bad_dates_without_echo(client, fake_db):
    for dob in ("2020-02-30", "01/15/1990", "1990-1-5", ""):
        r = client.post("/VerifyMemberBene", json={**VERIFY_BODY, "date_of_birth": dob}, headers=bearer())
        assert r.status_code == 422, dob
        assert r.json()["error_code"] == "VALIDATION_ERROR"
        assert "Dela Cruz" not in r.text and (not dob or dob not in r.text)
    assert not fake_db.queries("vw_verify_member_bene")


def test_verify_rejects_oversized_input(client):
    r = client.post("/VerifyMemberBene", json={**VERIFY_BODY, "first_name": "A" * 101}, headers=bearer())
    assert r.status_code == 422


def test_eligible_bene_omits_maiden_name_and_account_no(client, fake_db):
    full = {"first_name": "Juan", "tuloy_id": "4ps-0001", "m_maiden_name": "Reyes", "account_no": "000111"}

    def like_mysql(sql, params):  # return only the columns the SELECT asks for
        cols = re.search(r"SELECT (.*?) FROM", sql).group(1).replace("`", "").split(",")
        return [{c: full.get(c) for c in cols}]
    fake_db.on(r"FROM vw_unmatched_member_bene WHERE `member_id` NOT IN", like_mysql)
    r = client.get("/GetEligibleBene?limit=1", headers=bearer())
    sql, _ = fake_db.queries("vw_unmatched_member_bene")[0]
    assert "m_maiden_name" not in sql and "account_no" not in sql
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert "m_maiden_name" not in item and "account_no" not in item
    assert item["first_name"] == "Juan" and item["tuloy_id"] == "4ps-0001"


def test_db_errors_are_generic(client, fake_db):
    fake_db.fail(db_error(1146), "vw_verify_member_bene")
    r = client.post("/VerifyMemberBene", json=VERIFY_BODY, headers=bearer())
    assert r.status_code == 500 and r.json()["error_code"] == "DB_ERROR"
    assert "tbl_person" not in r.text and "10.1.2.3" not in r.text
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


def test_db_timeout_maps_to_503(client, fake_db):
    fake_db.fail(db_error(3024), "vw_verify_member_bene")
    r = client.post("/VerifyMemberBene", json=VERIFY_BODY, headers=bearer())
    assert r.status_code == 503 and r.json()["error_code"] == "DB_TIMEOUT"


def test_list_pagination_is_bounded(client, fake_db):
    fake_db.on(r"FROM vw_get_matched_CHED", lambda sql, p: [{"id": i} for i in range(p[0])])
    r = client.get("/GetMatchedChedBene?limit=2&offset=4", headers=bearer())
    body = r.json()
    assert body["has_more"] and body["next_offset"] == 6 and len(body["items"]) == 2
    sql, params = fake_db.queries("vw_get_matched_CHED")[0]
    assert "ORDER BY `id`" in sql and params == (3, 4)
    for bad in ("limit=0", "limit=1001", "offset=-1", "limit=abc"):
        assert client.get(f"/GetMatchedChedBene?{bad}", headers=bearer()).status_code == 422


def test_ched_response_writes_in_one_transaction(client, fake_db):
    payload = {"status": True, "message": "FOUND", "enrollment_id": 0,
               "data": {"id": "4ps-0001", "member_id": 1001, "student_id": 5,
                        "degree": {"program_name": "BS Synthetic", "date_enrolled": "", "year_level": 1}}}
    r = client.post("/PostEnrolleeMatchedCHED", json=payload, headers=bearer())
    assert r.status_code == 200
    assert len(fake_db.queries("INSERT INTO matching_transaction_CHED_tbl")) == 1
    _, params = fake_db.queries("INSERT INTO enroll_matched_CHED_tbl")[0]
    assert params[0] == fake_db.next_id - 1  # match_id links to the transaction row
    assert params[9] is None  # blank date stored as NULL
    assert fake_db.commits == 1


def test_ched_response_rolls_back_on_error(client, fake_db):
    fake_db.fail(db_error(1452), "enroll_matched_CHED_tbl")
    payload = {"status": True, "message": "FOUND", "data": {"id": "4ps-0001", "member_id": 1}}
    r = client.post("/PostEnrolleeMatchedCHED", json=payload, headers=bearer())
    assert r.status_code == 500 and fake_db.commits == 0 and fake_db.rollbacks >= 1


def test_ched_not_found_only_writes_transaction(client, fake_db):
    r = client.post("/PostEnrolleeMatchedCHED", json={"status": False, "message": "NOT FOUND"}, headers=bearer())
    assert r.status_code == 200
    assert fake_db.queries("matching_transaction_CHED_tbl") and not fake_db.queries("enroll_matched_CHED_tbl")


def test_eligible_bene_masks_lrn_and_pcn_to_last_4(client, fake_db):
    fake_db.on(r"FROM vw_unmatched_member_bene WHERE `member_id` NOT IN",
               lambda sql, params: [{"tuloy_id": "4ps-0001", "lrn": "123456789012", "philsys": "1234-5678-9012"},
                                    {"tuloy_id": "4ps-0002", "lrn": None, "philsys": "123"}])
    items = client.get("/GetEligibleBene?limit=5", headers=bearer()).json()["items"]
    assert items[0]["lrn"] == "********9012" and items[0]["philsys"] == "****-****-9012"
    assert items[1]["lrn"] is None and items[1]["philsys"] == "123"  # 4 digits or fewer: nothing to hide


@pytest.mark.parametrize("errno,status,code", [
    (3024, 503, "DB_TIMEOUT"), (2003, 503, "DB_UNAVAILABLE"), (2006, 503, "DB_UNAVAILABLE"),
    (2013, 503, "DB_UNAVAILABLE"), (1146, 500, "DB_ERROR"), (1054, 500, "DB_ERROR"),
])
@pytest.mark.parametrize("path,scope", [
    ("/VerifyMemberBene", LEGACY_MEMBER_VERIFY), ("/v1/api/tuloy/members/verify", SCOPE_VERIFY),
])
def test_verify_db_failure_status_follows_contract(client, fake_db, path, scope, errno, status, code):
    """V11: outages and timeouts are 503; unexpected query or schema errors are 500, on both routes."""
    fake_db.scopes[1] = [(scope, "DSWD")]
    fake_db.fail(db_error(errno), "vw_verify_member_bene")
    r = client.post(path, json=VERIFY_BODY, headers=bearer())
    assert r.status_code == status and r.json()["error_code"] == code
    assert "tbl_person" not in r.text and "10.1.2.3" not in r.text


def test_matched_ched_omits_mothers_maiden_name(client, fake_db):
    """Data-owner rulings: R11 never returns mother's maiden name (ADR 2026-10-27, QA-14)."""
    full = {"id": 1, "tuloy_id": "4ps-0001", "first_name": "Juan", "mm_first_name": "Maria",
            "mm_middle_name": "Santos", "mm_last_name": "Reyes"}

    def like_mysql(sql, params):
        cols = re.search(r"SELECT (.*?) FROM", sql).group(1).replace("`", "").split(",")
        return [{c: full.get(c) for c in cols}]
    fake_db.on(r"FROM vw_get_matched_CHED", like_mysql)
    r = client.get("/GetMatchedChedBene?limit=1", headers=bearer())
    sql, _ = fake_db.queries("vw_get_matched_CHED")[0]
    assert r.status_code == 200 and "mm_" not in sql
    assert not any(k.startswith("mm_") for k in r.json()["items"][0]) and "Maria" not in r.text


@pytest.mark.parametrize('path', ['/GetUnifiedMatchedBene', '/v1/api/tuloy/staff/unified-matched-beneficiaries'])
def test_unified_matched_bene_pagination_and_json_details(client, fake_db, path):
    fake_db.on(
        r"FROM vw_unified_matched_enrollment",
        lambda sql, p: [
            {
                "agency_name": "CHED",
                "tuloy_id": f"4ps-{i:04d}",
                "member_id": 1000 + i,
                "enrollment_details": '{"degree": {"course_type": "Degree", "program_name": "BS Synthetic"}}',
                "created_at": "2026-10-30T00:00:00",
                "updated_at": None,
            }
            for i in range(p[0])
        ],
    )
    r = client.get(path+"?limit=2&offset=4", headers=bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] and body["next_offset"] == 6 and len(body["items"]) == 2
    assert body["items"][0]["enrollment_details"] == {
        "degree": {"course_type": "Degree", "program_name": "BS Synthetic"}
    }
    sql, params = fake_db.queries("vw_unified_matched_enrollment")[0]
    assert params == (3, 4)
    assert 'ORDER BY BINARY agency_name,BINARY tuloy_id,member_id,created_at,updated_at' in sql
    assert 'CAST(enrollment_details AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_bin' in sql
    for bad in ("limit=0", "limit=1001", "offset=-1", "limit=abc"):
        assert client.get(f"{path}?{bad}", headers=bearer()).status_code == 422


@pytest.mark.parametrize('payload', ['not-json', '"private-synthetic-value"', b'\xff'])
def test_unified_invalid_source_is_sanitized(client, fake_db, payload):
    fake_db.on(r'FROM vw_unified_matched_enrollment', lambda sql, p: [{
        'agency_name': 'CHED', 'tuloy_id': 'SYNTHETIC', 'member_id': 1001,
        'enrollment_details': payload, 'created_at': None, 'updated_at': None}])
    response = client.get('/GetUnifiedMatchedBene', headers=bearer())
    assert response.status_code == 503
    assert response.json()['error_code'] == 'UNIFIED_MATCHED_SOURCE_INVALID'
    assert 'private-synthetic-value' not in response.text


@pytest.mark.parametrize('path', ['/GetUnifiedMatchedBene', '/v1/api/tuloy/staff/unified-matched-beneficiaries'])
def test_unified_feed_rejects_anonymous_and_partner_accounts(client, fake_db, path):
    from scopes import LEGACY_CHED_READ
    assert client.get(path).status_code == 401
    for agency in ('CHED', 'TESDA', 'TEST'):
        fake_db.scopes[4] = [(LEGACY_CHED_READ, agency, agency, 'active')]
        assert client.get(path, headers=bearer('no-grants')).status_code == 403
    assert not fake_db.queries('FROM vw_unified_matched_enrollment')


@pytest.mark.parametrize('kind,fields', [
    ('degree', {'student_id': 'SYN-STUDENT', 'program_name': 'BS Synthetic', 'semester': 1}),
    ('diploma', {'uli_id': 'SYN-ULI', 'program_name': 'Synthetic Diploma', 'year_level': 1}),
    ('short_course', {'uli_id': 'SYN-ULI', 'program_name': 'Synthetic Training', 'start_date': '2026-10-01'}),
])
def test_unified_course_fields_exclude_unreviewed_personal_data(client, fake_db, kind, fields):
    import json
    private = 'PRIVATE-SYNTHETIC-DO-NOT-RETURN'
    details = {kind: {**fields, 'mothers_maiden': private, 'mm_first_name': private,
                     'lrn': private, 'philsys': private}, 'personal_details': {'name': private}}
    fake_db.on(r'FROM vw_unified_matched_enrollment', lambda sql, p: [{
        'agency_name': 'CHED' if kind == 'degree' else 'TESDA',
        'tuloy_id': 'SYNTHETIC', 'member_id': 1001, 'enrollment_details': json.dumps(details),
        'created_at': None, 'updated_at': None}])
    response = client.get('/GetUnifiedMatchedBene', headers=bearer())
    assert response.status_code == 200
    assert response.json()['items'][0]['enrollment_details'] == {kind: fields}
    assert private not in response.text


def test_unified_filters_are_bound_and_applied_before_pagination(client, fake_db):
    import re
    fake_db.on(r'FROM vw_unified_matched_enrollment', lambda sql, p: [])
    value = "x_%' OR 1=1 --"
    response = client.get('/v1/api/tuloy/staff/unified-matched-beneficiaries',
        params={'agency': 'TESDA', 'course_type': 'short_course', 'search': value, 'limit': 50, 'offset': 100},
        headers=bearer())
    assert response.status_code == 200
    sql, params = fake_db.queries('FROM vw_unified_matched_enrollment')[0]
    assert 'WHERE BINARY agency_name=BINARY %s' in sql
    assert 'JSON_CONTAINS_PATH' in sql
    assert value not in sql and params[:2] == ('TESDA', '$.short_course')
    assert params[-2:] == (51, 100)
    assert params[2] == "%x=_=%' OR 1==1 --%"
    assert sql.index('WHERE') < sql.index('ORDER BY') < sql.index('LIMIT')


@pytest.mark.parametrize('params', [{'agency': 'DSWD'}, {'course_type': 'unknown'}, {'search': 'x'*101}])
def test_unified_invalid_filters_never_read_view(client, fake_db, params):
    response = client.get('/v1/api/tuloy/staff/unified-matched-beneficiaries', params=params, headers=bearer())
    assert response.status_code == 422
    assert not fake_db.queries('FROM vw_unified_matched_enrollment')
