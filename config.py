"""Typed settings, loaded and validated once from the environment (.env locally).

Secrets are excluded from repr() so a logged Settings object never shows them.
"""
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv

ENVIRONMENTS = ("local", "dev", "uat", "staging", "prod")
# Reviewed deployment profile. No credentials are embedded in code.
NAPA_STAGING_DATABASE = 'dswd_4ps_tuloy_dev'
# Staging's database address is a load balancer over a multi-master cluster (DBA, 2026-11-06): a
# commit is confirmed only once it is the same on every node, so BCMD may run on either node.
NAPA_STAGING_HOSTNAMES = ('ppisv2-v3Master', 'ppisv2-v3slaveG13')
# Hard cap on a request body. main.py rejects a larger declared Content-Length; R3 also enforces it while streaming.
MAX_BODY_BYTES = 1_000_000
DEFAULT_SECRET_PLACEHOLDERS = {"your-super-secret-key-for-jwt", "changeme", "secret"}


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or inconsistent."""


def valid_keycloak_issuer(issuer: str, *, allow_local_http: bool = False) -> bool:
    """HTTP is allowed only for an explicitly local validator on exact loopback hosts."""
    try:
        parsed = urlsplit(issuer)
        parsed.port  # Reject malformed ports before any network request.
        local_http = (allow_local_http and parsed.scheme == "http"
                      and parsed.hostname in ("localhost", "127.0.0.1", "::1"))
        return bool((parsed.scheme == "https" or local_http) and parsed.hostname
                    and not parsed.username and not parsed.password and not parsed.query
                    and not parsed.fragment and not issuer.endswith("/"))
    except (ValueError, TypeError):
        return False


def _str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(name: str, default: int, minimum: int = 0, maximum: int = 2**31) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _list(name: str, default: str = "") -> tuple:
    raw = _str(name, default) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class DbTarget:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    app_env: str
    # The configured schema holds ODS views (read through READ ONLY sessions) and the
    # application-owned tables (api_users, CHED match tables). One runtime account.
    ods: DbTarget
    # Optional separate account for the write pool (app tables only). Defaults to DB_USER.
    rw_user: str = ""
    rw_password: str = field(default="", repr=False)
    db_ssl_ca: Optional[str] = None
    # Check the MySQL certificate's host name on a direct connection. Off for an IP-addressed
    # server with MySQL's auto-generated certificate (the chain is still verified against DB_SSL_CA).
    db_ssl_verify_identity: bool = True
    # Separate DDL account for scripts/migrate.py only; never used by the API.
    migration_user: Optional[str] = None
    migration_password: Optional[str] = field(default=None, repr=False)
    # SSH bastion (optional)
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    ssh_user: Optional[str] = None
    ssh_password: Optional[str] = field(default=None, repr=False)
    ssh_pkey: Optional[str] = None
    ssh_known_hosts: str = ""
    ssh_keepalive_seconds: int = 30
    # Database behaviour
    db_connect_timeout: int = 10
    db_query_timeout_ms: int = 15000
    db_pool_size: int = 4
    # Auth
    secret_key: str = field(default="", repr=False)
    secret_key_ephemeral: bool = False
    access_token_expire_minutes: int = 30
    jwt_issuer: str = "tuloy-api"
    jwt_audience: str = "tuloy-api"
    allow_signup: bool = False
    staff_auth_mode: str = "legacy"
    staff_full_access_enabled: bool = False
    keycloak_issuer: str = ""
    keycloak_api_audience: str = ""
    keycloak_cache_seconds: int = 300
    keycloak_timeout_seconds: int = 5
    # HTTP
    cors_origins: tuple = ()
    trusted_hosts: tuple = ("127.0.0.1", "localhost")
    rate_limit_per_minute: int = 120
    expose_docs: bool = True
    # Cache
    redis_url: Optional[str] = None
    cache_expire_seconds: int = 1200
    # Partner API (/v1/api/tuloy); operations stay 501 until their contract is approved
    partner_api_enabled: bool = False
    # POST /PostAppSettings rewrites login-page branding for everyone; off until its access rule is ruled
    settings_write_enabled: bool = False
    # Disabled by default; non-local use requires identity/schema/privilege checks.
    bcmd_enabled: bool = False
    bcmd_user: str = ""
    bcmd_password: str = field(default="", repr=False)
    bcmd_expected_database: str = ""
    bcmd_expected_hostname: str = ""
    bcmd_use_existing_account: bool = True
    log_level: str = "INFO"

    @property
    def staff_full_access(self) -> bool:
        # Defense in depth: production cannot obtain full staff access even from
        # a Settings instance constructed without load_settings().
        return (self.staff_full_access_enabled and self.staff_auth_mode == "keycloak"
                and self.app_env in ("local", "staging"))

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"

    @property
    def bcmd_hostnames(self) -> tuple:
        """Server names BCMD may run on: BCMD_EXPECT_HOSTNAME, comma-separated. Empty: no check."""
        return tuple(name.strip() for name in self.bcmd_expected_hostname.split(",") if name.strip())


def _db_target(prefix: str) -> Optional[DbTarget]:
    host = _str(f"{prefix}HOST")
    if host is None:
        return None
    missing = [k for k in ("DATABASE", "USER", "PASSWORD") if _str(f"{prefix}{k}") is None]
    if missing:
        raise ConfigError(f"{prefix}HOST is set but {', '.join(prefix + m for m in missing)} missing")
    return DbTarget(
        host=host,
        port=_int(f"{prefix}PORT", 3306, 1, 65535),
        database=_str(f"{prefix}DATABASE"),
        user=_str(f"{prefix}USER"),
        password=_str(f"{prefix}PASSWORD"),
    )


def load_settings() -> Settings:
    """Build Settings from the environment, failing fast on anything invalid."""
    # Real environment variables win over the file. TULOY_ENV_FILE="" disables the file.
    env_file = os.getenv("TULOY_ENV_FILE", ".env")
    if env_file:
        load_dotenv(env_file, override=False)

    # No default: a deployment that forgets APP_ENV must not silently get local-only leniency.
    app_env = (_str("APP_ENV") or "").lower()
    if app_env not in ENVIRONMENTS:
        raise ConfigError(f"APP_ENV must be one of {', '.join(ENVIRONMENTS)}")

    ods = _db_target("DB_")
    if ods is None:
        raise ConfigError("DB_HOST, DB_DATABASE, DB_USER and DB_PASSWORD are required")

    ssh_host = _str("SSH_HOST")
    ssh_user = _str("SSH_USER")
    ssh_password = _str("SSH_PASSWORD")
    ssh_pkey = _str("SSH_PKEY")
    if ssh_host:
        if not ssh_user:
            raise ConfigError("SSH_USER is required when SSH_HOST is set")
        if not ssh_password and not ssh_pkey:
            raise ConfigError("Set SSH_PASSWORD or SSH_PKEY when SSH_HOST is set")
        if ssh_pkey and not os.path.exists(os.path.expanduser(ssh_pkey)):
            raise ConfigError("SSH_PKEY does not point to an existing file")
    known_hosts = os.path.expanduser(_str("SSH_KNOWN_HOSTS", "~/.ssh/known_hosts"))
    if ssh_host and not os.path.exists(known_hosts):
        raise ConfigError("SSH_KNOWN_HOSTS file not found; host-key verification is mandatory")

    secret_key = _str("SECRET_KEY")
    ephemeral = False
    if secret_key is None or secret_key in DEFAULT_SECRET_PLACEHOLDERS:
        if app_env != "local":
            raise ConfigError("SECRET_KEY must be set to a unique value outside local")
        # Local only: random per-process key, so tokens die with the process.
        secret_key = secrets.token_urlsafe(48)
        ephemeral = True
    elif len(secret_key) < 32:
        raise ConfigError("SECRET_KEY must be at least 32 characters")

    rw_user = _str("DB_RW_USER") or ods.user
    rw_password = _str("DB_RW_PASSWORD") or (ods.password if rw_user == ods.user else None)
    if rw_password is None:
        raise ConfigError("DB_RW_PASSWORD is required when DB_RW_USER is set")
    db_ssl_ca = _str("DB_SSL_CA")
    if db_ssl_ca and not os.path.exists(os.path.expanduser(db_ssl_ca)):
        raise ConfigError("DB_SSL_CA does not point to an existing file")
    redis_url = _str("REDIS_URL")
    log_level = (_str("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    trusted_hosts_raw = _str("TRUSTED_HOSTS")

    # Preserve existing deployments until staff SSO is explicitly enabled.
    staff_auth_mode = _str("STAFF_AUTH_MODE", "legacy")
    if staff_auth_mode not in ("legacy", "keycloak"):
        raise ConfigError("STAFF_AUTH_MODE must be legacy or keycloak")
    staff_full_access_enabled = _bool("STAFF_FULL_ACCESS_ENABLED", False)
    if staff_full_access_enabled and (app_env not in ("local", "staging") or staff_auth_mode != "keycloak"):
        raise ConfigError("STAFF_FULL_ACCESS_ENABLED requires Keycloak SSO in local/staging only")
    keycloak_issuer = _str("KEYCLOAK_ISSUER", "")
    keycloak_api_audience = _str("KEYCLOAK_API_AUDIENCE", "")
    if staff_auth_mode == "keycloak":
        if not valid_keycloak_issuer(keycloak_issuer, allow_local_http=app_env == "local"):
            raise ConfigError("KEYCLOAK_ISSUER requires HTTPS (local permits loopback HTTP), without a trailing slash")
        if not keycloak_api_audience:
            raise ConfigError("KEYCLOAK_API_AUDIENCE is required in keycloak mode")

    cors = _list("CORS_ORIGINS")
    allow_signup = _bool("ALLOW_SIGNUP", False)
    expose_docs = _bool("EXPOSE_DOCS", app_env in ("local", "dev"))
    if app_env not in ("local", "dev"):
        if "*" in cors:
            raise ConfigError("CORS_ORIGINS may not contain * outside local/dev")
        if allow_signup:
            raise ConfigError("ALLOW_SIGNUP must be false outside local/dev")
        if not trusted_hosts_raw or "*" in _list("TRUSTED_HOSTS"):
            raise ConfigError("TRUSTED_HOSTS must list the served host names outside local/dev (no *)")
        if redis_url and not redis_url.startswith("rediss://"):
            raise ConfigError("REDIS_URL must use rediss:// (TLS) outside local/dev")
        if (_str("MIGRATION_DB_USER") or "") == ods.user:
            raise ConfigError("MIGRATION_DB_USER must differ from the runtime DB_USER outside local/dev")

    bcmd_use_existing_account = _bool("BCMD_USE_EXISTING_ACCOUNT", True)
    bcmd_user = _str("BCMD_DB_USER", "")
    bcmd_password = _str("BCMD_DB_PASSWORD", "")
    if bcmd_use_existing_account and not bcmd_user and not bcmd_password:
        bcmd_user = rw_user or ods.user
        bcmd_password = rw_password or ods.password

    # Martin approved activation through the existing main deployment pipeline,
    # without a sysadmin/env edit. Other databases and local mode stay off by default.
    napa_staging_profile = app_env != 'local' and ods.database == NAPA_STAGING_DATABASE

    return Settings(
        app_env=app_env,
        ods=ods,
        rw_user=rw_user,
        rw_password=rw_password,
        db_ssl_ca=db_ssl_ca,
        db_ssl_verify_identity=_bool("DB_SSL_VERIFY_IDENTITY", True),
        migration_user=_str("MIGRATION_DB_USER"),
        migration_password=_str("MIGRATION_DB_PASSWORD"),
        ssh_host=ssh_host,
        ssh_port=_int("SSH_PORT", 22, 1, 65535),
        ssh_user=ssh_user,
        ssh_password=ssh_password,
        ssh_pkey=ssh_pkey,
        ssh_known_hosts=known_hosts,
        ssh_keepalive_seconds=_int("SSH_KEEPALIVE_SECONDS", 30, 0, 600),
        db_connect_timeout=_int("DB_CONNECT_TIMEOUT", 10, 1, 120),
        db_query_timeout_ms=_int("DB_QUERY_TIMEOUT_MS", 15000, 100, 600000),
        db_pool_size=_int("DB_POOL_SIZE", 4, 1, 32),
        secret_key=secret_key,
        secret_key_ephemeral=ephemeral,
        access_token_expire_minutes=_int("ACCESS_TOKEN_EXPIRE_MINUTES", 30, 1, 1440),
        jwt_issuer=_str("JWT_ISSUER", "tuloy-api"),
        jwt_audience=_str("JWT_AUDIENCE", "tuloy-api"),
        allow_signup=allow_signup,
        staff_auth_mode=staff_auth_mode,
        staff_full_access_enabled=staff_full_access_enabled,
        keycloak_issuer=keycloak_issuer,
        keycloak_api_audience=keycloak_api_audience,
        keycloak_cache_seconds=_int("KEYCLOAK_CACHE_SECONDS", 300, 30, 3600),
        keycloak_timeout_seconds=_int("KEYCLOAK_TIMEOUT_SECONDS", 5, 1, 15),
        cors_origins=cors,
        trusted_hosts=_list("TRUSTED_HOSTS", "127.0.0.1,localhost"),
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 120, 0, 100000),
        expose_docs=expose_docs,
        redis_url=redis_url,
        cache_expire_seconds=_int("CACHE_EXPIRE_SECONDS", 1200, 0, 86400),
        partner_api_enabled=_bool("PARTNER_API_ENABLED", False),
        settings_write_enabled=_bool("SETTINGS_WRITE_ENABLED", False),
        bcmd_enabled=_bool("BCMD_ENABLED", napa_staging_profile),
        bcmd_user=bcmd_user,
        bcmd_password=bcmd_password,
        bcmd_expected_database=_str("BCMD_EXPECT_DATABASE", NAPA_STAGING_DATABASE if napa_staging_profile else ""),
        bcmd_expected_hostname=_str("BCMD_EXPECT_HOSTNAME",
                                    ",".join(NAPA_STAGING_HOSTNAMES) if napa_staging_profile else ""),
        bcmd_use_existing_account=bcmd_use_existing_account,
        log_level=log_level,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
