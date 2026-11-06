"""Create the national verification accounts using admin credentials. Never run by the API.

Preview: python -m scripts.provision_verification
Apply:   python -m scripts.provision_verification --apply --days 7 --by Martin
         python -m scripts.provision_verification --apply --no-expiry --by Martin

Each account gets tuloy:member:verify, and the DSWD account also tuloy:legacy:member:verify (DSWD
only, data-owner rulings ADR 2026-10-27), each bound to its agency code as partner_id and agency. Coverage is national: the grant model has no regional restriction,
and no wildcard partner or agency is ever created. --days and --no-expiry are mutually exclusive;
without --no-expiry the grants expire (default 7 days). A username that already exists is skipped,
never overwritten or granted anything. Each new account gets its own random password, written only
to the git-ignored .run/ directory (owner-only file); passwords are never printed or logged.
After the commit, each account and grant gets an audit event (actor, outcome, identifiers, expiry);
a rolled-back run gets one failed event. No password, hash or token is ever audited.
Remote writes also require --allow-remote and interactive database-name confirmation.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from config import load_settings
from observability import audit, configure_logging
from scopes import APPROVED_AGENCIES, LEGACY_MEMBER_VERIFY, SCOPE_VERIFY, agencies_for
from scripts.migrate import connect, is_loopback
from security import get_password_hash

ACCOUNTS = (("tuloy-tesda01", "TESDA"), ("tuloy-ched01", "CHED"), ("tuloy-dswd01", "DSWD"))
GRANT_SCOPES = (LEGACY_MEMBER_VERIFY, SCOPE_VERIFY)
MAX_DAYS = 365


def provision(cur, by: str, days: int | None, granted: list | None = None):
    """Insert the missing accounts and the grants their agency may hold. days=None means non-expiring grants.

    Returns (outcomes, credentials): outcomes is [(username, agency, "created" | "skipped")];
    credentials holds a record, including the new password, for created accounts only.
    If given, `granted` collects (username, user_id, grant_id, scope, agency) for each new grant.
    """
    if not {agency for _, agency in ACCOUNTS} <= APPROVED_AGENCIES:
        raise SystemExit("ACCOUNTS binds an agency outside APPROVED_AGENCIES; nothing created")
    outcomes, credentials = [], []
    for username, agency in ACCOUNTS:
        cur.execute("SELECT id FROM api_users WHERE username = %s", (username,))
        if cur.fetchone():
            outcomes.append((username, agency, "skipped"))
            continue
        password = secrets.token_urlsafe(24)
        cur.execute("INSERT INTO api_users (username, hashed_password, disabled) VALUES (%s, %s, 0)",
                    (username, get_password_hash(password)))
        uid = cur.lastrowid
        for scope in GRANT_SCOPES:
            if agency not in agencies_for(scope):
                continue
            if days is None:
                cur.execute("INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                            "VALUES (%s, %s, %s, %s, %s, NULL)", (uid, scope, agency, agency, by))
            else:
                cur.execute("INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                            "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL %s DAY)",
                            (uid, scope, agency, agency, by, days))
            if granted is not None:
                granted.append((username, uid, cur.lastrowid, scope, agency))
        credentials.append({"username": username, "password": password, "agency": agency, "coverage": "national",
                            "grant_days": days})
        outcomes.append((username, agency, "created"))
    return outcomes, credentials


def _restrict_to_current_user(path: Path) -> None:
    """Windows ignores the os.open mode, so drop inherited ACEs and grant only the current user. Fails closed.

    D (delete) lets the operator remove the file once the passwords are handed over.
    """
    try:
        user = os.environ["USERNAME"]
        if os.environ.get("USERDOMAIN"):
            user = os.environ["USERDOMAIN"] + "\\" + user
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W,D)"],
                       check=True, capture_output=True, timeout=30)
    except (KeyError, OSError, subprocess.SubprocessError):
        path.unlink(missing_ok=True)
        raise SystemExit("Could not restrict the credential file to the current user; file deleted, no changes made.")


def _write_credentials(credentials: list) -> Path:
    output = Path(__file__).resolve().parent.parent / ".run"
    output.mkdir(exist_ok=True)
    path = output / f"verification-credentials-{secrets.token_hex(6)}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    if sys.platform == "win32":
        os.close(fd)
        _restrict_to_current_user(path)  # before any secret is written
        fd = os.open(path, os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(credentials, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _audit_outcomes(by: str, days: int | None, outcomes: list, granted: list) -> None:
    """Audit what was committed: identifiers only, never a password, hash or token.

    Usernames are redacted by the log policy, so accounts are identified by user_id and agency
    (one account per agency).
    """
    ids = {username: uid for username, uid, *_ in granted}
    for username, agency, state in outcomes:
        audit("authn.account", state, actor=by, user_id=ids.get(username), agency=agency)
    for _, uid, grant_id, scope, agency in granted:
        audit("authz.grant", "created", actor=by, grant_id=grant_id, user_id=uid, scope=scope,
              partner_id=agency, agency=agency, days=days)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    expiry = parser.add_mutually_exclusive_group()
    expiry.add_argument("--days", type=int, help=f"grants last this many days (1..{MAX_DAYS}); default 7")
    expiry.add_argument("--no-expiry", action="store_true", help="grants do not expire (must be asked for)")
    parser.add_argument("--by", default="Martin")
    args = parser.parse_args(argv)
    days = None if args.no_expiry else (7 if args.days is None else args.days)
    if (days is not None and not 1 <= days <= MAX_DAYS) or not args.by.strip():
        parser.error(f"--days must be 1..{MAX_DAYS} and --by must identify the operator")
    settings = load_settings()
    configure_logging(settings.log_level)
    print(f"Target: {settings.ods.host}:{settings.ods.port}/{settings.ods.database}")
    for username, agency in ACCOUNTS:
        routes = "both verification routes" if agency in agencies_for(LEGACY_MEMBER_VERIFY) else "partner verification only"
        print(f"{username}: {agency}; all regions; {routes}; "
              + ("no expiry" if days is None else f"{days} days"))
    if not args.apply:
        print("Preview only. No accounts or grants created.")
        return 0
    if not settings.migration_user or not settings.migration_password:
        raise SystemExit("MIGRATION_DB_USER and MIGRATION_DB_PASSWORD are required; no changes made.")
    if settings.ssh_host or not is_loopback(settings.ods.host):
        if not args.allow_remote:
            raise SystemExit("Remote target requires --allow-remote; no changes made.")
        if input("Type the target database name to create these accounts: ") != settings.ods.database:
            raise SystemExit("Database name did not match; no changes made.")
    holders = []
    granted = []
    conn = None
    credential_path = None
    committed = False
    try:
        conn = connect(settings, holders)
        conn.autocommit = False
        cur = conn.cursor()
        try:
            outcomes, credentials = provision(cur, args.by, days, granted)
            if credentials:
                credential_path = _write_credentials(credentials)
            conn.commit()
            committed = True
        finally:
            cur.close()
        _audit_outcomes(args.by, days, outcomes, granted)
        for username, agency, state in outcomes:
            print(f"{username} ({agency}): {state}")
        if credential_path:
            print(f"Created {len(credentials)} account(s) with {len(granted)} grants. "
                  f"Private credentials: {credential_path}")
        else:
            print("Nothing created; every account already exists.")
    finally:
        if conn is not None:
            if not committed:
                conn.rollback()
            conn.close()
        if not committed:
            audit("authn.account", "failed", actor=args.by, reason="rolled_back")
        if not committed and credential_path is not None:
            credential_path.unlink(missing_ok=True)
        for tunnel in holders:
            tunnel.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
