import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mysql.connector import Error

from config import get_settings
from database import get_readonly_connection, query_failed
from errors import ApiError
from observability import audit, pseudonym
from scopes import KNOWN_SCOPES, LEGACY_SCOPES, DOCS_SCOPES, STAFF_SCOPES, BCMD_SCOPES, LEGACY_CHED_WRITE, agency_permitted

ALGORITHM = "HS256"

# Only staff web operations. Partner APIs, imports and internal service docs
# retain their existing service-token boundaries.
_FULL_STAFF_SCOPES = (LEGACY_SCOPES - {LEGACY_CHED_WRITE}) | STAFF_SCOPES
# Staff web scopes a staff_user_scopes row may carry. The BCMD review scope is held only through
# an explicit row; the temporary full-access override never includes it (and reads no grant rows).
_STAFF_GRANTABLE = LEGACY_SCOPES | DOCS_SCOPES | STAFF_SCOPES | BCMD_SCOPES

logger = logging.getLogger("tuloy.auth")

bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    bearerFormat="JWT",
    description="Paste the `access_token` returned by `POST /token`. Access depends on the account's grants.",
    auto_error=False,
)

# Compared against when the username does not exist, so both paths cost one bcrypt check.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode("utf-8")


@dataclass(frozen=True)
class Grant:
    """One active row of api_user_scopes. Authority is the whole tuple, never the scope alone."""
    scope: str
    partner_id: str
    agency: str


@dataclass(frozen=True)
class Principal:
    """The authenticated API user with the grants that were active when the request started."""
    id: int
    username: str
    grants: frozenset = field(default_factory=frozenset)
    expires_at: datetime | None = None
    kind: str = "api"
    full_staff_access: bool = False

    @property
    def actor(self) -> str:
        return f"staff:{self.id}" if self.kind == "staff" else f"user:{self.id}"

    @property
    def scopes(self) -> frozenset:
        """Scopes with at least one grant that authorises (the agency filter of grants_for applies)."""
        return frozenset(g.scope for g in self.grants if agency_permitted(g.scope, g.agency))

    def grants_for(self, scope: str) -> frozenset:
        """The (partner_id, agency) bindings under which this user holds ``scope``.

        A grant bound to an agency the scope does not permit (scopes.SCOPE_AGENCIES) authorises nothing.
        """
        return frozenset(g for g in self.grants if g.scope == scope and agency_permitted(scope, g.agency))

    def __getitem__(self, key):  # existing routes read current_user["username"]
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


def _unauthenticated(detail: str = "Could not validate credentials") -> ApiError:
    return ApiError(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", detail, headers={"WWW-Authenticate": "Bearer"})


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pwd_bytes = plain_password.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pwd_bytes, hashed_password.encode("utf-8"))
    except ValueError:
        return False


