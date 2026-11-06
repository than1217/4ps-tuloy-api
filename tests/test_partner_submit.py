"""R3 POST /enrollments: the grant decides the agency, the body is strict, the end is still 501."""
import copy
import logging
import uuid

import pytest

import main
from conftest import bearer
from partner.router import PREFIX

URL = f"{PREFIX}/enrollments"
SUBMIT = "tuloy:enrollment:submit"
COMMON = {"tuloy_id": "synthetic-tuloy-1", "enrollment_status": 1, "date_encoded": "2026-01-31T08:30:00+08:00"}
CHED = {**COMMON, "student_id": "synthetic-student-1",
        "degree": {"program_name": "Synthetic Program", "date_enrolled": "2026-01-15", "program_duration": 4,
                   "academic_year": "2026-2027", "year_level": 1, "semester": 1}}
TESDA = {**COMMON, "uli": "synthetic-uli-1",
         "short_course": {"program_name": "Synthetic Course", "start_date": "2026-02-01", "end_date": "2026-03-01",
                          "training_status": "synthetic-status"}}
MARKERS = ("synthetic-tuloy-1", "synthetic-student-1", "synthetic-uli-1", "Synthetic Program", "Synthetic Course")


def post(client, body, headers=None):
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4()), **(headers or {})}
    return client.post(URL, headers=h, json=body)


def grant(fake_db, *rows):
    # G04: partner_id equals agency, so 2-tuples are expanded to (scope, agency, agency, "active").
    fake_db.scopes[1] = [(r[0], r[1], r[1], "active") if len(r) == 2 else r for r in rows]


def with_(base, **changes):
    out = copy.deepcopy(base)
    out.update(changes)
    return out


def nested(base, key, **changes):
    out = copy.deepcopy(base)
    out[key].update(changes)
    return out


def last_authz(caplog):
    return [x for x in caplog.records if x.name == "tuloy.audit" and x.action == "authz.scope"][-1]


def test_ched_valid_ends_in_501_and_persists_nothing(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    r = post(client, CHED)
    assert r.status_code == 501 and r.json()["error_code"] == "NOT_IMPLEMENTED"
    assert not fake_db.queries("INSERT") and not fake_db.queries("UPDATE")


def test_tesda_valid_ends_in_501(client, fake_db):
    grant(fake_db, (SUBMIT, "TESDA"))
    r = post(client, TESDA)
    assert r.status_code == 501 and r.json()["error_code"] == "NOT_IMPLEMENTED"
    assert not fake_db.queries("INSERT")


@pytest.mark.parametrize("extra", [{}, {"mothers_maiden": None}, {"date_updated": None},
                                   {"date_updated": "2026-02-01T00:00:00Z"}])
def test_optional_fields_accepted(client, fake_db, extra):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, {**CHED, **extra}).status_code == 501


@pytest.mark.parametrize("extra", [{"uli": "x"}, {"uli": None}, {"short_course": None},
                                   {"short_course": TESDA["short_course"]}])
def test_ched_account_rejects_tesda_fields(client, fake_db, extra):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, {**CHED, **extra}).status_code == 422


@pytest.mark.parametrize("extra", [{"degree": CHED["degree"]}, {"degree": None}, {"student_id": "x"},
                                   {"student_id": None}])
def test_tesda_account_rejects_ched_fields(client, fake_db, extra):
    grant(fake_db, (SUBMIT, "TESDA"))
    assert post(client, {**TESDA, **extra}).status_code == 422


def test_agency_sections_are_required(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, {k: v for k, v in CHED.items() if k != "degree"}).status_code == 422
    assert post(client, {k: v for k, v in CHED.items() if k != "student_id"}).status_code == 422
    grant(fake_db, (SUBMIT, "TESDA"))
    assert post(client, {k: v for k, v in TESDA.items() if k != "short_course"}).status_code == 422


def test_dswd_binding_is_denied(client, fake_db, caplog):
    grant(fake_db, (SUBMIT, "DSWD"))
    with caplog.at_level(logging.INFO, logger="tuloy.audit"):
        r = post(client, CHED)
    assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN"
    ev = last_authz(caplog)
    assert ev.outcome == "denied" and ev.reason == "payload_not_approved" and ev.agency == "DSWD"


def test_two_distinct_submit_grants_are_ambiguous(client, fake_db, caplog):
    grant(fake_db, (SUBMIT, "CHED"), (SUBMIT, "TESDA"))
    with caplog.at_level(logging.INFO, logger="tuloy.audit"):
        r = post(client, CHED)
    assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN"
    assert last_authz(caplog).reason == "ambiguous_binding"


def test_same_agency_with_two_partners_is_ambiguous(client, fake_db):
    grant(fake_db, (SUBMIT, "P-1", "CHED", "active"), (SUBMIT, "CHED", "CHED", "active"))
    assert post(client, CHED).status_code == 403


