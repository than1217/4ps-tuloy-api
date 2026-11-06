"""scripts/grants.py and scripts/provision_verification.py: expiry flags and provisioning rules. No database."""
import dataclasses
import json
import os
import stat
import subprocess
import sys

import pytest

from config import get_settings
from scopes import KNOWN_SCOPES, LEGACY_MEMBER_VERIFY, SCOPE_VERIFY, agencies_for
from scripts import grants, provision_verification as pv

NULL_EXPIRY = "expires_at) VALUES (%s, %s, %s, %s, %s, NULL)"


class Cursor:
    """Records statements; answers the existing-username lookup from `existing`."""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.statements = []
        self.lastrowid = 0
        self._row = None

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql.startswith("SELECT id FROM api_users"):
            self._row = (1,) if params[0] in self.existing else None
        elif sql.startswith("INSERT INTO api_users"):
            self.lastrowid += 1

    def fetchone(self):
        return self._row

    def close(self):
        pass

    def inserts(self, table):
        return [(s, p) for s, p in self.statements if s.startswith(f"INSERT INTO {table}")]


class GrantCursor(Cursor):
    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql.startswith("SELECT id, disabled"):
            self._row = (7, 0)
        elif sql.startswith("INSERT"):
            self.lastrowid = 55


def _args(**kw):
    base = dict(user="u", scope=LEGACY_MEMBER_VERIFY, partner="DSWD", agency="dswd", by="m", days=None, no_expiry=False)
    return type("A", (), {**base, **kw})


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(grants, "load_settings", get_settings)
    monkeypatch.setattr(grants, "connect", lambda settings, holder: pytest.fail("must not connect"))


GRANT_ARGV = ["grant", "--user", "u", "--scope", LEGACY_MEMBER_VERIFY, "--partner", "CHED", "--agency", "CHED", "--by", "m"]


def test_grant_requires_days_or_no_expiry(no_db):
    with pytest.raises(SystemExit) as err:
        grants.main(GRANT_ARGV)                     # neither flag: no silent default
    assert err.value.code == 2


def test_grant_days_and_no_expiry_are_mutually_exclusive(no_db):
    with pytest.raises(SystemExit) as err:
        grants.main([*GRANT_ARGV, "--days", "7", "--no-expiry"])
    assert err.value.code == 2


def test_provision_days_and_no_expiry_are_mutually_exclusive():
    with pytest.raises(SystemExit) as err:
        pv.main(["--days", "7", "--no-expiry"])
    assert err.value.code == 2


def test_grant_no_expiry_inserts_null_expiry():
    cur = GrantCursor()
    assert grants.cmd_grant(cur, _args(no_expiry=True)) == 0
    sql, params = cur.inserts("api_user_scopes")[0]
    assert NULL_EXPIRY in sql and params == (7, LEGACY_MEMBER_VERIFY, "DSWD", "DSWD", "m")


def test_grant_days_stays_dated_and_bounded():
    cur = GrantCursor()
    assert grants.cmd_grant(cur, _args(days=7)) == 0
    assert "INTERVAL %s DAY" in cur.inserts("api_user_scopes")[0][0]
    for days in (0, grants.MAX_DAYS + 1):
        with pytest.raises(SystemExit):
            grants.cmd_grant(GrantCursor(), _args(days=days))


def test_provision_creates_the_three_accounts_and_legacy_verify_for_dswd_only():
    cur = Cursor()
    outcomes, credentials = pv.provision(cur, "Martin", None)
    assert outcomes == [("tuloy-tesda01", "TESDA", "created"), ("tuloy-ched01", "CHED", "created"),
                        ("tuloy-dswd01", "DSWD", "created")]
    grant_rows = [p for _, p in cur.inserts("api_user_scopes")]
    assert sorted((p[1], p[3]) for p in grant_rows) == [
        (LEGACY_MEMBER_VERIFY, "DSWD"), (SCOPE_VERIFY, "CHED"), (SCOPE_VERIFY, "DSWD"), (SCOPE_VERIFY, "TESDA")]
    for _, _, partner, agency, by in grant_rows:
        assert partner == agency and agency in {"TESDA", "CHED", "DSWD"} and by == "Martin"
    assert all(NULL_EXPIRY in s for s, _ in cur.inserts("api_user_scopes"))
    passwords = [c["password"] for c in credentials]
    assert len(set(passwords)) == 3 and all(len(p) >= 24 for p in passwords)
    hashes = [p[1] for _, p in cur.inserts("api_users")]
    assert all(h.startswith("$2") and h not in passwords for h in hashes)