def get_password_hash(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def _fetch_user(db, username: str):
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, username, hashed_password, disabled FROM api_users WHERE username = %s",
            (username,),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


# Active = not revoked and not expired, judged by the database clock. A NULL expires_at is a
# non-expiring grant (migration 0006). Read on every request, so a revocation or expiry takes
# effect from the next request; JWT claims never carry scopes.
_ACTIVE_GRANTS_FROM = (
    "FROM api_user_scopes "
    "WHERE user_id = %s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"
)
ACTIVE_GRANTS_SQL = "SELECT scope, partner_id, agency " + _ACTIVE_GRANTS_FROM
# Same rows plus their expiry, for the staff identity read (GET /v1/api/tuloy/staff/me).
ACTIVE_GRANTS_WITH_EXPIRY_SQL = "SELECT scope, partner_id, agency, expires_at " + _ACTIVE_GRANTS_FROM


def _read_active_grants(db, user_id: int, sql: str) -> list:
    """Active, enforceable grants as (Grant, extra columns). Missing table or unknown scope = none."""
    cursor = db.cursor()
    try:
        cursor.execute(sql, (user_id,))
        rows = cursor.fetchall()
    except Error as exc:
        if getattr(exc, "errno", None) == 1146:  # table not migrated yet: nobody holds a scope
            return []
        raise
    finally:
        cursor.close()
    grants = []
    for scope, partner_id, agency, *extra in rows:
        if scope in KNOWN_SCOPES and partner_id and agency:
            grants.append((Grant(scope, partner_id, agency), tuple(extra)))
        else:
            logger.warning("grant_ignored", extra={"reason": "unknown_scope_or_unbound", "user_id": user_id})
    return grants


def fetch_grants(db, user_id: int, *, staff: bool = False) -> frozenset:
    """Active, enforceable grants for user_id. Missing table or unknown scope = no grant (deny)."""
    return frozenset(g for g, _ in _read_active_grants(db, user_id, _staff_sql() if staff else ACTIVE_GRANTS_SQL)
                     if not staff or g.scope in _STAFF_GRANTABLE)


def fetch_grants_with_expiry(db, user_id: int, *, staff: bool = False) -> list:
    """The same active grants as fetch_grants, as (Grant, expires_at) pairs; expires_at is None if non-expiring."""
    return [(g, extra[0]) for g, extra in _read_active_grants(db, user_id, _staff_sql(expiry=True) if staff else ACTIVE_GRANTS_WITH_EXPIRY_SQL)
            if not staff or g.scope in _STAFF_GRANTABLE]


def _staff_sql(*, expiry=False):
    columns = "scope, partner_id, agency, expires_at" if expiry else "scope, partner_id, agency"
    return ("SELECT " + columns + " FROM staff_user_scopes WHERE staff_user_id = %s "
            "AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP")


def authenticate_user(db, username: str, password: str):
    user = _fetch_user(db, username)
    if not user:
        verify_password(password, _DUMMY_HASH)
        return False
    if not verify_password(password, user["hashed_password"]):
        return False
    return user


def create_access_token(data: dict) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    to_encode = {
        **data,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[ALGORITHM],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        leeway=10,
    )


def recheck_grant(db, principal: Principal, scope: str) -> Grant:
    """Re-read the grant inside a write transaction, as its first statement.

    Cutoff: a revocation committed before this read denies the write. One committed after it
    does not cancel work already authorised (documented in docs/PRODUCTION_READINESS.md).
    """
    try:
        current = {g for g in fetch_grants(db, principal.id) if g.scope == scope and agency_permitted(scope, g.agency)}
    except Error as exc:
        raise query_failed(exc, "grant_recheck_failed")
    if not current:
        audit("authz.scope", "denied", actor=principal.actor, scope=scope, reason="revoked_during_request")
        raise ApiError(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Not permitted for this operation")
    # Legacy data carries no partner/agency column to compare with, so the binding cannot limit
    # which rows are written; it is returned so the caller can attribute the write in the audit log.
    return min(current, key=lambda g: (g.partner_id, g.agency))


def _denied(request: Request, reason: str, actor: str | None = None, **fields) -> None:
    """Audit a rejected credential (S11). Never the token itself; an unknown name only as a pseudonym."""
    audit("authn.token", "denied", actor=actor, reason=reason,
          client_ip=request.client.host if request.client else "unknown", **fields)


def get_current_user(request: Request,
                     credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                     db=Depends(get_readonly_connection)) -> Principal:
    if credentials is None or not credentials.credentials:
        _denied(request, "missing_token")
        raise _unauthenticated("Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        _denied(request, "invalid_token", error=type(exc).__name__)
        raise _unauthenticated()
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        _denied(request, "invalid_subject")
        raise _unauthenticated()

    try:
        user = _fetch_user(db, username)
        if user is None:
            _denied(request, "unknown_user", subject=pseudonym(username))
            raise _unauthenticated()
        if user.get("disabled"):
            _denied(request, "disabled", actor=f"user:{user['id']}")
            raise ApiError(status.HTTP_403_FORBIDDEN, "ACCOUNT_DISABLED", "Inactive user")
        grants = fetch_grants(db, user["id"])
    except Error as exc:
        raise query_failed(exc, "auth_lookup_failed")
    return Principal(id=user["id"], username=user["username"], grants=grants)


def get_verified_staff_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict:
    settings = get_settings()
    if settings.staff_auth_mode != "keycloak":
        raise _unauthenticated("Staff SSO is not enabled")
    if credentials is None or not credentials.credentials:
        raise _unauthenticated("Not authenticated")
    from keycloak_auth import get_validator
    try:
        return get_validator(settings.keycloak_issuer, settings.keycloak_api_audience,
                                settings.keycloak_cache_seconds, settings.keycloak_timeout_seconds,
                                allow_local_http=settings.is_local).decode(
                                    credentials.credentials)
    except jwt.InvalidTokenError:
        raise _unauthenticated() from None


def get_staff_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                   db=Depends(get_readonly_connection)) -> Principal:
    settings = get_settings()
    if settings.staff_auth_mode == "legacy":
        return get_current_user(request, credentials, db)
    payload = get_verified_staff_token(credentials)
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, disabled FROM staff_users WHERE issuer = %s AND subject = %s",
            (payload["iss"], payload["sub"]),
        )
        user = cursor.fetchone()
        if user is None:
            raise ApiError(403, "STAFF_NOT_REGISTERED", "Initialize the staff session first")
        if user.get("disabled"):
            raise ApiError(403, "ACCOUNT_DISABLED", "Inactive user")
        full_access = settings.staff_full_access
        if full_access:
            grants = frozenset(Grant(scope, "DSWD", "DSWD") for scope in _FULL_STAFF_SCOPES)
        else:
            grants = fetch_grants(db, user["id"], staff=True)
    except Error as exc:
        raise query_failed(exc, "staff_auth_lookup_failed")
    finally:
        cursor.close()
    username = payload.get("preferred_username")
    if not isinstance(username, str) or not username.strip() or len(username) > 255:
        username = "DSWD staff"
    return Principal(user["id"], username, grants,
                     datetime.fromtimestamp(payload["exp"], timezone.utc), kind="staff",
                     full_staff_access=full_access)


def get_beneficiary_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                              db=Depends(get_readonly_connection)) -> Principal:
    """Select one fixed validator; an unverified header grants no authority or fallback."""
    if credentials is None or not credentials.credentials:
        _denied(request, "missing_token")
        raise _unauthenticated("Not authenticated")
    try:
        if len(credentials.credentials) > 32768:
            raise jwt.InvalidTokenError()
        header = jwt.get_unverified_header(credentials.credentials)
        if header.get("crit"):
            raise jwt.InvalidTokenError()
        algorithm = header.get("alg")
    except jwt.InvalidTokenError:
        _denied(request, "invalid_token")
        raise _unauthenticated() from None
    if algorithm == ALGORITHM:
        return get_current_user(request, credentials, db)
    if algorithm == "RS256" and get_settings().staff_auth_mode == "keycloak":
        return get_staff_user(request, credentials, db)
    _denied(request, "invalid_token")
    raise _unauthenticated()