def test_duplicate_identical_grant_rows_are_one_binding(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED", "CHED", "active"), (SUBMIT, "CHED", "CHED", "never"))
    assert post(client, CHED).status_code == 501


def test_partner_not_equal_to_agency_is_unapproved(client, fake_db, caplog):
    grant(fake_db, (SUBMIT, "P-1", "CHED", "active"))
    with caplog.at_level(logging.INFO, logger="tuloy.audit"):
        r = post(client, CHED)
    assert r.status_code == 403
    assert last_authz(caplog).reason == "unapproved_binding"


def test_agency_outside_catalogue_is_unapproved(client, fake_db):
    grant(fake_db, (SUBMIT, "ELSEWHERE", "ELSEWHERE", "active"))
    assert post(client, CHED).status_code == 403


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_expired_or_revoked_grant_is_403(client, fake_db, state):
    grant(fake_db, (SUBMIT, "CHED", "CHED", state))
    assert post(client, CHED).status_code == 403


def test_no_grant_is_403(client, fake_db):
    grant(fake_db)
    assert post(client, CHED).status_code == 403


def test_other_scope_does_not_count_toward_binding(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"), ("tuloy:enrollment:read", "TESDA"))
    assert post(client, CHED).status_code == 501


def test_grant_check_precedes_body_validation(client, fake_db):
    grant(fake_db)
    assert post(client, {"junk": 1}).status_code == 403
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
    assert client.post(URL, headers=h, content=b"not json").status_code == 403
    grant(fake_db, (SUBMIT, "DSWD"))
    assert post(client, {"junk": 1}).status_code == 403
    grant(fake_db, (SUBMIT, "CHED"), (SUBMIT, "TESDA"))
    assert post(client, {"junk": 1}).status_code == 403


def test_missing_or_bad_idempotency_key_is_422(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    assert client.post(URL, headers=bearer(), json=CHED).status_code == 422
    assert client.post(URL, headers={**bearer(), "Idempotency-Key": "nope"}, json=CHED).status_code == 422


def test_mothers_maiden_object_rejected(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, {**CHED, "mothers_maiden": {"first_name": "Synthetic"}}).status_code == 422
    assert post(client, {**CHED, "mothers_maiden": {}}).status_code == 422


@pytest.mark.parametrize("agency,base,field", [("CHED", CHED, "tuloy_id"), ("CHED", CHED, "student_id"),
                                                ("TESDA", TESDA, "uli")])
def test_ids_are_strict_non_empty_strings(client, fake_db, agency, base, field):
    grant(fake_db, (SUBMIT, agency))
    assert post(client, {**base, field: 12345}).status_code == 422
    assert post(client, {**base, field: ""}).status_code == 422
    for kept in ("x" * 51, " padded ", "x" * 200, " "):  # preserved as sent: no length bound, no stripping
        assert post(client, {**base, field: kept}).status_code == 501


@pytest.mark.parametrize("field", ["agency", "partner_id", "partner", "agency_id", "owner", "owner_id"])
def test_body_cannot_name_agency_or_owner(client, fake_db, field):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, {**CHED, field: "CHED"}).status_code == 422


def test_unknown_nested_field_rejected(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, nested(CHED, "degree", extra="x")).status_code == 422
    grant(fake_db, (SUBMIT, "TESDA"))
    assert post(client, nested(TESDA, "short_course", extra="x")).status_code == 422


@pytest.mark.parametrize("agency,body", [
    ("CHED", nested(CHED, "degree", date_enrolled="2026-02-30")),
    ("CHED", nested(CHED, "degree", date_enrolled="20260115")),
    ("CHED", nested(CHED, "degree", date_enrolled=20260115)),
    ("CHED", nested(CHED, "degree", academic_year="2026-2028")),
    ("CHED", nested(CHED, "degree", academic_year="2026-27")),
    ("CHED", nested(CHED, "degree", program_duration="4")),
    ("CHED", nested(CHED, "degree", semester=5)),
    ("CHED", with_(CHED, enrollment_status="1")),
    ("CHED", with_(CHED, enrollment_status=True)),
    ("CHED", with_(CHED, date_encoded="2026-01-31T08:30:00")),
    ("CHED", with_(CHED, date_encoded="2026-01-31")),
    ("CHED", with_(CHED, date_encoded=1769820600)),
    ("CHED", with_(CHED, date_updated="2026-01-31T08:29:59+08:00")),
    ("CHED", with_(CHED, date_updated="2026-01-31T08:30:00")),
    ("TESDA", nested(TESDA, "short_course", end_date="2026-01-31")),
    ("TESDA", nested(TESDA, "short_course", start_date="2026-02-30")),
    ("TESDA", nested(TESDA, "short_course", training_status=1)),
])
def test_bad_values_rejected(client, fake_db, agency, body):
    grant(fake_db, (SUBMIT, agency))
    assert post(client, body).status_code == 422


def test_date_updated_equal_instant_in_other_offset_is_ok(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    assert post(client, with_(CHED, date_updated="2026-01-31T00:30:00Z")).status_code == 501


def test_non_object_and_invalid_json_rejected(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
    for raw in (b"", b"[]", b"not json", b'"x"', b"null"):
        assert client.post(URL, headers=h, content=raw).status_code == 422, raw


def test_422_does_not_echo_input(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED"))
    bad = with_(CHED, student_id=987654321, enrollment_status="secret-status",
                mothers_maiden={"first_name": "Marker"})
    bad["degree"]["academic_year"] = "SecretYear"
    r = post(client, bad)
    assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION_ERROR"
    for leaked in ("987654321", "secret-status", "Marker", "SecretYear", "synthetic-tuloy-1", "Synthetic Program"):
        assert leaked not in r.text
    assert all(set(e) == {"loc", "msg", "type"} for e in r.json()["detail"])


def test_audit_events_carry_binding_but_no_identifiers(client, fake_db, caplog):
    with caplog.at_level(logging.INFO, logger="tuloy.audit"):
        grant(fake_db, (SUBMIT, "TESDA"))
        assert post(client, TESDA).status_code == 501
        grant(fake_db, (SUBMIT, "DSWD"))
        assert post(client, CHED).status_code == 403
        grant(fake_db, (SUBMIT, "CHED"), (SUBMIT, "TESDA"))
        assert post(client, CHED).status_code == 403
    events = [r for r in caplog.records if r.name == "tuloy.audit"]
    ok = [r for r in events if r.action == "partner.enrollments.submit"][-1]
    assert (ok.outcome, ok.actor, ok.scope, ok.partner_id, ok.agency) == (
        "not_implemented", "user:1", SUBMIT, "TESDA", "TESDA")
    dumped = " ".join(str(v) for r in events for v in r.__dict__.values())
    for marker in MARKERS:
        assert marker not in dumped


def test_openapi_documents_both_bodies_and_agency_from_grant():
    op = main.app.openapi()["paths"][URL]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    props = [set(s["properties"]) for s in schema["oneOf"]]
    assert {"student_id", "degree"} <= props[0] and {"uli", "short_course"} <= props[1]
    assert all(s["additionalProperties"] is False for s in schema["oneOf"])
    assert "grant" in op["description"] and "Idempotency-Key" in str(op["parameters"])


def test_scope_is_checked_before_the_idempotency_header(client, fake_db):
    grant(fake_db)
    assert client.post(URL, headers=bearer(), json=CHED).status_code == 403


# --- R3 review additions: synthetic "now" behaviour. Everything below ends at 501, 403 or 422; nothing is stored. ---
import json  # noqa: E402
import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from itertools import permutations  # noqa: E402

from observability import JsonFormatter  # noqa: E402

CHED_G, TESDA_G = (SUBMIT, "CHED"), (SUBMIT, "TESDA")
COMMON_KEYS = ("tuloy_id", "enrollment_status", "date_encoded")


def raw_post(client, raw, headers=None):
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4()), "Content-Type": "application/json", **(headers or {})}
    return client.post(URL, headers=h, content=raw)


def without(base, key, section=None):
    out = copy.deepcopy(base)
    (out[section] if section else out).pop(key)
    return out


def assert_422_no_echo(r):
    assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION_ERROR", r.text
    for marker in MARKERS + ("2026-01-31", "2026-01-15"):
        assert marker not in r.text


@pytest.mark.parametrize("agency,base", [("CHED", CHED), ("TESDA", TESDA)])
def test_each_required_field_omitted_or_null_is_422(client, fake_db, agency, base):
    grant(fake_db, (SUBMIT, agency))
    section = "degree" if agency == "CHED" else "short_course"
    top = list(COMMON_KEYS) + (["student_id", "degree"] if agency == "CHED" else ["uli", "short_course"])
    for key in top:
        assert_422_no_echo(post(client, without(base, key)))
        assert_422_no_echo(post(client, with_(base, **{key: None})))
    for key in base[section]:
        assert_422_no_echo(post(client, without(base, key, section)))
        assert_422_no_echo(post(client, nested(base, section, **{key: None})))


@pytest.mark.parametrize("bad", [True, 1.5, ["x"], {"a": "x"}])
@pytest.mark.parametrize("agency,base,field", [("CHED", CHED, "tuloy_id"), ("CHED", CHED, "student_id"),
                                                ("TESDA", TESDA, "uli")])
def test_ids_of_wrong_json_type_are_422(client, fake_db, agency, base, field, bad):
    grant(fake_db, (SUBMIT, agency))
    assert_422_no_echo(post(client, {**base, field: bad}))


@pytest.mark.parametrize("bad", [1.5, 1.0, True, None, [1], {"a": 1}, "1"])
def test_enrollment_status_must_be_a_strict_integer(client, fake_db, bad):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, with_(CHED, enrollment_status=bad)))


@pytest.mark.parametrize("field", ["year_level", "semester", "program_duration"])
@pytest.mark.parametrize("bad", [True, False, 1.0, "1"])
def test_nested_integers_reject_bool_float_and_string(client, fake_db, field, bad):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, nested(CHED, "degree", **{field: bad})))


