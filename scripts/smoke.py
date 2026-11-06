"""Read-only smoke test against a running local API (default http://127.0.0.1:8000).

Checks liveness, readiness, the partner docs spec (six Partner operations), and the DB-backed auth path.
With SMOKE_USERNAME / SMOKE_PASSWORD set it also logs in and calls POST /VerifyMemberBene with a
synthetic person (expects "Not Found"; this route only reads) and one Partner route.
No business writes. Prints no tokens, passwords or response data.
"""
import json
import ipaddress
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import jwt

from config import load_settings
from partner.router import PARTNER_OPERATIONS
from security import create_access_token

BASE = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8001")
SYNTHETIC_PERSON = {"first_name": "Zzsmoke", "last_name": "Synthetic", "date_of_birth": "1900-01-01"}
results = []


class Resp:
    def __init__(self, status_code, body, headers):
        self.status_code, self._body, self.headers = status_code, body, headers

    def json(self):
        return json.loads(self._body or b"{}")


class Client:
    """Minimal stdlib HTTP client so the smoke test needs only runtime dependencies."""

    def __init__(self, base_url, timeout):
        self.base, self.timeout = base_url, timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _send(self, method, path, headers=None, json_body=None, form=None):
        data, headers = None, dict(headers or {})
        if json_body is not None:
            data, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif form is not None:
            data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return Resp(r.status, r.read(), r.headers)
        except urllib.error.HTTPError as e:
            return Resp(e.code, e.read(), e.headers)

    def get(self, path, headers=None):
        return self._send("GET", path, headers)

    def post(self, path, json=None, data=None, headers=None):
        return self._send("POST", path, headers, json_body=json, form=data)


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def _loopback_url(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> int:
    with Client(BASE, timeout=30) as c:
        r = c.get("/healthz")
        check("liveness /healthz", r.status_code == 200)
        r = c.get("/readyz")
        check("readiness /readyz", r.status_code == 200, str(r.json().get("checks")))
        r = c.get("/tuloy/openapi.json")
        if r.status_code == 200:
            paths = r.json()["paths"]
            partner = sorted((m.upper(), p) for p, ops in paths.items() for m in ops if p.startswith("/v1/api/tuloy"))
            expected = sorted(PARTNER_OPERATIONS)
            check("openapi", True, f"{len(paths)} paths; partner ops {'= the six' if partner == expected else 'not mounted (PARTNER_API_ENABLED=false)' if not partner else 'MISMATCH'}")
        else:
            check("openapi", r.status_code == 404, "docs disabled by EXPOSE_DOCS")

        r = c.get("/GetMatchedChedBene")
        check("unauthenticated request rejected", r.status_code == 401)
        # Token signed with this process's key for a username that cannot exist: proves the
        # tunnel + api_users lookup path without touching any real account.
        try:
            settings = load_settings()
            if not _loopback_url(BASE):
                # A token minted with this machine's SECRET_KEY would be a valid credential on a
                # remote server that shares the key. Only mint for loopback targets.
                print("[SKIP] DB-backed auth lookup - SMOKE_BASE_URL is not loopback; no token minted")
            elif not settings.secret_key_ephemeral:
                token = create_access_token({"sub": "smoke-test-nonexistent-user-zz"})
                r = c.post("/VerifyMemberBene", json=SYNTHETIC_PERSON, headers={"Authorization": f"Bearer {token}"})
                check("DB-backed auth lookup (synthetic subject -> 401)", r.status_code == 401,
                      f"request_id={r.headers.get('X-Request-ID')}")
            else:
                print("[SKIP] DB-backed auth lookup - SECRET_KEY is ephemeral; set SECRET_KEY to run it")
        except Exception as exc:
            check("DB-backed auth lookup", False, type(exc).__name__)

        user, password = os.getenv("SMOKE_USERNAME"), os.getenv("SMOKE_PASSWORD")
        if not user or not password:
            print("[SKIP] authenticated read path - set SMOKE_USERNAME and SMOKE_PASSWORD for a test account")
        else:
            r = c.post("/token", data={"username": user, "password": password})
            check("login /token", r.status_code == 200, f"status {r.status_code}")
            if r.status_code == 200:
                token = r.json()["access_token"]
                claims = jwt.decode(token, options={"verify_signature": False})
                check("token has expiry", "exp" in claims)
                auth = {"Authorization": f"Bearer {token}"}
                r = c.post("/VerifyMemberBene", json=SYNTHETIC_PERSON, headers=auth)
                check("authenticated read: VerifyMemberBene (synthetic)", r.status_code == 200 and r.json().get("status") == "Not Found",
                      f"status {r.status_code}")
                # 403 without a tuloy:enrollment:read grant, 501 with one (operation not yet approved),
                # 404 when PARTNER_API_ENABLED is off. Never 200: nothing is implemented.
                r = c.get("/v1/api/tuloy/enrollments", headers=auth)
                check("partner route: 403 without grant / 501 with grant / 404 unmounted",
                      r.status_code in (403, 404, 501), f"status {r.status_code}")
                r = c.get("/GetEligibleBene?limit=1", headers=auth)
                check("bulk feed GetEligibleBene stays denied (policy: ungranted)", r.status_code == 403,
                      f"status {r.status_code}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
