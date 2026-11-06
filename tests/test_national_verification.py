"""National verification grants: agency identifies the caller, not a regional partition."""
from types import SimpleNamespace

import pytest

from conftest import bearer
from scopes import LEGACY_MEMBER_VERIFY, SCOPE_VERIFY
from scripts.grants import cmd_grant


@pytest.mark.parametrize("agency", ["CHED", "TESDA", "DSWD"])
@pytest.mark.parametrize("path,scope", [
    ("/VerifyMemberBene", LEGACY_MEMBER_VERIFY),
    ("/v1/api/tuloy/members/verify", SCOPE_VERIFY),
])
def test_verification_is_national_for_each_agency(client, fake_db, agency, path, scope):
    fake_db.scopes[1] = [(scope, agency)]
    fake_db.on(r"FROM vw_verify_member_bene", lambda sql, p: [{"tuloy_id": "synthetic-national", "member_id": 123}])
    response = client.post(path, headers=bearer(), json={
        "first_name": "Synthetic", "last_name": "National", "date_of_birth": "2000-01-01",
    })
    if scope == LEGACY_MEMBER_VERIFY and agency != "DSWD":   # data-owner rulings: R8 is DSWD only
        assert response.status_code == 403 and not fake_db.queries("FROM vw_verify_member_bene")
        return
    assert response.status_code == 200
    assert response.json()["status"] == "Matched"
    sql, params = fake_db.queries("FROM vw_verify_member_bene")[0]
    assert params == ("Synthetic", "National", "2000-01-01")
    assert not any(column in sql.lower() for column in ("region", "agency", "partner_id", "bgy_psgc_code"))


@pytest.mark.parametrize("scope,agency", [(SCOPE_VERIFY, "CHED"), (SCOPE_VERIFY, "tesda"), (SCOPE_VERIFY, " DSWD "),
                                          (LEGACY_MEMBER_VERIFY, " DSWD ")])
def test_provision_verification_agency(fake_db, agency, scope):
    fake_db.on(r"SELECT id, disabled FROM api_users", lambda sql, p: [(1, 0)])
    args = SimpleNamespace(user="synthetic-staff", scope=scope, partner=agency,
                           agency=agency, days=7, no_expiry=False, by="synthetic-operator")
    assert cmd_grant(fake_db.cursor(), args) == 0
    _, params = fake_db.queries("INSERT INTO api_user_scopes")[0]
    assert params[3] == agency.strip().upper()


@pytest.mark.parametrize("agency", ["", "UNKNOWN", "ALL"])
def test_invalid_verification_agency_does_not_write(fake_db, agency):
    args = SimpleNamespace(scope=SCOPE_VERIFY, agency=agency, days=7, no_expiry=False)
    with pytest.raises(SystemExit, match="CHED, TESDA or DSWD"):
        cmd_grant(fake_db.cursor(), args)
    assert not fake_db.executed
