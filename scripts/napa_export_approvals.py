"""Print review hashes, or record genuine approval attestations. Dry-run by default.

Uses the configured existing BCMD account; never creates users, grants or tables.
An application request cannot record approvals. Do not put beneficiary data here.
"""
import argparse
from datetime import date
import json
from pathlib import Path

from bcmd_export import artifacts, ROLES
from config import get_settings
from database import get_bcmd_connection
from bcmd_readiness import verify


def validate(rows):
    expected = artifacts()
    if not isinstance(rows, list) or not rows:
        raise ValueError('Expected a non-empty attestation array')
    for row in rows:
        gate = row['gate_code']
        if row['decision'] not in {'APPROVED', 'REVOKED'} or row['approver_role'] not in ROLES[gate]:
            raise ValueError('Invalid decision or owner role')
        for key, maximum in [('approver_label', 64), ('reference', 120)]:
            if not isinstance(row[key], str) or not 0 < len(row[key].strip()) <= maximum:
                raise ValueError('A real approver and evidence reference are required')
        date.fromisoformat(row['approved_on'])
        if gate in expected and row['artifact_sha256'] != expected[gate]:
            raise ValueError('Artifact mismatch; obtain approval for the current artifact')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--expect-db')
    parser.add_argument('--expect-host')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps({'artifact_sha256': artifacts()}, indent=2))
    if not args.manifest:
        if args.apply:
            parser.error('--apply requires --manifest')
        return
    rows = validate(json.loads(args.manifest.read_text(encoding='utf8')))
    if not args.apply:
        print('DRY_RUN: valid attestations; nothing written')
        return
    if not args.expect_db or not args.expect_host:
        parser.error('--apply requires --expect-db and --expect-host')
    connections = get_bcmd_connection(get_settings())
    try:
        db = next(connections)
        verify(db, get_settings())
        cur = db.cursor(dictionary=True)
        try:
            cur.execute('SELECT DATABASE() db,@@hostname host')
            if cur.fetchone() != {'db': args.expect_db, 'host': args.expect_host}:
                raise ValueError('Wrong target; nothing written')
            for row in rows:
                columns = ('gate_code', 'decision', 'approver_role', 'approver_label', 'reference', 'approved_on', 'artifact_sha256')
                cur.execute('INSERT INTO bcmd_approval (' + ','.join('`'+c+'`' for c in columns) + ') VALUES (' + ','.join(['%s']*len(columns)) + ')',
                            tuple(row.get(c) for c in columns))
            db.commit()
            print('RECORDED: ' + str(len(rows)) + ' attestations')
        finally:
            db.rollback()
            cur.close()
    finally:
        connections.close()


if __name__ == '__main__':
    main()
