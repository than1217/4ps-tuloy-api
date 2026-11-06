"""Grant, revoke and list scope grants in api_user_scopes. Never run by the API.

    python -m scripts.grants list   --user tuloy_admin
    python -m scripts.grants grant  --user tuloy_admin --scope tuloy:legacy:member:verify \\
                                    --partner DSWD --agency DSWD --days 7 --by Martin
    python -m scripts.grants grant  ... --no-expiry --by Martin     (explicit; never the default)
    python -m scripts.grants revoke --grant-id 12 --by Martin

Uses the admin account (MIGRATION_DB_USER), not the runtime one. Rows are never edited or
deleted: revoke sets revoked_at/revoked_by once, a regrant inserts a new row. A remote target
needs --allow-remote and typing the database name. Prints grant metadata only, no secrets.
Every scope binds an approved agency (CHED, TESDA or DSWD) with partner equal to agency; anything
else is rejected before the database is touched. The legacy scopes are narrower (scopes.SCOPE_AGENCIES,
data-owner rulings): R8/R9/R11 are DSWD only and R10 is CHED or DSWD.
"""
import argparse
import sys

import mysql.connector

from config import ConfigError, load_settings
from observability import audit, configure_logging
from scopes import APPROVED_AGENCIES, KNOWN_SCOPES, agencies_for
from scripts.migrate import connect, is_loopback

MAX_DAYS = 365


def _user_id(cur, username: str) -> int:
    cur.execute("SELECT id, disabled FROM api_users WHERE username = %s", (username,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"no api_users row for {username!r}")
    if row[1]:
        raise SystemExit(f"{username!r} is disabled; enable the account first")
    return row[0]


def cmd_list(cur, args) -> int:
    cur.execute("SELECT g.grant_id, g.scope, g.partner_id, g.agency, g.granted_by, g.granted_at, g.expires_at, "
                "g.revoked_at, g.revoked_by, (g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at > CURRENT_TIMESTAMP)) "
                "FROM api_user_scopes g JOIN api_users u ON u.id = g.user_id WHERE u.username = %s "
                "ORDER BY g.grant_id", (args.user,))
    rows = cur.fetchall()
    for r in rows:
        state = "ACTIVE" if r[9] else ("revoked" if r[7] else "expired")
        print(f"#{r[0]} {state:7} {r[1]} partner={r[2]} agency={r[3]} by={r[4]} at={r[5]} expires={r[6] if r[6] is not None else 'never'}"
              + (f" revoked={r[7]} by={r[8]}" if r[7] else ""))
    print(f"{len(rows)} grant row(s)")
    return 0


def cmd_grant(cur, args) -> int:
    if args.scope not in KNOWN_SCOPES:
        raise SystemExit(f"unknown scope {args.scope!r}; known: {', '.join(sorted(KNOWN_SCOPES))}")
    if not args.no_expiry and not 1 <= args.days <= MAX_DAYS:
        raise SystemExit(f"--days must be 1..{MAX_DAYS}; use --no-expiry for a grant that does not expire")
    args.agency = args.agency.strip().upper()
    if args.agency not in APPROVED_AGENCIES:
        raise SystemExit("agency must be CHED, TESDA or DSWD; verification coverage is all regions (national)")
    args.partner = args.partner.strip().upper()
    if args.partner != args.agency:
        raise SystemExit(f"partner must equal agency ({args.agency}); unknown bindings are rejected")
    if args.agency not in agencies_for(args.scope):
        raise SystemExit(f"{args.scope} may only be granted to {' or '.join(sorted(agencies_for(args.scope)))} "
                         "(data-owner rulings, ADR 2026-10-27)")
    uid = _user_id(cur, args.user)
    try:
        if args.no_expiry:
            cur.execute("INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, NULL)", (uid, args.scope, args.partner, args.agency, args.by))
        else:
            cur.execute("INSERT INTO api_user_scopes (user_id, scope, partner_id, agency, granted_by, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL %s DAY)",
                        (uid, args.scope, args.partner, args.agency, args.by, args.days))
    except mysql.connector.Error as exc:
        if exc.errno == 1062:
            print("an active grant for this (user, scope, partner, agency) already exists; revoke it first")
            return 1
        raise
    gid = cur.lastrowid
    audit("authz.grant", "created", actor=args.by, grant_id=gid, user_id=uid, scope=args.scope,
          partner_id=args.partner, agency=args.agency, days=None if args.no_expiry else args.days)
    print(f"granted #{gid}: {args.scope} to {args.user} "
          + ("with no expiry" if args.no_expiry else f"for {args.days} day(s)"))
    return 0


def cmd_revoke(cur, args) -> int:
    cur.execute("UPDATE api_user_scopes SET revoked_at = CURRENT_TIMESTAMP, revoked_by = %s "
                "WHERE grant_id = %s AND revoked_at IS NULL", (args.by, args.grant_id))
    if cur.rowcount != 1:
        print(f"grant #{args.grant_id} not found or already revoked; nothing changed")
        return 1
    audit("authz.grant", "revoked", actor=args.by, grant_id=args.grant_id)
    print(f"revoked #{args.grant_id}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--user", required=True)
    p_grant = sub.add_parser("grant")
    p_grant.add_argument("--user", required=True)
    p_grant.add_argument("--scope", required=True)
    p_grant.add_argument("--partner", required=True)
    p_grant.add_argument("--agency", required=True)
    expiry = p_grant.add_mutually_exclusive_group(required=True)
    expiry.add_argument("--days", type=int, help=f"grant lasts this many days (1..{MAX_DAYS})")
    expiry.add_argument("--no-expiry", action="store_true", help="grant does not expire (must be asked for)")
    p_grant.add_argument("--by", required=True, help="who authorised this grant")
    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--grant-id", type=int, required=True)
    p_revoke.add_argument("--by", required=True)
    for p in (p_list, p_grant, p_revoke):
        p.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    configure_logging(settings.log_level)
    remote = bool(settings.ssh_host) or not is_loopback(settings.ods.host)
    if remote and args.command != "list":
        if not args.allow_remote:
            print("Refusing to change grants on a remote database without --allow-remote.")
            return 3
        typed = input(f"Type the database name to change its grants ({len(settings.ods.database)} chars): ")
        if typed != settings.ods.database:
            print("Database name did not match. Nothing changed.")
            return 3

    tunnel_holder = []
    try:
        conn = connect(settings, tunnel_holder)
        cur = conn.cursor()
        return {"list": cmd_list, "grant": cmd_grant, "revoke": cmd_revoke}[args.command](cur, args)
    finally:
        if tunnel_holder:
            tunnel_holder[0].stop()


if __name__ == "__main__":
    sys.exit(main())
