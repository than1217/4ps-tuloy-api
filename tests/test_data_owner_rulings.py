"""Data-owner rulings (ADR 2026-10-27-tuloy-data-owner-access-rulings): who may use the legacy routes.

R8 /VerifyMemberBene, R9 /GetEligibleBene and R11 /GetMatchedChedBene are DSWD only; R10
/PostEnrolleeMatchedCHED is CHED or DSWD. A grant bound to another agency authorises nothing.
"""
import logging

import pytest

from conftest import bearer
from scopes import (
    LEGACY_BENEFICIARY_READ,
    LEGACY_CHED_READ,
    LEGACY_CHED_WRITE,
    LEGACY_MEMBER_VERIFY,
    SCOPE_VERIFY,
    agencies_for,
)
from security import Grant, Principal
from test_rbac import LEGACY_ROUTES, call

DATA_SQL = ("vw_verify_member_bene", "vw_get_matched_CHED", "enroll_matched_CHED_tbl", "INSERT INTO")  # any data read or write
EXCLUDED = [(s, a) for s in sorted(LEGACY_ROUTES) for a in ("CHED", "TESDA") if a not in agencies_for(s)]
PERMITTED = [(s, a) for s in sorted(LEGACY_ROUTES) for a in ("CHED", "TESDA", "DSWD") if a in agencies_for(s)]


def _scope_denials(caplog):
    return [r for r in caplog.records if r.name == "tuloy.audit" and getattr(r, "action", "") == "authz.scope"]


def test_rulings_table():
    assert agencies_for(LEGACY_MEMBER_VERIFY) == agencies_for(LEGACY_BENEFICIARY_READ) == {"DSWD"}
    assert agencies_for(LEGACY_CHED_READ) == {"DSWD"}
    assert agencies_for(LEGACY_CHED_WRITE) == {"CHED", "DSWD"}


@pytest.mark.parametrize("scope,agency", EXCLUDED)
def test_excluded_agency_is_denied_and_audited(client, fake_db, caplog, scope, agency):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    fake_db.scopes[4] = [(scope, agency)]
    r = call(client, scope, "no-grants")
    assert r.status_code == 403 and r.json()["error_code"] == "FORBIDDEN"
    assert r.json()["detail"] == "Not permitted for this operation" and agency not in r.text
    event = _scope_denials(caplog)[-1]
    assert event.outcome == "denied" and event.scope == scope and event.reason == "agency_not_permitted"
    assert not [q for marker in DATA_SQL for q in fake_db.queries(marker)]


@pytest.mark.parametrize("scope,agency", PERMITTED)
def test_permitted_agency_is_allowed(client, fake_db, scope, agency):
    fake_db.scopes[4] = [(scope, agency)]
    assert call(client, scope, "no-grants").status_code == 200


def test_ched_write_attributes_to_the_permitted_binding(client, fake_db):
    """A user holding R10 under TESDA and CHED writes under CHED only."""
    fake_db.scopes[4] = [(LEGACY_CHED_WRITE, "TESDA"), (LEGACY_CHED_WRITE, "CHED")]
    assert call(client, LEGACY_CHED_WRITE, "no-grants").status_code == 200


def test_no_grant_denial_keeps_its_event(client, caplog):
    caplog.set_level(logging.INFO, logger="tuloy.audit")
    call(client, LEGACY_MEMBER_VERIFY, "no-grants")
    assert not hasattr(_scope_denials(caplog)[-1], "reason")


def test_unruled_scopes_are_not_narrowed():
    p = Principal(1, "u", grants=frozenset({Grant(SCOPE_VERIFY, "TESDA", "TESDA"),
                                            Grant(LEGACY_MEMBER_VERIFY, "TESDA", "TESDA")}))
    assert p.grants_for(SCOPE_VERIFY) == {Grant(SCOPE_VERIFY, "TESDA", "TESDA")}
    assert p.grants_for(LEGACY_MEMBER_VERIFY) == frozenset()
    assert p.scopes == {SCOPE_VERIFY}   # the excluded legacy grant is not reported as a held scope (QA-18)


def test_staff_me_lists_only_grants_that_authorise(client, fake_db):
    fake_db.scopes[4] = [(LEGACY_MEMBER_VERIFY, "CHED"), (LEGACY_CHED_WRITE, "CHED"), (SCOPE_VERIFY, "CHED")]
    body = client.get("/v1/api/tuloy/staff/me", headers=bearer("no-grants")).json()
    assert [(g["scope"], g["agency"]) for g in body["active_grants"]] == [
        (LEGACY_CHED_WRITE, "CHED"), (SCOPE_VERIFY, "CHED")]
    assert "partner_id" not in str(body) and "id" not in body["user"]