def test_long_identifiers_and_any_status_integer_reach_501(client, fake_db):
    grant(fake_db, CHED_G)
    assert post(client, with_(CHED, tuloy_id="t" * 200, student_id="s" * 200)).status_code == 501
    for status in (100, -1, 0, 99, 2**40):
        assert post(client, with_(CHED, enrollment_status=status)).status_code == 501, status
    grant(fake_db, TESDA_G)
    assert post(client, with_(TESDA, uli="u" * 200)).status_code == 501
    assert not fake_db.queries("INSERT") and not fake_db.queries("UPDATE")


@pytest.mark.parametrize("bad", ["Synthetic Maiden", 12345, True, False, ["x"], [], "", 0])
def test_mothers_maiden_only_null_or_omitted(client, fake_db, bad):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, {**CHED, "mothers_maiden": bad}))


@pytest.mark.parametrize("key", ["agency", "partner_id"])
def test_ownership_keys_set_to_null_are_422(client, fake_db, key):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, {**CHED, key: None}))
    assert_422_no_echo(post(client, nested(CHED, "degree", **{key: None})))
    grant(fake_db, TESDA_G)
    assert_422_no_echo(post(client, {**TESDA, key: None}))
    assert_422_no_echo(post(client, nested(TESDA, "short_course", **{key: None})))


