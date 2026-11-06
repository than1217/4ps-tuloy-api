import dataclasses

import paramiko
import pytest
from mysql.connector.errors import InterfaceError, PoolError

import database
import tunnel as tunnel_mod
from config import get_settings
from errors import ApiError


@pytest.fixture
def known_hosts(tmp_path):
    key = paramiko.Ed25519Key.generate() if hasattr(paramiko.Ed25519Key, "generate") else None
    if key is None:
        key = paramiko.ECDSAKey.generate()
    path = tmp_path / "known_hosts"
    path.write_text(f"203.0.113.10 {key.get_name()} {key.get_base64()}\n")
    return str(path), key


def ssh_settings(known_hosts_path):
    return dataclasses.replace(get_settings(), ssh_host="203.0.113.10", ssh_port=22, ssh_user="u",
                               ssh_password="p", ssh_known_hosts=known_hosts_path)


def test_host_key_must_be_pinned(tmp_path):
    empty = tmp_path / "known_hosts"
    empty.write_text("")
    with pytest.raises(tunnel_mod.TunnelError, match="not in known_hosts"):
        tunnel_mod.load_host_key("203.0.113.10", 22, str(empty))


def test_host_key_loaded(known_hosts):
    path, key = known_hosts
    assert tunnel_mod.load_host_key("203.0.113.10", 22, path).asbytes() == key.asbytes()


class FakeForwarder:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_active = False
        self.local_bind_port = 40000 + len(FakeForwarder.instances)
        self._transport = type("T", (), {"is_active": lambda s: True})()
        self.stopped = False
        FakeForwarder.instances.append(self)

    def start(self):
        self.is_active = True

    def stop(self, force=False):
        self.is_active = False
        self.stopped = True


def test_tunnel_is_verified_loopback_and_restartable(monkeypatch, known_hosts):
    path, key = known_hosts
    FakeForwarder.instances = []
    monkeypatch.setattr(tunnel_mod, "SSHTunnelForwarder", FakeForwarder)
    t = tunnel_mod.Tunnel(ssh_settings(path), "192.0.2.10", 3306)
    port = t.ensure()
    kw = FakeForwarder.instances[0].kwargs
    assert kw["ssh_host_key"].asbytes() == key.asbytes()
    assert kw["local_bind_address"] == ("127.0.0.1", 0)
    assert kw["allow_agent"] is False and kw["host_pkey_directories"] == [] and kw["ssh_config_file"] is None
    assert kw["remote_bind_address"] == ("192.0.2.10", 3306)
    # Transport dies -> ensure() builds a new forwarder on a new port and stops the old one.
    FakeForwarder.instances[0].is_active = False
    assert t.ensure() != port and FakeForwarder.instances[0].stopped
    t.stop()
    assert FakeForwarder.instances[1].stopped and not t.is_up
    t.stop()  # idempotent


def test_tunnel_start_failure_hides_details(monkeypatch, known_hosts):
    path, _ = known_hosts

    class Failing(FakeForwarder):
        def start(self):
            raise RuntimeError("auth failed for user secret-user@198.51.100.20")
    monkeypatch.setattr(tunnel_mod, "SSHTunnelForwarder", Failing)
    t = tunnel_mod.Tunnel(ssh_settings(path), "192.0.2.10", 3306)
    with pytest.raises(tunnel_mod.TunnelError) as err:
        t.start()
    assert "secret-user" not in str(err.value)