def test_provision_with_days_uses_dated_grants():
    cur = Cursor()
    pv.provision(cur, "Martin", 7)
    assert all("INTERVAL %s DAY" in s and p[-1] == 7 for s, p in cur.inserts("api_user_scopes"))


def test_provision_skips_existing_username_and_grants_nothing_to_it():
    cur = Cursor(existing={"tuloy-ched01"})
    outcomes, credentials = pv.provision(cur, "Martin", None)
    assert outcomes == [("tuloy-tesda01", "TESDA", "created"), ("tuloy-ched01", "CHED", "skipped"),
                        ("tuloy-dswd01", "DSWD", "created")]
    assert [c["username"] for c in credentials] == ["tuloy-tesda01", "tuloy-dswd01"]
    assert [p[0] for _, p in cur.inserts("api_users")] == ["tuloy-tesda01", "tuloy-dswd01"]
    assert not [s for s, _ in cur.statements if s.startswith(("UPDATE", "DELETE"))]
    assert len(cur.inserts("api_user_scopes")) == 3


def test_provision_all_existing_creates_nothing():
    cur = Cursor(existing={u for u, _ in pv.ACCOUNTS})
    outcomes, credentials = pv.provision(cur, "Martin", None)
    assert credentials == [] and {o[2] for o in outcomes} == {"skipped"} and not cur.inserts("api_users")


def test_provisioned_scopes_exclude_everything_else():
    assert set(pv.GRANT_SCOPES) == {LEGACY_MEMBER_VERIFY, SCOPE_VERIFY}


class Conn:
    def __init__(self, cur):
        self.cur, self.autocommit, self.commits, self.rollbacks = cur, True, 0, 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _apply_env(monkeypatch, tmp_path, conn):
    settings = dataclasses.replace(get_settings(), migration_user="m", migration_password="p")
    monkeypatch.setattr(pv, "load_settings", lambda: settings)
    monkeypatch.setattr(pv, "connect", lambda s, h: conn)
    monkeypatch.setattr(pv, "__file__", str(tmp_path / "scripts" / "provision_verification.py"))