def test_both_agency_sections_absent_is_422(client, fake_db):
    for g, base in ((CHED_G, CHED), (TESDA_G, TESDA)):
        grant(fake_db, g)
        assert_422_no_echo(post(client, {k: v for k, v in base.items() if k not in ("degree", "short_course")}))


def test_account_sending_the_other_agencys_body_is_422(client, fake_db):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, TESDA))
    grant(fake_db, TESDA_G)
    assert_422_no_echo(post(client, CHED))


@pytest.mark.parametrize("bad", [[], ["x"], "text", None, 5, True])
def test_agency_section_of_wrong_type_is_422(client, fake_db, bad):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, with_(CHED, degree=bad)))
    grant(fake_db, TESDA_G)
    assert_422_no_echo(post(client, with_(TESDA, short_course=bad)))


def test_valid_grant_with_malformed_json_is_422(client, fake_db):
    grant(fake_db, CHED_G)
    for raw in (b'{"tuloy_id": ', b"{'a': 1}", b"\xff\xfe", b'{"tuloy_id": "synthetic-tuloy-1",}'):
        r = raw_post(client, raw)
        assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION_ERROR", raw
        assert "synthetic-tuloy-1" not in r.text


@pytest.mark.parametrize("rows", [[(SUBMIT, "CHED", "CHED", "revoked")], [(SUBMIT, "P-1", "CHED", "active")],
                                  [(SUBMIT, "DSWD")], [(SUBMIT, "ELSEWHERE", "ELSEWHERE", "active")], []])
def test_denied_binding_with_malformed_json_is_403(client, fake_db, rows):
    grant(fake_db, *rows)
    key = str(uuid.uuid4())
    for raw in (b'{"tuloy_id": ', b"not json", b""):
        assert raw_post(client, raw, {"Idempotency-Key": key}).status_code == 403, raw


@pytest.mark.parametrize("after", [[], [(SUBMIT, "CHED", "CHED", "revoked")],
                                   [(SUBMIT, "CHED"), (SUBMIT, "TESDA")], [(SUBMIT, "DSWD")], [(SUBMIT, "P-1", "CHED", "active")]])
def test_authorisation_is_per_request_not_cached(client, fake_db, after):
    grant(fake_db, CHED_G)
    assert post(client, CHED).status_code == 501
    grant(fake_db, *after)
    assert post(client, CHED).status_code == 403
    grant(fake_db, CHED_G)  # restored: allowed again
    assert post(client, CHED).status_code == 501


def test_agency_change_between_requests_applies_to_the_next_request(client, fake_db):
    grant(fake_db, CHED_G)
    assert post(client, CHED).status_code == 501
    grant(fake_db, TESDA_G)  # no cached CHED binding: the CHED body is now checked against TESDA's model
    assert_422_no_echo(post(client, CHED))
    assert post(client, TESDA).status_code == 501
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, TESDA))


def test_two_identical_active_rows_are_one_binding(client, fake_db):
    grant(fake_db, (SUBMIT, "CHED", "CHED", "active"), (SUBMIT, "CHED", "CHED", "active"))
    assert post(client, CHED).status_code == 501


_VALID_ROW = (SUBMIT, "CHED", "CHED", "active")
_INVALID_ROWS = [(SUBMIT, "TESDA", "TESDA", "active"), (SUBMIT, "P-1", "CHED", "active"),
                 (SUBMIT, "DSWD", "DSWD", "active"), (SUBMIT, "X", "X", "active")]


@pytest.mark.parametrize("invalid", _INVALID_ROWS)
def test_valid_plus_distinct_invalid_row_is_403_in_any_order(client, fake_db, invalid):
    for order in permutations([_VALID_ROW, invalid]):
        grant(fake_db, *order)
        assert post(client, CHED).status_code == 403, order


def test_valid_plus_two_invalid_rows_is_403_in_every_permutation(client, fake_db):
    rows = [_VALID_ROW, _INVALID_ROWS[0], _INVALID_ROWS[1]]
    for order in permutations(rows):
        grant(fake_db, *order)
        assert post(client, CHED).status_code == 403, order
    # an exact duplicate of the valid row does not change that
    for order in permutations(rows + [_VALID_ROW]):
        grant(fake_db, *order)
        assert post(client, CHED).status_code == 403, order


