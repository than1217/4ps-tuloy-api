"""Verify staff access tokens. No claim here grants application permissions."""
import json
import threading
import time
from functools import lru_cache
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt

from config import valid_keycloak_issuer


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_json(url, timeout):
    # Trust only the validated issuer's origin; never follow a discovery redirect.
    request = Request(url, headers={"Accept": "application/json"})
    with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("OIDC document too large")
    return json.loads(raw)


class KeycloakValidator:
    def __init__(self, issuer, audience, cache_seconds, timeout, *, allow_local_http=False):
        if not valid_keycloak_issuer(issuer, allow_local_http=allow_local_http):
            raise jwt.InvalidTokenError("Could not validate credentials")
        self.issuer = issuer
        self.audience = audience
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._lock = threading.Lock()
        self._keys = {}
        self._expires = 0.0
        self._retry_after = 0.0
        self._rotation_after = 0.0

    def _reload(self, now):
        # Bound failed fetches too. An outage must not cause one network call per request.
        if now < self._retry_after:
            raise jwt.InvalidTokenError()
        self._retry_after = now + 5
        discovery = _fetch_json(self.issuer + "/.well-known/openid-configuration", self.timeout)
        if not isinstance(discovery, dict) or discovery.get("issuer") != self.issuer:
            raise ValueError("Invalid discovery")
        uri = discovery.get("jwks_uri")
        origin = urlsplit(self.issuer)
        target = urlsplit(uri) if isinstance(uri, str) else None
        if (target is None or target.scheme != origin.scheme or target.netloc != origin.netloc
                or target.username or target.password or target.fragment or target.query):
            raise ValueError("Invalid JWKS location")
        document = _fetch_json(uri, self.timeout)
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise ValueError("Invalid JWKS")
        keys = {}
        for item in document["keys"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid JWK")
            kid = item.get("kid")
            if (not isinstance(kid, str) or not kid or item.get("kty") != "RSA"
                    or item.get("alg", "RS256") != "RS256" or item.get("use", "sig") != "sig"
                    or "d" in item or ("key_ops" in item and item["key_ops"] != ["verify"])):
                continue
            if kid in keys:
                raise ValueError("Ambiguous key")
            key = jwt.PyJWK.from_dict(item, algorithm="RS256").key
            if key.key_size < 2048:
                raise ValueError("Weak key")
            keys[kid] = key
        if not keys:
            raise ValueError("No usable signing keys")
        self._keys = keys
        self._expires = now + self.cache_seconds
        self._retry_after = 0.0

    def decode(self, token):
        try:
            if not isinstance(token, str) or len(token) > 32768:
                raise ValueError("Invalid token")
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid or header.get("crit"):
                raise ValueError("Invalid header")
            with self._lock:
                now = time.monotonic()
                reloaded = now >= self._expires
                if reloaded:
                    self._reload(now)
                if kid not in self._keys and not reloaded and now >= self._rotation_after:
                    self._rotation_after = now + 5
                    self._reload(now)
                key = self._keys.get(kid)
                if key is None:
                    raise ValueError("Unknown signing key")
            payload = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                 audience=self.audience,
                                 options={"require": ["iss", "aud", "sub", "iat", "exp", "typ"]})
            # Keycloak access tokens carry typ=Bearer. ID tokens carry typ=ID.
            if (payload["typ"] != "Bearer" or not isinstance(payload["sub"], str)
                    or not payload["sub"].strip() or len(payload["sub"]) > 255):
                raise ValueError("Not an access token")
            for name in ("iat", "exp", "nbf"):
                if name in payload and type(payload[name]) is not int:
                    raise ValueError("Invalid NumericDate")
            if payload["exp"] <= payload["iat"] or payload["exp"] > 253402300799:
                raise ValueError("Invalid lifetime")
            return payload
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError, AttributeError):
            # Do not propagate claims, token text, provider responses or transport details.
            raise jwt.InvalidTokenError("Could not validate credentials") from None


@lru_cache(maxsize=4)
def get_validator(issuer, audience, cache_seconds=300, timeout=5, *, allow_local_http=False):
    return KeycloakValidator(issuer, audience, cache_seconds, timeout, allow_local_http=allow_local_http)
