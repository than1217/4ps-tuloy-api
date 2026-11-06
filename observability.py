"""JSON logging with request IDs, secret/PII redaction, and audit events."""
import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# Keys whose values are never logged, whatever the nesting.
SENSITIVE_KEYS = {
    "password", "hashed_password", "secret", "secret_key", "token", "access_token",
    "authorization", "api_key", "ssh_password", "db_password",
    "first_name", "middle_name", "last_name", "date_of_birth", "birthdate", "lrn", "pcn",
    "philsys", "account_no", "member_id", "household_id", "listahanan_id", "mm_first_name",
    "mm_middle_name", "mm_last_name", "contact", "email", "student_id", "transaction_id",
    "tuloy_id", "enrollment_id", "username",
}
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api_key)\s*[=:]\s*\S+")


def redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _JWT.sub("<jwt-redacted>", text)
    return _KV_SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if str(k).lower() in SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            # Exception type only: messages from drivers can carry SQL values or hostnames.
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            tb = record.exc_info[2]
            frames = []
            while tb is not None:
                code = tb.tb_frame.f_code
                frames.append(f"{os.path.basename(code.co_filename)}:{tb.tb_lineno}:{code.co_name}")
                tb = tb.tb_next
            payload["exc_frames"] = frames[-8:]
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("paramiko", "sshtunnel", "mysql.connector"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn access logs are replaced by the request middleware's log line.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


def pseudonym(value: str) -> str:
    """Stable, non-reversible tag for correlating log lines (e.g. a login name) without logging it."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def new_request_id(incoming: str | None) -> str:
    if incoming and REQUEST_ID_PATTERN.match(incoming):
        return incoming
    return uuid.uuid4().hex


audit_logger = logging.getLogger("tuloy.audit")


def audit(action: str, outcome: str, actor: str | None = None, **fields) -> None:
    """Emit an audit event. Callers pass identifiers and counts, never personal data."""
    audit_logger.info("audit", extra={"action": action, "outcome": outcome, "actor": actor or "anonymous", **fields})
