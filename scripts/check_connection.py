"""Staged, read-only connection check: bastion -> forward -> DB auth -> bounded queries.

Run from the repo root:  .\\.venv\\Scripts\\python.exe -m scripts.check_connection
Prints PASS/FAIL per stage. Never prints credentials, user names, or row values.
Exit code: 0 all passed, otherwise the number of the first failing stage.
"""
import base64
import hashlib
import re
import sys
import time

import mysql.connector
import paramiko

from config import ConfigError, load_settings
from tunnel import Tunnel, TunnelError, load_host_key, tcp_reachable

REQUIRED_OBJECTS = (
    # ODS source objects read by the API
    "vw_verify_member_bene",
    "vw_get_matched_CHED",
    # Application-owned tables the legacy routes use
    "api_users",
    "matching_transaction_CHED_tbl",
    "enroll_matched_CHED_tbl",
    # FK target referenced by enroll_matched_CHED_tbl
    "education_enrollment",
)
VERIFY_VIEW_COLUMNS = (
    "match_keys", "first_name", "middle_name", "last_name", "date_of_birth", "sex_id",
    "member_id", "listahanan_id", "lrn", "philsys", "bgy_psgc_code", "tuloy_id",
)


def report(stage: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {stage}{' - ' + detail if detail else ''}")
    return ok


def redact_grant(grant: str) -> str:
    grant = re.sub(r"\bTO\s+\S+", "TO <account>", grant, flags=re.I)
    return re.sub(r"IDENTIFIED BY\s+\S+", "IDENTIFIED BY <redacted>", grant, flags=re.I)


def main() -> int:
    try:
        s = load_settings()
    except ConfigError as exc:
        report("0 config", False, str(exc))
        return 10
    report("0 config", True, f"APP_ENV={s.app_env}, ssh={'on' if s.ssh_host else 'off'}, "
           f"ssh_auth={'key' if s.ssh_pkey else 'password'}, migration_account={'set' if s.migration_user else 'not set'}")

    tunnel = None
    host, port = s.ods.host, s.ods.port
    try:
        if s.ssh_host:
            if not report("a bastion TCP reachability", tcp_reachable(s.ssh_host, s.ssh_port),
                          f"port {s.ssh_port}"):
                return 1
            try:
                pinned = load_host_key(s.ssh_host, s.ssh_port, s.ssh_known_hosts)
            except TunnelError as exc:
                report("b ssh host key + auth", False, str(exc))
                return 2
            fp = base64.b64encode(hashlib.sha256(pinned.asbytes()).digest()).decode().rstrip("=")
            client = paramiko.SSHClient()
            client.load_host_keys(s.ssh_known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                client.connect(
                    s.ssh_host, port=s.ssh_port, username=s.ssh_user, password=s.ssh_password,
                    key_filename=s.ssh_pkey, allow_agent=False, look_for_keys=False,
                    timeout=10, banner_timeout=15, auth_timeout=15,
                )
                server_key = client.get_transport().get_remote_server_key()
                report("b ssh host key + auth", True,
                       f"host key verified against known_hosts ({server_key.get_name()}; pinned SHA256:{fp} for {pinned.get_name()})")
            except paramiko.BadHostKeyException:
                report("b ssh host key + auth", False, "HOST KEY MISMATCH - possible spoofing; stop and verify out of band")
                return 2
            except paramiko.AuthenticationException:
                report("b ssh host key + auth", False, "authentication rejected")
                return 2
            except Exception as exc:
                report("b ssh host key + auth", False, type(exc).__name__)
                return 2
            finally:
                client.close()

            try:
                tunnel = Tunnel(s, s.ods.host, s.ods.port)
                tunnel.start()
                host, port = "127.0.0.1", tunnel.ensure()
            except TunnelError as exc:
                report("c forward to DB host", False, str(exc))
                return 3
            if not report("c forward to DB host", tcp_reachable(host, port),
                          f"127.0.0.1:<ephemeral> -> ODS host:{s.ods.port}"):
                return 3
        else:
            report("a/b/c ssh", True, "skipped (SSH_HOST not set)")

        try:
            conn = mysql.connector.connect(
                host=host, port=port, database=s.ods.database, user=s.ods.user,
                password=s.ods.password, connection_timeout=s.db_connect_timeout, autocommit=True,
            )
        except mysql.connector.Error as exc:
            report("d database authentication", False, f"MySQL error {exc.errno}")
            return 4
        report("d database authentication", True, f"server {conn.get_server_info()}")

        try:
            cur = conn.cursor()
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET SESSION max_execution_time = %s", (s.db_query_timeout_ms,))
            t0 = time.perf_counter()
            cur.execute("SELECT 1")
            cur.fetchall()
            report("e bounded read-only query", True,
                   f"SELECT 1 in {(time.perf_counter() - t0) * 1000:.0f} ms; session READ ONLY")

            cur.execute(
                "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (" + ",".join(["%s"] * len(REQUIRED_OBJECTS)) + ")",
                REQUIRED_OBJECTS,
            )
            found = {r[0].lower(): (r[1], r[2]) for r in cur.fetchall()}
            missing = []
            for name in REQUIRED_OBJECTS:
                info = found.get(name.lower())
                if info:
                    rows = "" if info[0] == "VIEW" else f", ~{info[1]} rows (estimate)"
                    print(f"       object {name}: {info[0]}{rows}")
                else:
                    missing.append(name)
                    print(f"       object {name}: MISSING")

            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vw_verify_member_bene'"
            )
            cols = {r[0].lower() for r in cur.fetchall()}
            missing_cols = [c for c in VERIFY_VIEW_COLUMNS if c not in cols]
            print(f"       vw_verify_member_bene columns expected by code missing: {missing_cols or 'none'}")

            if "vw_verify_member_bene" in found:
                t0 = time.perf_counter()
                cur.execute("SELECT 1 FROM vw_verify_member_bene LIMIT 1")
                cur.fetchall()
                print(f"       bounded probe on vw_verify_member_bene: {(time.perf_counter() - t0) * 1000:.0f} ms (no values read)")

            cur.execute("SHOW GRANTS FOR CURRENT_USER()")
            grants = [redact_grant(r[0]) for r in cur.fetchall()]
            print("       grants:")
            for g in grants:
                print(f"         {g}")
            writes = [g for g in grants if re.search(r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", g)]
            report("f objects and grants", not missing,
                   ("missing: " + ", ".join(missing) + "; " if missing else "")
                   + ("account has WRITE/DDL privileges (not least-privilege)" if writes else "SELECT-only account"))
            cur.close()
            return 0 if not missing else 6
        except mysql.connector.Error as exc:
            report("e/f read-only checks", False, f"MySQL error {exc.errno}")
            return 5
        finally:
            conn.close()
    finally:
        if tunnel is not None:
            tunnel.stop()
            print("       tunnel closed")


if __name__ == "__main__":
    sys.exit(main())