class FakeConn:
    def __init__(self):
        self.statements, self.rolled_back, self.closed = [], 0, False

    def cursor(self):
        conn = self

        class C:
            def execute(self, sql, params=()):
                conn.statements.append((sql, params))

            def close(self):
                pass
        return C()

    def is_connected(self):
        return True

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def get_connection(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def pools(monkeypatch):
    def install(ro=(), rw=()):
        fake = {"ro": FakePool(ro), "rw": FakePool(rw)}

        def build(settings):
            # A rebuild after discard gets the same fakes back, so scripted outcomes continue.
            if not database._pools:
                database._pools = dict(fake)
        monkeypatch.setattr(database, "_build_pools", build)
        monkeypatch.setattr(database, "_pools", dict(fake))
        monkeypatch.setattr(database, "_down_until", 0.0)
        monkeypatch.setattr(database, "POOL_WAIT_SECONDS", 0.2)
    return install


def test_readonly_session_and_release(pools):
    conn = FakeConn()
    pools(ro=[conn])
    gen = database.get_ods_connection()
    assert next(gen) is conn
    assert "transaction_read_only = ON" in conn.statements[0][0]
    with pytest.raises(StopIteration):
        next(gen)
    assert conn.rolled_back == 1 and conn.closed


def test_pool_exhaustion_waits_then_503(pools):
    conn = FakeConn()
    pools(rw=[PoolError("exhausted"), conn])
    assert database._checkout("rw", get_settings()) is conn
    pools(rw=[PoolError("exhausted")] * 100)
    with pytest.raises(ApiError) as err:
        database._checkout("rw", get_settings())
    assert err.value.status_code == 503 and err.value.error_code == "DB_BUSY"


def test_db_outage_is_503(pools):
    pools(ro=[InterfaceError(msg="Can't connect to 192.0.2.10", errno=2003)] * 3)
    with pytest.raises(ApiError) as err:
        database._checkout("ro", get_settings())
    assert err.value.status_code == 503 and "192.0.2.10" not in err.value.detail


def test_stale_connection_rebuilds_once(pools):
    conn = FakeConn()
    pools(ro=[InterfaceError(msg="Lost connection", errno=2013), conn])
    assert database._checkout("ro", get_settings()) is conn


def test_failed_build_starts_cooldown(monkeypatch):
    calls = []

    def endpoint(settings):
        calls.append(1)
        raise tunnel_mod.TunnelError("down")
    monkeypatch.setattr(database, "_endpoint", endpoint)
    monkeypatch.setattr(database, "_pools", {})
    monkeypatch.setattr(database, "_down_until", 0.0)
    for _ in range(3):
        with pytest.raises(ApiError) as err:
            database._checkout("ro", get_settings())
        assert err.value.status_code == 503
    # Only the first request paid for a connect attempt; the rest failed fast.
    assert len(calls) == 1
    monkeypatch.setattr(database, "_down_until", 0.0)


def test_tunnel_outage_is_503(monkeypatch):
    def broken(settings):
        raise tunnel_mod.TunnelError("SSH tunnel failed to start (SSHException)")
    monkeypatch.setattr(database, "_build_pools", broken)
    with pytest.raises(ApiError) as err:
        database._checkout("ro", get_settings())
    assert err.value.status_code == 503


def test_ping_never_raises(monkeypatch):
    def broken(settings):
        raise tunnel_mod.TunnelError("down")
    monkeypatch.setattr(database, "_build_pools", broken)
    assert database.ping()["database"] == "down"


# --- Connect-failure diagnostics: errno and reason are logged, account names never are ---

def _ssl_failure():
    import ssl
    try:
        raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                              "certificate signature failure (_ssl.c:1010)")
    except ssl.SSLError as cause:
        try:
            raise InterfaceError(errno=2055, values=("192.0.2.10:3306", f"Errno 1: {cause}")) from cause
        except InterfaceError as exc:
            return exc


def test_connect_failure_reports_tls_reason():
    fields = database.connect_failure(_ssl_failure(), get_settings())
    assert fields["exc_type"] == "InterfaceError" and fields["errno"] == 2055
    assert fields["cause"] == "SSLCertVerificationError"
    assert "CERTIFICATE_VERIFY_FAILED" in fields["detail"] and "certificate signature failure" in fields["detail"]
    assert fields["tls"] in ("tls-unverified", "verify-ca", "verify-identity")
    assert "target" in fields and "via_tunnel" in fields


def test_connect_failure_never_logs_server_message():
    from mysql.connector.errors import ProgrammingError
    exc = ProgrammingError(msg="Access denied for user 'tuloy_api'@'192.0.2.7' (using password: YES)",
                           errno=1045, sqlstate="28000")
    fields = database.connect_failure(exc)
    assert fields["errno"] == 1045 and fields["reason"].startswith("access denied")
    assert "detail" not in fields and "tuloy_api" not in str(fields)


def test_connect_failure_redacts_account_in_client_message():
    exc = InterfaceError(msg="Authentication failed for 'tuloy_api'@'192.0.2.7'", errno=2059)
    assert "tuloy_api" not in database.connect_failure(exc)["detail"]


def test_tls_mode_names_what_is_verified():
    s = get_settings()
    assert database.tls_mode(dataclasses.replace(s, db_ssl_ca=None)) == "tls-unverified"
    assert database.tls_mode(dataclasses.replace(s, db_ssl_ca="ca.pem", db_ssl_verify_identity=False,
                                                 ssh_host=None)) == "verify-ca"
    assert database.tls_mode(dataclasses.replace(s, db_ssl_ca="ca.pem", db_ssl_verify_identity=True,
                                                 ssh_host=None)) == "verify-identity"


def test_failed_pool_build_logs_errno_and_reason(monkeypatch, caplog):
    import logging

    def failing_pool(**kwargs):
        raise _ssl_failure()
    monkeypatch.setattr(database, "MySQLConnectionPool", failing_pool)
    monkeypatch.setattr(database, "_pools", {})
    monkeypatch.setattr(database, "_down_until", 0.0)
    settings = dataclasses.replace(get_settings(), ssh_host=None)
    with caplog.at_level(logging.ERROR, logger="tuloy.db"):
        with pytest.raises(InterfaceError):
            database._build_pools(settings)
    rec = next(r for r in caplog.records if r.getMessage() == "db_connect_failed")
    assert rec.errno == 2055 and "certificate signature failure" in rec.detail
    assert settings.ods.password not in str(vars(rec)) and settings.ods.user not in str(vars(rec))
    monkeypatch.setattr(database, "_down_until", 0.0)