# RFC 3339 shape. Pydantic by itself accepts a space separator, missing seconds, "+0800" and a comma fraction,
# so the model checks the raw string first (partner/router.py _RFC3339).
@pytest.mark.parametrize("value", [
    "2026-01-31 08:30:00+08:00", "2026-01-31T08:30+08:00", "2026-01-31T08:30:00+0800", "2026-01-31T08:30:00+08",
    "2026-01-31t08:30:00Z", "2026-01-31T08:30:00z", "2026-01-31T08:30:00,5Z", "2026-01-31T08:30:00",
    "2026-01-31T08:30:00 Z", " 2026-01-31T08:30:00Z", "2026-01-31T08:30:00Z ", "2026-01-31T08:30:00+8:00",
    "2026-01-31T08:30:00.Z", "20260131T083000Z", "2026-01-31", "2026-02-30T08:30:00Z", "2026-13-01T08:30:00Z",
    "2026-01-31T24:00:00Z", "2026-01-31T08:60:00Z", "2026-01-31T08:30:00+24:00", "2026-01-31T08:30:00+99:99", "",
])
@pytest.mark.parametrize("field", ["date_encoded", "date_updated"])
def test_non_rfc3339_timestamps_are_422(client, fake_db, field, value):
    grant(fake_db, CHED_G)
    body = with_(CHED, **{field: value})
    if field == "date_updated":
        body["date_encoded"] = "2000-01-01T00:00:00Z"
    assert_422_no_echo(post(client, body))


@pytest.mark.parametrize("value", ["2026-01-31T08:30:00Z", "2026-01-31T08:30:00+08:00", "2026-01-31T08:30:00-00:00",
                                   "2026-01-31T08:30:00.123456+05:30", "2026-01-31T08:30:00.5Z"])
def test_rfc3339_timestamps_are_accepted(client, fake_db, value):
    grant(fake_db, CHED_G)
    assert post(client, with_(CHED, date_encoded=value)).status_code == 501
    assert post(client, with_(CHED, date_encoded="2000-01-01T00:00:00Z", date_updated=value)).status_code == 501


@pytest.mark.parametrize("value", ["2026-02-30", "2026-02-29", "2026-04-31", "2026-13-01", "2026-00-10",
                                   "2026-01-00", "2026-1-5", "2026-01-15T00:00:00", "", None])
def test_invalid_calendar_dates_are_422_in_every_date_field(client, fake_db, value):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, nested(CHED, "degree", date_enrolled=value)))
    grant(fake_db, TESDA_G)
    assert_422_no_echo(post(client, nested(TESDA, "short_course", start_date=value)))
    assert_422_no_echo(post(client, nested(TESDA, "short_course", end_date=value)))
    if value:
        for field in ("date_encoded", "date_updated"):
            assert_422_no_echo(post(client, with_(TESDA, **{field: f"{value}T00:00:00Z"})))


def test_leap_day_in_a_leap_year_is_valid(client, fake_db):
    grant(fake_db, CHED_G)
    assert post(client, nested(CHED, "degree", date_enrolled="2028-02-29")).status_code == 501


def test_timestamp_ordering_compares_instants(client, fake_db):
    grant(fake_db, CHED_G)
    enc = "2026-01-31T08:30:00+08:00"  # = 00:30:00Z
    for same in ("2026-01-31T08:30:00+08:00", "2026-01-31T00:30:00Z", "2026-01-30T19:30:00-05:00",
                 "2026-01-31T06:00:00+05:30"):
        assert post(client, with_(CHED, date_encoded=enc, date_updated=same)).status_code == 501, same
    for earlier in ("2026-01-31T00:29:59Z", "2026-01-31T08:29:59+08:00", "2026-01-30T19:29:59-05:00"):
        assert_422_no_echo(post(client, with_(CHED, date_encoded=enc, date_updated=earlier)))


def test_short_course_same_day_is_accepted(client, fake_db):
    grant(fake_db, TESDA_G)
    assert post(client, nested(TESDA, "short_course", start_date="2026-02-01", end_date="2026-02-01")).status_code == 501
    assert_422_no_echo(post(client, nested(TESDA, "short_course", start_date="2026-02-01", end_date="2026-01-31")))


def test_same_idempotency_key_sequential_is_always_501_and_stores_nothing(client, fake_db):
    grant(fake_db, CHED_G)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    changed = with_(CHED, enrollment_status=2)
    for body in (CHED, CHED, changed, CHED, changed):
        assert post(client, body, key).status_code == 501
    assert not fake_db.queries("INSERT") and not fake_db.queries("UPDATE") and not fake_db.queries("DELETE")
    assert fake_db.commits == 0


