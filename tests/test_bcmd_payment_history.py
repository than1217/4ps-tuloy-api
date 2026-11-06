"""Payment-history SQL must use the existing payment table's primary key."""
import re

import bcmd_payment_history as history


def test_recorded_uses_payment_primary_key_and_preserves_reference(fake_db):
    fake_db.on(r'FROM payment_transaction p', lambda sql, params: [{
        'id': 42, 'member_id': 7, 'status': 1, 'category': 'TA',
        'member_rows': 1, 'academic_year': '2026-2027', 'periods': 1, 'semester': 2,
    }])
    cur = fake_db.cursor(dictionary=True)
    found = history.recorded(cur, ['7'])

    assert found['7'] == [{
        'ref': 'payment_transaction:42', 'id': 42, 'state': 'PENDING',
        'category': 'TA', 'period': ('2026-2027', 2), 'mapped': True,
    }]
    sql, params = fake_db.executed[-1]
    # Check the emitted database contract, including GROUP BY and ORDER BY references.
    payment_columns = set(re.findall(r'\bp\.(\w+)', sql))
    assert payment_columns == {'payment_transaction_id', 'member_id', 'status', 'batch_id'}
    assert re.search(r'SELECT\s+p\.payment_transaction_id\s+AS\s+id\b', sql, re.I)
    assert params == (7,)
