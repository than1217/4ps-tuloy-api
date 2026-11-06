import json
import logging
import pathlib

import database
import main
from conftest import bearer
from observability import JsonFormatter, audit, redact, redact_text

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_liveness_and_readiness(client, monkeypatch):
    assert client.get("/healthz").json() == {"status": "ok"}
    monkeypatch.setattr(database, "ping", lambda: {"database": "down", "tunnel": "down"})
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "not_ready"
    monkeypatch.setattr(database, "ping", lambda: {"database": "up", "tunnel": "up"})
    assert client.get("/readyz").status_code == 200


def test_request_id_propagation(client):
    r = client.get("/healthz", headers={"X-Request-ID": "abc12345-req"})
    assert r.headers["X-Request-ID"] == "abc12345-req"
    r = client.get("/healthz", headers={"X-Request-ID": "bad id\r\ninjected"})
    assert r.headers["X-Request-ID"] != "bad id\r\ninjected" and len(r.headers["X-Request-ID"]) == 32


def test_security_headers_and_body_limit(client):
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "no-store"
    big = client.post("/VerifyMemberBene", content=b"x" * 1_000_001, headers={**bearer(), "Content-Type": "application/json"})
    assert big.status_code == 413


def test_untrusted_host_rejected(client):
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400


def test_no_cors_by_default(client):
    r = client.options("/token", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_startup_performs_no_schema_changes():
    sources = [ROOT / "main.py", ROOT / "database.py", ROOT / "security.py", *(ROOT / "routers").glob("*.py"),
               *(ROOT / "partner").glob("*.py")]
    for path in sources:
        text = path.read_text(encoding="utf-8").upper()
        for ddl in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "FOREIGN_KEY_CHECKS", "CREATE INDEX"):
            assert ddl not in text, f"{path.name} contains {ddl}"


def test_lifespan_survives_db_outage(monkeypatch):
    from fastapi.testclient import TestClient

    def boom(settings=None):
        raise RuntimeError("tunnel down")
    monkeypatch.setattr(database, "init", boom)
    monkeypatch.setattr(database, "ping", lambda: {"database": "down", "tunnel": "down"})
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503


def test_lifespan_logs_db_errno(monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from mysql.connector.errors import InterfaceError

    def refused(settings=None):
        raise InterfaceError(errno=2003, values=("192.0.2.10", 3306, "Errno 111: Connection refused"))
    monkeypatch.setattr(database, "init", refused)
    monkeypatch.setattr(database, "ping", lambda: {"database": "down", "tunnel": "n/a"})
    with caplog.at_level(logging.ERROR, logger="tuloy"):
        with TestClient(main.app):
            pass
    rec = next(r for r in caplog.records if r.getMessage() == "db_init_failed")
    assert rec.exc_type == "InterfaceError" and rec.errno == 2003 and "Connection refused" in rec.detail


def test_redaction():
    text = redact_text("Authorization: Bearer abc.def.ghi password=hunter2 eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl")
    assert "abc.def.ghi" not in text and "hunter2" not in text and "eyJhbGciOi" not in text
    nested = redact({"first_name": "Juan", "inner": [{"lrn": "123456789012", "count": 3}], "action": "x"})
    assert nested == {"first_name": "<redacted>", "inner": [{"lrn": "<redacted>", "count": 3}], "action": "x"}


def test_json_log_line_redacts_extras():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token=abc123", (), None)
    record.date_of_birth = "2000-01-01"
    record.count = 2
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "token=<redacted>" and line["date_of_birth"] == "<redacted>" and line["count"] == 2


def test_audit_event_shape(caplog):
    with caplog.at_level(logging.INFO, logger="tuloy.audit"):
        audit("legacy.verify_member", "Matched", actor="user:1")
    rec = caplog.records[-1]
    assert rec.action == "legacy.verify_member" and rec.outcome == "Matched" and rec.actor == "user:1"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_unhandled_error_has_request_id_and_template_path():
    from fastapi.testclient import TestClient

    app = main.create_app()

    @app.get("/boom/{student_id}")
    def boom(student_id: str):
        raise RuntimeError("leaks synthetic-value-123")

    cap = _Capture()
    logging.getLogger("tuloy.app").addHandler(cap)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/boom/STU-999", headers={"X-Request-ID": "req-boom-0001"})
    finally:
        logging.getLogger("tuloy.app").removeHandler(cap)
    assert r.status_code == 500
    assert r.json()["request_id"] == "req-boom-0001" and r.headers["X-Request-ID"] == "req-boom-0001"
    assert "synthetic-value-123" not in r.text
    lines = [JsonFormatter().format(rec) for rec in cap.records]
    request_line = next(json.loads(x) for x in lines if json.loads(x)["event"] == "request")
    assert request_line["path"] == "/boom/{student_id}" and request_line["status"] == 500
    assert all("STU-999" not in x and "synthetic-value-123" not in x for x in lines)


def test_limiter_refuses_new_keys_when_full():
    from ratelimit import FixedWindowLimiter

    lim = FixedWindowLimiter(limit=5, window_seconds=60, max_keys=3)
    for key in ("a", "b", "c"):
        assert lim.hit(key)[0]
    assert lim.hit("d")[0] is False
    assert lim.hit("a")[0] is True
    assert all("a" != k for k in lim._counts)  # keys are stored hashed