def test_same_idempotency_key_concurrent_is_always_501_and_stores_nothing(client, fake_db):
    grant(fake_db, CHED_G)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    bodies = [CHED if i % 2 == 0 else with_(CHED, enrollment_status=i) for i in range(16)]
    barrier = threading.Barrier(len(bodies))

    def send(body):
        barrier.wait(timeout=10)
        return post(client, body, key).status_code

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        codes = list(pool.map(send, bodies))
    assert codes == [501] * len(bodies)
    assert not fake_db.queries("INSERT") and not fake_db.queries("UPDATE") and not fake_db.queries("DELETE")
    assert fake_db.commits == 0


class _Capture(logging.Handler):
    """Formats each audit event while the request is still in flight, so request_id comes from the real formatter."""
    def __init__(self):
        super().__init__(logging.INFO)
        self.setFormatter(JsonFormatter())
        self.events = []

    def emit(self, record):
        self.events.append(json.loads(self.format(record)))


@pytest.fixture
def audit_events():
    cap, log = _Capture(), logging.getLogger("tuloy.audit")
    old = log.level
    log.setLevel(logging.INFO)
    log.addHandler(cap)
    yield cap.events
    log.removeHandler(cap)
    log.setLevel(old)


def test_audit_events_carry_request_id_and_no_body_values(client, fake_db, audit_events):
    seen = []  # (response, expected action)
    grant(fake_db, TESDA_G)
    seen.append((post(client, TESDA), "partner.enrollments.submit"))      # 501
    grant(fake_db, (SUBMIT, "CHED", "CHED", "revoked"))
    seen.append((post(client, CHED), "authz.scope"))                      # 403 denial
    grant(fake_db, CHED_G)
    seen.append((post(client, with_(CHED, student_id=987654321)), None))  # 422
    seen.append((raw_post(client, b'{"tuloy_id": "synthetic-tuloy-1"'), None))  # 422 malformed
    assert [r.status_code for r, _ in seen] == [501, 403, 422, 422]
    ids = [r.headers["X-Request-ID"] for r, _ in seen]
    assert len(set(ids)) == 4
    by_id = {}
    for ev in audit_events:
        by_id.setdefault(ev["request_id"], []).append(ev)
    for rid, (r, action) in zip(ids, seen):
        events = by_id.get(rid, [])
        if action:
            assert any(e["action"] == action for e in events), (r.status_code, events)
        for e in events:  # any event on any path, 422 included, is correlated and body-free
            assert e["request_id"] == rid
    assert all(e["request_id"] != "-" for e in audit_events)
    dumped = json.dumps(audit_events)
    for marker in MARKERS + ("987654321", "2026-01-31", "2026-01-15"):
        assert marker not in dumped


def test_audit_request_id_follows_a_client_supplied_id(client, fake_db, audit_events):
    grant(fake_db, CHED_G)
    r = post(client, CHED, {"X-Request-ID": "synthetic-req-0001"})
    assert r.headers["X-Request-ID"] == "synthetic-req-0001"
    assert [e["request_id"] for e in audit_events if e["action"] == "partner.enrollments.submit"] == ["synthetic-req-0001"]


# --- Review round 2: media type, streamed size cap, denial audit fields, OpenAPI wording, catalogue gaps ---
from config import MAX_BODY_BYTES  # noqa: E402

JSON_CT = {"Content-Type": "application/json"}


def _audit_of(events, action="authz.scope"):
    return [e for e in events if e["action"] == action][-1]


@pytest.mark.parametrize("ct", ["text/plain", "application/x-www-form-urlencoded", "application/jsonx", "text/json"])
def test_non_json_media_type_is_415(client, fake_db, ct):
    grant(fake_db, CHED_G)
    r = raw_post(client, json.dumps(CHED).encode(), {"Content-Type": ct})
    assert r.status_code == 415 and r.json()["error_code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


def test_missing_content_type_is_415(client, fake_db):
    grant(fake_db, CHED_G)
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4())}
    r = client.post(URL, headers=h, content=json.dumps(CHED).encode())
    assert r.status_code == 415 and r.json()["error_code"] == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.parametrize("ct", ["application/json; charset=utf-8", "Application/JSON", "APPLICATION/JSON;charset=UTF-8",
                                " application/json ;charset=utf-8"])
def test_json_media_type_with_parameters_reaches_501(client, fake_db, ct):
    grant(fake_db, CHED_G)
    assert raw_post(client, json.dumps(CHED).encode(), {"Content-Type": ct}).status_code == 501


@pytest.mark.parametrize("rows", [[], [(SUBMIT, "CHED", "CHED", "revoked")], [(SUBMIT, "DSWD")],
                                  [(SUBMIT, "CHED"), (SUBMIT, "TESDA")], [(SUBMIT, "P-1", "CHED", "active")]])
def test_denied_binding_with_text_plain_is_403_binding_first(client, fake_db, rows):
    grant(fake_db, *rows)
    assert raw_post(client, b"x", {"Content-Type": "text/plain"}).status_code == 403


def _body_of_size(size):
    body = with_(CHED, tuloy_id="")
    base = len(json.dumps(body, separators=(",", ":")).encode())
    body["tuloy_id"] = "t" * (size - base)
    raw = json.dumps(body, separators=(",", ":")).encode()
    assert len(raw) == size
    return raw


