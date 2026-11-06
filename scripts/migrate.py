"""Explicit migrations for application-owned tables. Never run by the API.

    python -m scripts.migrate status
    python -m scripts.migrate apply [--to 0001] [--allow-remote]
    python -m scripts.migrate apply --backfill 0003 [--allow-remote]

Connects with MIGRATION_DB_USER / MIGRATION_DB_PASSWORD (a DDL account separate from the
runtime DB_USER) to DB_HOST/DB_DATABASE, through the SSH tunnel when SSH_HOST is set.
Applying to anything other than a loopback host requires --allow-remote and an interactive
confirmation that names the database, shown after the target server identity is printed.
Runs hold a named lock (GET_LOCK) and use a short lock_wait_timeout. Applied versions and checksums go to schema_migrations.
A missing version (no file for it below the newest one) or a pending file older than an applied
version stops both status and apply with exit 6, before any SQL change.
--backfill VERSION applies exactly one such pending file, named explicitly, and records it with its real
checksum. It never edits or deletes ledger rows, and every other sequence problem still stops the run.
"""
import argparse
import hashlib
import ipaddress
import pathlib
import re
import sys

import mysql.connector

from config import ConfigError, load_settings
from tunnel import Tunnel

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
FILE_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
LOCK_WAIT_SECONDS = 10


def discover():
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = FILE_PATTERN.match(path.name)
        if m:
            sql = path.read_text(encoding="utf-8")
            files.append((m.group(1), path.name, hashlib.sha256(sql.encode()).hexdigest(), sql))
    return files


def statements(sql: str):
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def connect(settings, tunnel_holder):
    user = settings.migration_user
    password = settings.migration_password
    if not user or not password:
        raise SystemExit("MIGRATION_DB_USER and MIGRATION_DB_PASSWORD are required (separate DDL account).")
    host, port = settings.ods.host, settings.ods.port
    if settings.ssh_host:
        tunnel_holder.append(Tunnel(settings, host, port))
        tunnel_holder[0].start()
        host, port = "127.0.0.1", tunnel_holder[0].ensure()
    return mysql.connector.connect(host=host, port=port, database=settings.ods.database, user=user,
                                   password=password, connection_timeout=settings.db_connect_timeout,
                                   autocommit=True)


def ensure_ledger(cur):
    cur.execute(
        "CREATE TABLE IF NOT EXISTS `schema_migrations` ("
        " `version` char(4) NOT NULL PRIMARY KEY, `name` varchar(255) NOT NULL,"
        " `checksum` char(64) NOT NULL, `applied_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def applied(cur) -> dict:
    cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'schema_migrations'")
    if not cur.fetchall():
        return {}
    cur.execute("SELECT version, checksum FROM schema_migrations")
    return {v: c for v, c in cur.fetchall()}


def sequence_problems(on_disk, done, backfill=None) -> list:
    """Gaps that would make versions apply out of order. Checked before any lock or SQL change.

    A version below the newest file must exist on disk (e.g. 0005 without 0004 means 0004 has not
    been merged here), and a pending file must not be older than an applied version, except the one
    version named by --backfill.
    """
    problems = []
    if on_disk:
        newest = int(max(on_disk))
        missing = [f"{n:04d}" for n in range(1, newest) if f"{n:04d}" not in on_disk]
        if missing:
            problems.append(f"missing migration file(s): {', '.join(missing)} (needed before {max(on_disk)})")
    if done:
        late = sorted(v for v in on_disk if v not in done and v < max(done) and v != backfill)
        if late:
            problems.append(f"pending {', '.join(late)} older than applied {max(done)}: the ledger has a gap")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "apply"))
    parser.add_argument("--to", help="apply up to and including this version")
    parser.add_argument("--backfill", help="apply only this pending version, older than an applied one")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip the prompt (loopback targets only)")
    args = parser.parse_args(argv)
    if args.backfill and (args.command != "apply" or args.to):
        print("--backfill is an apply option and cannot be combined with --to.")
        return 2

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    remote = bool(settings.ssh_host) or not is_loopback(settings.ods.host)
    if args.command == "apply" and remote and not args.allow_remote:
        print("Refusing to migrate a remote database without --allow-remote.")
        return 3

    tunnel_holder = []
    try:
        conn = connect(settings, tunnel_holder)
        cur = conn.cursor()
        # DDL waits at most this long for a metadata lock instead of stalling live traffic.
        cur.execute("SET SESSION lock_wait_timeout = %s", (LOCK_WAIT_SECONDS,))
        cur.execute("SELECT VERSION(), DATABASE(), CURRENT_USER()")
        version_str, database_name, current_user = cur.fetchone()
        print(f"target: MySQL {version_str}, database {database_name}, as {current_user}, "
              f"{'remote' if remote else 'loopback'}{' via SSH tunnel' if settings.ssh_host else ''}")

        done = applied(cur)
        pending = []
        mismatch = False
        for version, name, checksum, sql in discover():
            if version in done:
                state = "applied" if done[version] == checksum else "CHECKSUM MISMATCH"
                mismatch = mismatch or state != "applied"
            else:
                state = "pending"
                pending.append((version, name, checksum, sql))
            print(f"{version} {name}: {state}")
        known = {v for v, *_ in discover()}
        for version in sorted(set(done) - known):
            print(f"{version} (no file): applied in the database but missing here")
        if mismatch:
            print("An applied migration file was edited. Fix that before applying anything.")
            return 4
        if args.backfill:
            if args.backfill not in {v for v, *_ in pending}:
                print(f"--backfill {args.backfill}: no pending migration file with that version. Nothing applied.")
                return 6
            if not done or args.backfill > max(done):
                print(f"--backfill {args.backfill} is not older than an applied version; use plain apply. Nothing applied.")
                return 6
            pending = [p for p in pending if p[0] == args.backfill]
        problems = sequence_problems(known, done, args.backfill)
        if problems:
            for problem in problems:
                print(problem)
            print("Migrations must apply in order with no missing version. Nothing applied.")
            return 6
        if args.command == "status":
            return 0
        if not pending:
            print("Nothing to apply.")
            return 0

        if remote:
            typed = input(f"Type the database name to migrate it ({len(settings.ods.database)} chars): ")
            if typed != settings.ods.database:
                print("Database name did not match. Nothing applied.")
                return 3
        elif not args.yes:
            if input("Apply migrations to the local database? [y/N] ").strip().lower() != "y":
                return 3

        cur.execute("SELECT GET_LOCK('tuloy_schema_migrate', 0)")
        if cur.fetchone()[0] != 1:
            print("Another migration run holds the lock. Nothing applied.")
            return 5
        try:
            ensure_ledger(cur)
            # Re-read under the lock in case another run applied something meanwhile.
            done = applied(cur)
            for version, name, checksum, sql in pending:
                if args.to and version > args.to:
                    break
                if version in done:
                    continue
                for stmt in statements(sql):
                    cur.execute(stmt)
                cur.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                            (version, name, checksum))
                print(f"{version} {name}: applied now")
        finally:
            cur.execute("SELECT RELEASE_LOCK('tuloy_schema_migrate')")
            cur.fetchall()
        return 0
    finally:
        if tunnel_holder:
            tunnel_holder[0].stop()

if __name__ == "__main__":
    sys.exit(main())
