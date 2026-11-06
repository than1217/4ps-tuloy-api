"""scripts/migrate.py: version-sequence guard. No database: a scripted cursor stands in."""
import hashlib

import pytest

from config import get_settings
from scripts import migrate

READS = ("SET SESSION", "SELECT VERSION()", "SELECT TABLE_NAME FROM information_schema", "SELECT version, checksum")


class Cursor:
    """Answers the reads migrate.py makes; records every statement."""

    def __init__(self, ledger):
        self.ledger = dict(ledger)
        self.statements = []
        self._one = None
        self._all = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        if sql.startswith("SELECT VERSION()"):
            self._one = ("8.0-test", "tuloy_test", "migrator@localhost")
        elif sql.startswith("SELECT TABLE_NAME"):
            self._all = [("schema_migrations",)]
        elif sql.startswith("SELECT version, checksum"):
            self._all = list(self.ledger.items())
        elif sql.startswith("SELECT GET_LOCK"):
            self._one = (1,)
        elif sql.startswith("INSERT INTO schema_migrations"):
            self.ledger[params[0]] = params[2]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def changes(self):
        return [s for s in self.statements if not s.startswith(READS)]


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    def _run(files, applied, command="apply", extra=(), stale=()):
        for version in files:
            (tmp_path / f"{version}_m.sql").write_text(f"CREATE TABLE t{version} (id int);", encoding="utf-8")
        checksum = lambda v: hashlib.sha256((tmp_path / f"{v}_m.sql").read_bytes()).hexdigest()
        cur = Cursor({v: "0" * 64 if v in stale else checksum(v) for v in applied})
        conn = type("Conn", (), {"cursor": lambda self: cur})()
        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
        monkeypatch.setattr(migrate, "load_settings", get_settings)
        monkeypatch.setattr(migrate, "connect", lambda settings, holder: conn)
        code = migrate.main([command, "--yes", *extra])
        return code, cur, capsys.readouterr().out
    return _run


def test_missing_predecessor_refused_before_any_sql(run):
    code, cur, out = run(["0001", "0002", "0003", "0005"], ["0001", "0002"])
    assert code == 6
    assert "missing migration file(s): 0004" in out
    assert cur.changes() == []
    assert "Nothing applied" in out


def test_contiguous_files_apply(run):
    code, cur, out = run(["0001", "0002", "0003", "0004"], ["0001", "0002"])
    assert code == 0
    assert sorted(cur.ledger) == ["0001", "0002", "0003", "0004"]
    assert "CREATE TABLE t0003 (id int)" in cur.statements
    assert "0004 0004_m.sql: applied now" in out


def test_already_applied_gap_reported_by_status(run):
    code, cur, out = run(["0001", "0002", "0003", "0004", "0005"], ["0001", "0002", "0005"], command="status")
    assert code == 6
    assert "pending 0003, 0004 older than applied 0005" in out
    assert cur.changes() == []


def test_already_applied_gap_blocks_apply(run):
    code, cur, out = run(["0001", "0002", "0003", "0004", "0005"], ["0001", "0002", "0005"])
    assert code == 6
    assert cur.changes() == []


def test_sequence_problems_clean():
    assert migrate.sequence_problems({"0001", "0002"}, {"0001": "x"}) == []
    assert migrate.sequence_problems(set(), {}) == []


# ---- --backfill: one explicitly named gap (staging 2026-10-29: 0003 pending under applied 0004/0005) ----

def test_backfill_applies_only_the_named_gap(run):
    code, cur, out = run(["0001", "0002", "0003", "0004", "0005", "0006"], ["0001", "0002", "0004", "0005"],
                         extra=("--backfill", "0003"))
    assert code == 0
    assert "0003 0003_m.sql: applied now" in out
    assert "CREATE TABLE t0003 (id int)" in cur.statements
    assert "CREATE TABLE t0006 (id int)" not in cur.statements
    assert sorted(cur.ledger) == ["0001", "0002", "0003", "0004", "0005"]
    assert not any(s.startswith(("UPDATE", "DELETE")) for s in cur.statements)


def test_backfill_leaves_other_gaps_blocking(run):
    code, cur, out = run(["0001", "0002", "0003", "0004", "0005"], ["0001", "0002", "0005"],
                         extra=("--backfill", "0003"))
    assert code == 6
    assert "pending 0004 older than applied 0005" in out
    assert cur.changes() == []


@pytest.mark.parametrize("extra, applied", [
    (("--backfill", "0005"), ["0001", "0002", "0004"]),          # newer than every applied version
    (("--backfill", "0002"), ["0001", "0002", "0004", "0005"]),  # already applied
    (("--backfill", "0009"), ["0001", "0002", "0004", "0005"]),  # no such file
])
def test_backfill_refuses_a_version_that_is_not_a_gap(run, extra, applied):
    code, cur, out = run(["0001", "0002", "0003", "0004", "0005"], applied, extra=extra)
    assert code == 6
    assert cur.changes() == []


def test_backfill_is_apply_only_and_excludes_to(run):
    assert run(["0001"], ["0001"], command="status", extra=("--backfill", "0001"))[0] == 2
    assert run(["0001"], ["0001"], extra=("--backfill", "0001", "--to", "0001"))[0] == 2


def test_backfill_still_stops_on_checksum_mismatch(run):
    code, cur, out = run(["0001", "0002", "0003", "0004"], ["0001", "0002", "0004"], extra=("--backfill", "0003"),
                         stale=("0004",))
    assert code == 4
    assert "0004 0004_m.sql: CHECKSUM MISMATCH" in out
    assert cur.changes() == []