def test_body_exactly_at_the_limit_is_accepted_and_validated(client, fake_db):
    grant(fake_db, CHED_G)
    r = raw_post(client, _body_of_size(MAX_BODY_BYTES))
    assert MAX_BODY_BYTES == 1_000_000 and r.status_code == 501  # a valid body of exactly the limit


def test_body_at_the_limit_with_unknown_padding_reaches_validation(client, fake_db):
    grant(fake_db, CHED_G)
    raw = json.dumps({**CHED, "pad": ""}).encode()
    raw = raw[:-2] + b"x" * (MAX_BODY_BYTES - len(raw)) + b'"}'
    assert len(raw) == MAX_BODY_BYTES
    assert_422_no_echo(raw_post(client, raw))


def test_declared_length_over_the_limit_is_413(client, fake_db):
    grant(fake_db, CHED_G)
    r = raw_post(client, _body_of_size(MAX_BODY_BYTES) + b" ")
    assert r.status_code == 413 and r.json()["error_code"] == "PAYLOAD_TOO_LARGE"
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


def _chunks(raw, step=65536):
    for i in range(0, len(raw), step):
        yield raw[i:i + step]


def test_chunked_body_over_the_limit_is_413_with_the_middleware_body(client, fake_db):
    grant(fake_db, CHED_G)
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4()), **JSON_CT}
    r = client.post(URL, headers=h, content=_chunks(_body_of_size(MAX_BODY_BYTES) + b" "))
    assert "content-length" not in r.request.headers and r.request.headers["transfer-encoding"] == "chunked"
    assert r.status_code == 413 and r.json()["error_code"] == "PAYLOAD_TOO_LARGE"
    assert r.json()["detail"] == "Request body too large" and r.json()["request_id"] == r.headers["X-Request-ID"]


def test_chunked_body_exactly_at_the_limit_is_accepted(client, fake_db):
    grant(fake_db, CHED_G)
    h = {**bearer(), "Idempotency-Key": str(uuid.uuid4()), **JSON_CT}
    r = client.post(URL, headers=h, content=_chunks(_body_of_size(MAX_BODY_BYTES)))
    assert r.status_code == 501


def test_other_routes_keep_the_declared_length_limit(client):
    big = client.post("/VerifyMemberBene", content=b"x" * (MAX_BODY_BYTES + 1), headers={**bearer(), **JSON_CT})
    assert big.status_code == 413 and big.json()["error_code"] == "PAYLOAD_TOO_LARGE"


def _denial(client, fake_db, audit_events, rows, body=CHED):
    grant(fake_db, *rows)
    r = post(client, body)
    assert r.status_code == 403
    ev = _audit_of(audit_events)
    assert ev["request_id"] == r.headers["X-Request-ID"]
    return ev


def test_audit_ambiguous_binding_lists_sorted_distinct_pairs(client, fake_db, audit_events):
    ev = _denial(client, fake_db, audit_events,
                 [(SUBMIT, "TESDA"), (SUBMIT, "P-1", "CHED", "active"), (SUBMIT, "CHED"), (SUBMIT, "CHED", "CHED", "never")])
    assert ev["outcome"] == "denied" and ev["reason"] == "ambiguous_binding"
    assert ev["actor"] == "user:1" and ev["scope"] == SUBMIT and ev["operation"] == "partner.enrollments.submit"
    assert ev["bindings"] == [{"partner_id": "CHED", "agency": "CHED"}, {"partner_id": "P-1", "agency": "CHED"},
                              {"partner_id": "TESDA", "agency": "TESDA"}]
    assert "partner_id" not in ev and "agency" not in ev


@pytest.mark.parametrize("row,reason", [((SUBMIT, "P-1", "CHED", "active"), "unapproved_binding"),
                                        ((SUBMIT, "ELSEWHERE", "ELSEWHERE", "active"), "unapproved_binding"),
                                        ((SUBMIT, "DSWD"), "payload_not_approved")])
def test_audit_single_binding_denials_carry_the_binding(client, fake_db, audit_events, row, reason):
    ev = _denial(client, fake_db, audit_events, [row])
    stored = (row[1], row[2]) if len(row) > 2 else (row[1], row[1])
    assert (ev["actor"], ev["scope"], ev["operation"], ev["outcome"], ev["reason"]) == (
        "user:1", SUBMIT, "partner.enrollments.submit", "denied", reason)
    assert (ev["partner_id"], ev["agency"]) == stored
    dumped = json.dumps(ev)
    assert not any(m in dumped for m in MARKERS)


def test_audit_no_grant_carries_actor_scope_operation_and_reason(client, fake_db, audit_events):
    ev = _denial(client, fake_db, audit_events, [])
    assert (ev["actor"], ev["scope"], ev["operation"], ev["outcome"], ev["reason"]) == (
        "user:1", SUBMIT, "partner.enrollments.submit", "denied", "no_grant")
    assert "partner_id" not in ev and "agency" not in ev and "bindings" not in ev


