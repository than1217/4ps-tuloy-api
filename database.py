"""MySQL access through bounded pools, optionally over the SSH tunnel.

Two pools share the runtime account:
- ``ro``: every session is READ ONLY with a SELECT time limit. Used for ODS views.
- ``rw``: used only for application-owned tables (api_users, CHED match tables).
Nothing here creates or alters schema; see migrations/ and scripts/migrate.py.
"""
import logging
import re
import threading
import time
from typing import Optional

import mysql.connector
from mysql.connector import Error
from mysql.connector.pooling import MySQLConnectionPool

from config import Settings, get_settings
from errors import ApiError
from observability import redact_text
from tunnel import Tunnel, TunnelError

logger = logging.getLogger("tuloy.db")

POOL_WAIT_SECONDS = 3.0
# After a failed tunnel/pool build, fail fast for this long instead of queueing every
# request behind another blocking SSH or MySQL connect attempt.
FAILURE_COOLDOWN_SECONDS = 5.0

_lock = threading.Lock()
_tunnel: Optional[Tunnel] = None
_pools: dict = {}
_pool_port: Optional[int] = None
_generation = 0
_down_until = 0.0

# Fixed labels for the connect failures seen in practice, so a log line says why without the
# server's message (which names the account). Client-side messages (2xxx, TLS, tunnel) carry only
# host:port and OpenSSL text and are logged as "detail" with any account name masked.
_CONNECT_REASONS = {
    1040: "too many connections",
    1045: "access denied for the configured account",
    1049: "unknown database",
    1130: "this host is not allowed to connect",
    2002: "cannot connect (socket)",
    2003: "cannot open a TCP connection",
    2013: "connection lost during handshake",
    2026: "TLS required but not available",
    2055: "connection lost during TLS or I/O",
    3159: "server requires TLS",
}
_ACCOUNT = re.compile(r"'[^']*'@'[^']*'")


def tls_mode(settings: Settings) -> str:
    """What the DB connection verifies: tls-unverified (no CA), verify-ca, or verify-identity."""
    if not settings.db_ssl_ca:
        return "tls-unverified"
    if settings.db_ssl_verify_identity and not settings.ssh_host:
        return "verify-identity"
    return "verify-ca"


def connect_failure(exc: BaseException, settings: Optional[Settings] = None) -> dict:
    """Safe log fields for a failed connect: type, errno, sqlstate, reason, client-side detail."""
    errno = getattr(exc, "errno", None)
    fields = {"exc_type": type(exc).__name__, "errno": errno, "sqlstate": getattr(exc, "sqlstate", None)}
    if errno in _CONNECT_REASONS:
        fields["reason"] = _CONNECT_REASONS[errno]
    client_side = isinstance(exc, TunnelError) or (
        isinstance(exc, Error) and (errno is None or errno == -1 or 2000 <= errno < 3000))
    if client_side:
        fields["detail"] = _ACCOUNT.sub("<account>", redact_text(str(exc)))[:300]
    if exc.__cause__ is not None:
        fields["cause"] = type(exc.__cause__).__name__
    if settings is not None:
        fields.update(target=f"{settings.ods.host}:{settings.ods.port}", tls=tls_mode(settings),
                      via_tunnel=bool(settings.ssh_host))
    return fields


def _endpoint(settings: Settings) -> tuple:
    global _tunnel
    if not settings.ssh_host:
        return settings.ods.host, settings.ods.port
    if _tunnel is None:
        _tunnel = Tunnel(settings, settings.ods.host, settings.ods.port)
    return "127.0.0.1", _tunnel.ensure()


def _discard_pools() -> None:
    """Drop the current pools and close their idle connections."""
    global _pools, _pool_port
    for pool in _pools.values():
        try:
            pool._remove_connections()
        except Exception:
            pass
    _pools = {}
    _pool_port = None