def _assert_only_current_user_ace(path):
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    aces = [line.split(str(path), 1)[-1].strip() for line in out.splitlines() if ":(" in line]
    assert len(aces) == 1, out
    assert aces[0].lower().startswith(os.environ["USERNAME"].lower()) or "\\" + os.environ["USERNAME"].lower() in aces[0].lower()
    assert "(I)" not in aces[0]  # nothing inherited


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL behaviour")
def test_restrict_failure_deletes_file_and_exits(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(5, "icacls")
    monkeypatch.setattr(pv.subprocess, "run", boom)
    target = tmp_path / "c.json"
    target.write_text("x")
    with pytest.raises(SystemExit):
        pv._restrict_to_current_user(target)
    assert not target.exists()


def test_apply_writes_private_credentials_and_never_prints_passwords(monkeypatch, tmp_path, capsys, caplog):
    conn = Conn(Cursor(existing={"tuloy-dswd01"}))
    _apply_env(monkeypatch, tmp_path, conn)
    assert pv.main(["--apply", "--no-expiry", "--by", "Martin"]) == 0
    files = list((tmp_path / ".run").glob("verification-credentials-*.json"))
    assert len(files) == 1 and conn.commits == 1 and conn.rollbacks == 0
    records = json.loads(files[0].read_text(encoding="utf-8"))
    assert [r["username"] for r in records] == ["tuloy-tesda01", "tuloy-ched01"]
    if sys.platform == "win32":
        _assert_only_current_user_ace(files[0])
        files[0].unlink()  # the owner can still delete it after handing the passwords over
    else:
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    shown = capsys.readouterr().out + caplog.text
    assert "skipped" in shown and not any(r["password"] in shown for r in records)


def test_apply_failure_rolls_back_and_leaves_no_credential_file(monkeypatch, tmp_path):
    conn = Conn(Cursor())

    def fail():
        raise RuntimeError("commit failed")
    conn.commit = fail
    _apply_env(monkeypatch, tmp_path, conn)
    with pytest.raises(RuntimeError):
        pv.main(["--apply", "--no-expiry"])
    assert conn.rollbacks == 1 and not list((tmp_path / ".run").glob("*.json"))


def _audit_events(out):
    return [e for e in (json.loads(line) for line in out.splitlines() if line.startswith("{")) if e.get("event") == "audit"]


def test_apply_audits_accounts_and_grants_without_secrets(monkeypatch, tmp_path, capsys):
    """N09: actor, outcome and identifiers for every account and grant; no password or hash."""
    cur = Cursor(existing={"tuloy-dswd01"})
    _apply_env(monkeypatch, tmp_path, Conn(cur))
    assert pv.main(["--apply", "--days", "7", "--by", "Martin"]) == 0
    out = capsys.readouterr().out
    events = _audit_events(out)
    accounts = [(e["outcome"], e["agency"], bool(e["user_id"])) for e in events if e["action"] == "authn.account"]
    assert accounts == [("created", "TESDA", True), ("created", "CHED", True), ("skipped", "DSWD", False)]
    grants_ = [e for e in events if e["action"] == "authz.grant"]
    assert len(grants_) == 2 and {e["scope"] for e in grants_} == {SCOPE_VERIFY}
    assert "Created 2 account(s) with 2 grants." in out   # the grants actually inserted, not accounts x scopes
    assert all(e["outcome"] == "created" and e["actor"] == "Martin" and e["days"] == 7
               and e["partner_id"] == e["agency"] and e["user_id"] for e in grants_)
    secrets_ = [json.loads(f.read_text(encoding="utf-8")) for f in (tmp_path / ".run").glob("*.json")][0]
    hashes = [p[1] for _, p in cur.inserts("api_users")]
    assert not any(r["password"] in out for r in secrets_) and not any(h in out for h in hashes)
    for f in (tmp_path / ".run").glob("*.json"):
        f.unlink()


def test_failed_apply_audits_failure_only(monkeypatch, tmp_path, capsys):
    conn = Conn(Cursor())

    def fail():
        raise RuntimeError("commit failed")
    conn.commit = fail
    _apply_env(monkeypatch, tmp_path, conn)
    with pytest.raises(RuntimeError):
        pv.main(["--apply", "--no-expiry", "--by", "Martin"])
    events = _audit_events(capsys.readouterr().out)
    assert [(e["action"], e["outcome"]) for e in events] == [("authn.account", "failed")]


@pytest.mark.parametrize("scope", sorted(KNOWN_SCOPES))
@pytest.mark.parametrize("partner,agency", [
    ("TEST", "TEST"), ("", ""), ("X", "X"), ("CHED", "TESDA"), ("P-CHED", "CHED"), ("", "CHED"),
    ("tesda" * 4, "TESDA"),
])
def test_grant_rejects_unknown_binding_for_every_scope(scope, partner, agency):
    """G04: every scope binds an approved agency with partner = agency; nothing reaches the database."""
    cur = GrantCursor()
    with pytest.raises(SystemExit, match="agency must be|partner must equal"):
        grants.cmd_grant(cur, _args(scope=scope, partner=partner, agency=agency, days=7))
    assert cur.statements == []


@pytest.mark.parametrize("scope,agency", [(s, a) for s in sorted(KNOWN_SCOPES) for a in ("CHED", " tesda ", "Dswd")
                                          if a.strip().upper() in agencies_for(s)])
def test_grant_accepts_approved_binding_for_every_scope(scope, agency):
    cur = GrantCursor()
    assert grants.cmd_grant(cur, _args(scope=scope, partner=agency.lower(), agency=agency, days=7)) == 0
    _, params = cur.inserts("api_user_scopes")[0]
    assert params[2] == params[3] == agency.strip().upper()


def test_provision_refuses_unapproved_account_binding(monkeypatch):
    monkeypatch.setattr(pv, "ACCOUNTS", (("tuloy-x01", "X"),))
    cur = Cursor()
    with pytest.raises(SystemExit, match="APPROVED_AGENCIES"):
        pv.provision(cur, "Martin", 7)
    assert cur.statements == []


@pytest.mark.parametrize("scope,agency", [
    ("tuloy:legacy:member:verify", "CHED"), ("tuloy:legacy:member:verify", "TESDA"),
    ("tuloy:legacy:beneficiary:read", "CHED"), ("tuloy:legacy:beneficiary:read", "TESDA"),
    ("tuloy:legacy:ched:read", "CHED"), ("tuloy:legacy:ched:read", "TESDA"),
    ("tuloy:legacy:ched:write", "TESDA"),
    ("tuloy:staff:admin", "CHED"), ("tuloy:staff:admin", "TESDA"),
])
def test_grant_refuses_agency_the_rulings_exclude(scope, agency):
    """Data-owner rulings: R8/R9/R11 are DSWD only; R10 is CHED or DSWD. Nothing reaches the database."""
    cur = GrantCursor()
    with pytest.raises(SystemExit, match="may only be granted to"):
        grants.cmd_grant(cur, _args(scope=scope, partner=agency, agency=agency, days=7))
    assert cur.statements == []