def test_other_routes_no_grant_audit_is_unchanged(client, fake_db, audit_events):
    fake_db.scopes[1] = []
    r = client.get(f"{PREFIX}/enrollments", headers=bearer())
    assert r.status_code == 403
    ev = _audit_of(audit_events)
    assert ev["scope"] == "tuloy:enrollment:read" and ev["actor"] == "user:1"
    assert "reason" not in ev and "operation" not in ev


def test_openapi_says_replay_and_409_are_activation_phase():
    op = main.app.openapi()["paths"][URL]["post"]
    text = op["description"]
    assert "every valid request returns 501" in text and "nothing is stored" in text
    assert "after activation" in text.lower()
    # 409 cannot be returned today: described as after activation, not listed as a response or error row
    assert "409" not in op["responses"] and not any(line.startswith("| 409 ") for line in text.splitlines())
    assert "different body will return 409" in text
    key = next(p for p in op["parameters"] if p["name"] == "Idempotency-Key")
    assert "after activation" in key["description"] and "501" in key["description"]
    assert {"413", "415"} <= set(op["responses"])
    assert any(line.startswith("| 415 ") and "UNSUPPORTED_MEDIA_TYPE" in line for line in text.splitlines())
    # R6 keeps its own wording
    r6 = main.app.openapi()["paths"][f"{PREFIX}/enrollments/{{transaction_id}}/events"]["post"]
    assert "after activation" not in next(p for p in r6["parameters"] if p["name"] == "Idempotency-Key")["description"]


# --- catalogue gaps: B10, B11, B22, B19(d), B06 ---
def _n(s):
    return "n" * s


@pytest.mark.parametrize("name,ok", [(_n(1), True), (_n(150), True), (" " + _n(150) + " ", True), (_n(151), False),
                                     ("", False), ("   ", False), (12345, False)])
def test_b10_b11_program_name_bounds(client, fake_db, name, ok):
    grant(fake_db, CHED_G)
    r = post(client, nested(CHED, "degree", program_name=name))
    assert r.status_code == (501 if ok else 422)
    grant(fake_db, TESDA_G)
    r = post(client, nested(TESDA, "short_course", program_name=name))
    assert r.status_code == (501 if ok else 422)


@pytest.mark.parametrize("name,ok", [(_n(1), True), (_n(50), True), (_n(51), False), ("", False), ("   ", False), (1, False)])
def test_b11_training_status_bounds(client, fake_db, name, ok):
    grant(fake_db, TESDA_G)
    assert post(client, nested(TESDA, "short_course", training_status=name)).status_code == (501 if ok else 422)


@pytest.mark.parametrize("field,lo,hi", [("program_duration", 0, 20), ("year_level", 0, 10), ("semester", 0, 4)])
def test_b10_integer_min_max_and_beyond(client, fake_db, field, lo, hi):
    grant(fake_db, CHED_G)
    for good in (lo, hi):
        assert post(client, nested(CHED, "degree", **{field: good})).status_code == 501, good
    for bad in (lo - 1, hi + 1):
        assert_422_no_echo(post(client, nested(CHED, "degree", **{field: bad})))


@pytest.mark.parametrize("year", ["2027-2026", 20262027, "2026-2028", "2026-27", "SecretYear"])
def test_b10_academic_year_bad_forms(client, fake_db, year):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, nested(CHED, "degree", academic_year=year)))


def test_b22_lower_case_stored_grant_is_unapproved_and_not_normalised(client, fake_db, audit_events):
    # grants.py upper-cases agency and partner on write; admin SQL bypasses it. The API compares the stored
    # values case-sensitively, so a lower-case (ched, ched) row is an unapproved binding: 403, not a CHED account.
    ev = _denial(client, fake_db, audit_events, [(SUBMIT, "ched", "ched", "active")])
    assert ev["reason"] == "unapproved_binding" and (ev["partner_id"], ev["agency"]) == ("ched", "ched")


def test_b21_valid_plus_lower_case_row_is_ambiguous(client, fake_db, audit_events):
    ev = _denial(client, fake_db, audit_events, [(SUBMIT, "CHED"), (SUBMIT, "ched", "ched", "active")])
    assert ev["reason"] == "ambiguous_binding"


@pytest.mark.parametrize("rows", [[(SUBMIT, "DSWD")], [(SUBMIT, "CHED"), (SUBMIT, "TESDA")], [(SUBMIT, "P-1", "CHED", "active")]])
def test_b19d_denied_binding_with_missing_key_is_422(client, fake_db, audit_events, rows):
    # Current order: scope -> Idempotency-Key -> binding -> body. The header fails before _submit_binding runs.
    grant(fake_db, *rows)
    before = len(audit_events)
    r = client.post(URL, headers=bearer(), json=CHED)
    assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION_ERROR"
    assert not [e for e in audit_events[before:] if e["action"] == "authz.scope"]


def test_b06_body_agency_tesda_on_ched_account_is_422(client, fake_db):
    grant(fake_db, CHED_G)
    assert_422_no_echo(post(client, {**CHED, "agency": "TESDA"}))
    grant(fake_db, TESDA_G)
    assert_422_no_echo(post(client, {**TESDA, "agency": "CHED"}))