def require_scope(scope: str, *, denial_fields: dict | None = None, shared: bool = False):
    """Dependency factory: the caller must hold an active grant for ``scope`` (deny by default).

    ``denial_fields`` adds fixed, non-personal fields to this route's denial audit event; routes that
    omit it emit exactly the event they always did.
    """
    if scope not in KNOWN_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    dependency = get_beneficiary_principal if shared else get_current_user

    def checker(principal: Principal = Depends(dependency)) -> Principal:
        if not principal.grants_for(scope):
            # A grant held under an agency the data-owner rulings exclude is denied with its own reason.
            excluded = {"reason": "agency_not_permitted"} if any(g.scope == scope for g in principal.grants) else {}
            audit("authz.scope", "denied", actor=principal.actor, scope=scope,
                  **{**(denial_fields or {}), **excluded})
            raise ApiError(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Not permitted for this operation")
        return principal
    return checker


def get_settings_writer(principal: Principal = Depends(get_beneficiary_principal)) -> Principal:
    # Settings writes are permitted by the explicit temporary local/staging
    # full-access policy. Scoped staff mode still has no settings-write scope.
    if principal.kind == "staff" and not principal.full_staff_access:
        audit("authz.settings", "denied", actor=principal.actor)
        raise ApiError(403, "FORBIDDEN", "Not permitted for this operation")
    return principal
