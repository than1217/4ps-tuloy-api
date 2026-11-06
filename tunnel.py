"""SSH tunnel through the bastion with mandatory host-key verification.

The local end binds to 127.0.0.1 on an ephemeral port. Only the configured
password or key is offered: no SSH agent, no ~/.ssh key scan, no ssh_config.
"""
import logging
import socket
import threading
from typing import Optional

import paramiko
from sshtunnel import SSHTunnelForwarder

logger = logging.getLogger("tuloy.tunnel")

HOST_KEY_PREFERENCE = ("ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "ssh-rsa")


class TunnelError(RuntimeError):
    pass


def load_host_key(host: str, port: int, known_hosts_path: str) -> paramiko.PKey:
    """Return the pinned host key for host:port from an OpenSSH known_hosts file."""
    try:
        keys = paramiko.HostKeys(known_hosts_path)
    except (IOError, paramiko.SSHException) as exc:
        raise TunnelError("known_hosts file could not be read") from exc
    entry = keys.lookup(host if port == 22 else f"[{host}]:{port}")
    if not entry:
        raise TunnelError(
            "Bastion host key is not in known_hosts. Verify its fingerprint out of band, "
            "then add it (ssh-keyscan output checked against the published fingerprint)."
        )
    for key_type in HOST_KEY_PREFERENCE:
        if key_type in entry:
            return entry[key_type]
    return next(iter(entry.values()))


def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Tunnel:
    """Owns one SSHTunnelForwarder and restarts it on demand (thread-safe)."""

    def __init__(self, settings, remote_host: str, remote_port: int):
        self._settings = settings
        self._remote = (remote_host, remote_port)
        self._lock = threading.Lock()
        self._forwarder: Optional[SSHTunnelForwarder] = None
        self.host_key = load_host_key(settings.ssh_host, settings.ssh_port, settings.ssh_known_hosts)

    def _build(self) -> SSHTunnelForwarder:
        s = self._settings
        kwargs = dict(
            ssh_address_or_host=(s.ssh_host, s.ssh_port),
            ssh_username=s.ssh_user,
            ssh_host_key=self.host_key,
            remote_bind_address=self._remote,
            local_bind_address=("127.0.0.1", 0),
            allow_agent=False,
            host_pkey_directories=[],
            ssh_config_file=None,
            set_keepalive=float(s.ssh_keepalive_seconds),
            mute_exceptions=False,
        )
        if s.ssh_pkey:
            kwargs["ssh_pkey"] = s.ssh_pkey
        if s.ssh_password:
            kwargs["ssh_password"] = s.ssh_password
        forwarder = SSHTunnelForwarder(**kwargs)
        forwarder.daemon_forward_servers = True
        forwarder.daemon_transport = True
        return forwarder

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self._forwarder is not None and self._forwarder.is_active:
            return
        self._stop_locked()
        forwarder = self._build()
        try:
            forwarder.start()
        except Exception as exc:  # sshtunnel raises several unrelated types
            try:
                forwarder.stop()
            except Exception:
                pass
            # Never include exc text verbatim: paramiko messages can echo usernames.
            raise TunnelError(f"SSH tunnel failed to start ({type(exc).__name__})") from exc
        self._forwarder = forwarder
        logger.info("ssh_tunnel_started", extra={"local_port": forwarder.local_bind_port})

    def ensure(self) -> int:
        """Return the local port, restarting the tunnel if the transport died."""
        with self._lock:
            if self._forwarder is None:
                self._start_locked()
            elif not self._forwarder.is_active or not self._transport_alive():
                logger.warning("ssh_tunnel_restarting")
                self._start_locked()
            return self._forwarder.local_bind_port

    def _transport_alive(self) -> bool:
        transport = getattr(self._forwarder, "_transport", None)
        return bool(transport and transport.is_active())

    @property
    def is_up(self) -> bool:
        return self._forwarder is not None and self._forwarder.is_active and self._transport_alive()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._forwarder is not None:
            try:
                self._forwarder.stop(force=True)
            except TypeError:  # older sshtunnel without force=
                self._forwarder.stop()
            except Exception:
                logger.exception("ssh_tunnel_stop_failed")
            self._forwarder = None
            logger.info("ssh_tunnel_stopped")