def _build_pools(settings: Settings) -> None:
    global _pools, _pool_port, _generation, _down_until
    try:
        host, port = _endpoint(settings)
        if _pools and port == _pool_port:
            return
        _discard_pools()
        _generation += 1
        base = dict(
            host=host,
            port=port,
            database=settings.ods.database,
            connection_timeout=settings.db_connect_timeout,
            read_timeout=max(5, settings.db_query_timeout_ms // 1000 + 5),
            write_timeout=30,
            autocommit=False,
            use_pure=True,
        )
        if settings.db_ssl_ca:
            base.update(ssl_ca=settings.db_ssl_ca, ssl_verify_cert=True, ssl_verify_identity=settings.db_ssl_verify_identity and not settings.ssh_host)
        size = settings.db_pool_size
        # Connections per process = size + size // 2, opened here. Fit this to the DB budget.
        ro = MySQLConnectionPool(pool_name=f"tuloy_ro_{_generation}", pool_size=size,
                                 user=settings.ods.user, password=settings.ods.password, **base)
        try:
            rw = MySQLConnectionPool(pool_name=f"tuloy_rw_{_generation}", pool_size=max(1, size // 2),
                                     user=settings.rw_user or settings.ods.user,
                                     password=settings.rw_password or settings.ods.password, **base)
        except Exception:
            ro._remove_connections()
            raise
        _pools = {"ro": ro, "rw": rw}
        _pool_port = port
        _down_until = 0.0
        logger.info("db_pools_ready", extra={"pool_size": size, "via_tunnel": bool(settings.ssh_host),
                                             "target": f"{settings.ods.host}:{settings.ods.port}",
                                             "tls": tls_mode(settings),
                                             "separate_rw_account": bool(settings.rw_user) and settings.rw_user != settings.ods.user})
    except Exception as exc:
        _down_until = time.monotonic() + FAILURE_COOLDOWN_SECONDS
        # Logged here, once per cooldown window: the retry in _checkout fails fast and never reaches
        # its own log line, so without this the errno and reason of a failed build are lost.
        logger.error("db_connect_failed", extra=connect_failure(exc, settings))
        raise


def init(settings: Optional[Settings] = None) -> None:
    """Start the tunnel and pools. Raises on failure; callers decide whether that is fatal."""
    settings = settings or get_settings()
    with _lock:
        _build_pools(settings)


def close() -> None:
    global _tunnel, _down_until
    with _lock:
        _discard_pools()
        _down_until = 0.0
        if _tunnel is not None:
            _tunnel.stop()
            _tunnel = None


def _unavailable() -> ApiError:
    return ApiError(503, "DB_UNAVAILABLE", "Service temporarily unavailable", headers={"Retry-After": "5"})


def _checkout(kind: str, settings: Settings):
    deadline = time.monotonic() + POOL_WAIT_SECONDS
    rebuilt = False
    while True:
        if time.monotonic() < _down_until:
            raise _unavailable()
        try:
            with _lock:
                _build_pools(settings)
                if kind == 'bcmd' and kind not in _pools:
                    # Lazy, separate pool: missing BCMD credentials do not break payroll/login.
                    host, port = _endpoint(settings)
                    options = dict(host=host, port=port, database=settings.ods.database,
                        user=settings.bcmd_user, password=settings.bcmd_password,
                        connection_timeout=settings.db_connect_timeout,
                        read_timeout=max(5, settings.db_query_timeout_ms // 1000 + 5),
                        write_timeout=30, autocommit=False, use_pure=True)
                    if settings.db_ssl_ca:
                        options.update(ssl_ca=settings.db_ssl_ca, ssl_verify_cert=True,
                            ssl_verify_identity=settings.db_ssl_verify_identity and not settings.ssh_host)
                    _pools[kind] = MySQLConnectionPool(pool_name=f'tuloy_bcmd_{_generation}',
                        pool_size=max(1, settings.db_pool_size // 2), **options)
                pool = _pools[kind]
            conn = pool.get_connection()
            break
        except TunnelError:
            raise _unavailable()
        except mysql.connector.errors.PoolError:
            if time.monotonic() > deadline:
                raise ApiError(503, "DB_BUSY", "Service busy, retry later", headers={"Retry-After": "2"})
            time.sleep(0.05)
        except Error as exc:
            if kind == 'bcmd':
                # A BCMD login/permission failure must never tear down payroll's
                # or authentication's pools (or their shared SSH transport).
                with _lock:
                    failed_pool = _pools.pop('bcmd', None)
                    if failed_pool is not None:
                        try:
                            failed_pool._remove_connections()
                        except Exception:
                            pass
                logger.error('bcmd_connect_failed', extra={'errno': getattr(exc, 'errno', None)})
                raise _unavailable()
            # Stale pool or dead tunnel: rebuild once (fresh tunnel when one is used), then give up.
            if not rebuilt:
                rebuilt = True
                with _lock:
                    _discard_pools()
                    if _tunnel is not None:
                        _tunnel.stop()
                continue
            logger.error("db_connect_failed", extra=connect_failure(exc, settings))
            raise _unavailable()

    cursor = conn.cursor()
    try:
        if kind == "ro":
            cursor.execute(
                "SET SESSION transaction_read_only = ON, max_execution_time = %s",
                (settings.db_query_timeout_ms,),
            )
        else:
            cursor.execute(
                "SET SESSION transaction_read_only = OFF, max_execution_time = %s, innodb_lock_wait_timeout = 10",
                (settings.db_query_timeout_ms,),
            )
    except Error as exc:
        conn.close()
        logger.error("db_session_init_failed", extra={"errno": getattr(exc, "errno", None)})
        raise ApiError(503, "DB_UNAVAILABLE", "Service temporarily unavailable")
    finally:
        cursor.close()
    return conn


def _release(conn) -> None:
    try:
        if conn.is_connected():
            conn.rollback()  # never return an open transaction to the pool
    except Error:
        pass
    finally:
        try:
            conn.close()
        except Error:
            pass


def get_ods_connection():
    """FastAPI dependency: read-only connection for ODS views."""
    conn = _checkout("ro", get_settings())
    try:
        yield conn
    finally:
        _release(conn)


def get_app_connection():
    """FastAPI dependency: read-write connection for application-owned tables."""
    conn = _checkout("rw", get_settings())
    try:
        yield conn
    finally:
        _release(conn)


def get_bcmd_connection(settings):
    """Dedicated BCMD account; never substitute the application or migration login."""
    conn = _checkout('bcmd', settings)
    try:
        yield conn
    finally:
        _release(conn)


# Backwards-compatible name used by existing imports.
get_db_connection = get_app_connection
# Read-only access to application tables (auth lookups) shares the ODS read-only pool.
get_readonly_connection = get_ods_connection


def query_failed(exc: Error, event: str) -> ApiError:
    """Log a DB error without its message (it can contain SQL values) and map it to an API error."""
    errno = getattr(exc, "errno", None)
    logger.error(event, extra={"errno": errno, "sqlstate": getattr(exc, "sqlstate", None)})
    if errno == 3024:  # ER_QUERY_TIMEOUT (max_execution_time exceeded)
        return ApiError(503, "DB_TIMEOUT", "Query took too long, narrow the request and retry")
    if errno in (2003, 2006, 2013):  # CR_CONN_HOST_ERROR, CR_SERVER_GONE_ERROR, CR_SERVER_LOST
        return ApiError(503, "DB_UNAVAILABLE", "Service temporarily unavailable")
    return ApiError(500, "DB_ERROR", "Database error")


def ping() -> dict:
    """Readiness probe: tunnel state plus SELECT 1 on a read-only session. Never raises."""
    settings = get_settings()
    result = {"database": "down", "tunnel": "n/a" if not settings.ssh_host else "down"}
    try:
        conn = _checkout("ro", settings)
    except ApiError:
        return result
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchall()
        cur.close()
        result["database"] = "up"
    except Error:
        pass
    finally:
        _release(conn)
    if settings.ssh_host and _tunnel is not None and _tunnel.is_up:
        result["tunnel"] = "up"
    return result
