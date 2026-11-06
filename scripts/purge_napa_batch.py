"""Explicit operator-only purge. No automatic retention schedule or API route.

Credentials come only from BCMD_PURGE_USER/PASSWORD in the process environment;
this script does not load application .env files. Dry-run is the default.
"""
import argparse
import os
from uuid import UUID, uuid4

import mysql.connector

from bcmd_store import PERSONAL_COLUMNS


def purge_batch(db, public_id, *, expected_database, expected_hostname, apply=False):
    """Fence readers, scrub all private snapshot columns and audit atomically."""
    public_id = str(UUID(str(public_id)))
    cur = db.cursor(dictionary=True)
    try:
        db.start_transaction(isolation_level='READ COMMITTED')
        cur.execute('SELECT DATABASE() AS db,@@hostname AS host,CURRENT_USER() AS account')
        identity = cur.fetchone()
        if identity['db'] != expected_database or identity['host'] != expected_hostname:
            raise ValueError('Target identity mismatch; nothing changed')
        cur.execute('SELECT id,status FROM bcmd_batch WHERE public_id=%s FOR UPDATE', (public_id,))
        batch = cur.fetchone()
        if not batch:
            raise ValueError('Batch not found; nothing changed')
        if batch['status'] == 'PURGED':
            return {'status': 'ALREADY_PURGED', 'public_id': public_id}
        cur.execute("SELECT id FROM bcmd_batch_export WHERE batch_id=%s AND status='STARTED' LIMIT 1 FOR UPDATE",
                    (batch['id'],))
        if cur.fetchone():
            raise ValueError('Export is in progress; nothing changed')
        cur.execute('SELECT COUNT(*) AS n FROM bcmd_batch_member WHERE batch_id=%s', (batch['id'],))
        count = cur.fetchone()['n']
        if not apply:
            return {'status': 'DRY_RUN', 'public_id': public_id, 'members': count,
                    'personal_columns': len(PERSONAL_COLUMNS)}
        cur.execute('UPDATE bcmd_batch_member SET ' + ','.join(f'{c}=NULL' for c in PERSONAL_COLUMNS)
                    + ' WHERE batch_id=%s', (batch['id'],))
        cur.execute("UPDATE bcmd_batch SET status='PURGED',purged_at=UTC_TIMESTAMP(3),purged_by=%s WHERE id=%s",
                    (identity['account'], batch['id']))
        cur.execute("""INSERT INTO bcmd_audit_event
            (batch_id,event_type,actor_label,request_id,reason_code)
            VALUES (%s,'BATCH_PURGED','DB_OPERATOR',%s,'MANUAL_PURGE')""",
                    (batch['id'], str(uuid4())))
        db.commit()
        return {'status': 'PURGED', 'public_id': public_id, 'members': count}
    finally:
        db.rollback()
        cur.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', required=True, type=UUID)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=3306)
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-hostname', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-batch', type=UUID)
    args = parser.parse_args()
    if args.apply and args.confirm_batch != args.batch:
        parser.error('--apply requires --confirm-batch with the same UUID')
    user = os.environ.get('BCMD_PURGE_USER')
    password = os.environ.get('BCMD_PURGE_PASSWORD')
    if not user or not password:
        parser.error('Set BCMD_PURGE_USER and BCMD_PURGE_PASSWORD for a separate operator account')
    db = None
    try:
        db = mysql.connector.connect(host=args.host, port=args.port, database=args.expect_db,
            user=user, password=password, connection_timeout=10)
        print(purge_batch(db, args.batch, expected_database=args.expect_db,
                         expected_hostname=args.expect_hostname, apply=args.apply))
    except (mysql.connector.Error, ValueError):
        # Connector errors can include SQL/account details. Keep the console sanitized.
        parser.exit(1, 'STOP: connection, target, batch or purge check failed; inspect with the DBA.\n')
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    main()
