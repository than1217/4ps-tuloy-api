"""Recorded payments and the program's one-payment rules, in one place.

Candidates, batch save, DQA, NAPA handoff and export creation, every payroll path and payment
generation all ask this module whether a recorded payment blocks a member (docs/bcmd-napa-workflow.md,
"Payment history"). A payment can be recorded after DQA, so each later step asks again.

Rules (Q-P5: nothing beyond these):
  TK   a TK payment blocks TK, in any period. It blocks nothing else.
  TT   a TT payment blocks TT and TA. It does not block TK.
  TA   a TA payment blocks TA for the same academic year and semester only.
  A recorded payment whose category (or, for TA, period) cannot be mapped blocks every category
  (PAYMENT_UNMAPPED, Q-P4). A void payment blocks nothing.

Status (Q-P2): no status value is officially confirmed as paid/disbursed or as void yet, so both
sets are empty and every recorded payment is PENDING. A pending payment still blocks (Q-P6), but
it is never called paid: its finding is PAYMENT_PENDING.

Source (Phase P0): payment_transaction is the only payment table this repository references.
Its batch_id is bcmd_batch.id (routers/payments.py, generate). Category comes from that batch and
the period from the batch member with the same 4Ps member ID. academic_year_term, assistance_type,
status and tuloy_payroll_type have no lookup in this repository, so they are never used to map.
"""
import hashlib
from collections import defaultdict

from mysql.connector import Error

from errors import ApiError

SOURCE = 'payment_transaction'
PAID_STATUSES = frozenset()   # Q-P2: officially confirmed paid/disbursed values. None confirmed yet.
VOID_STATUSES = frozenset()   # Q-P2: officially confirmed cancelled/reversed values. None confirmed yet.
assert not PAID_STATUSES & VOID_STATUSES
CATEGORIES = ('TK', 'TA', 'TT')
# Findings when the blocking payment's status is confirmed paid. A pending one gives PAYMENT_PENDING.
PAID_CODE = {('TK', 'TK'): 'ALREADY_PAID_TK', ('TT', 'TT'): 'ALREADY_PAID_TT',
             ('TA', 'TT'): 'TA_BLOCKED_BY_TT_PAYMENT', ('TA', 'TA'): 'ALREADY_PAID_SAME_PERIOD'}
FAIL_CODES = frozenset(PAID_CODE.values())
REVIEW_CODES = frozenset({'PAYMENT_PENDING', 'PAYMENT_UNMAPPED'})
CODES = FAIL_CODES | REVIEW_CODES
CHUNK = 500
# MySQL: table access denied, column access denied, table missing, unknown column.
UNREADABLE = (1142, 1143, 1146, 1054)


class Unavailable(Exception):
    """Payment history could not be read. Never treated as an empty history."""


def unavailable():
    return ApiError(503, 'PAYMENT_HISTORY_UNAVAILABLE', 'Payment history could not be read; nothing was paid or saved')


def config():
    """Part of the DQA ruleset: changing a rule or a status set needs a fresh DQA run."""
    return {'source': SOURCE, 'paid_statuses': sorted(PAID_STATUSES), 'void_statuses': sorted(VOID_STATUSES),
            'blocks': {'TK': ['TK'], 'TT': ['TT', 'TA'], 'TA': ['TA same academic_year and semester']},
            'unmapped': 'blocks every category', 'pending': 'blocks, never labelled paid'}


def payment_id(member_ref):
    """The integer member_id a payment row carries. Snapshots store str(int) (bcmd_store.snapshot)."""
    text = str(member_ref or '')
    return int(text) if text.isdigit() and str(int(text)) == text else None


def state(status):
    value = '' if status is None else str(status).strip()
    if value in VOID_STATUSES:
        return 'VOID'
    return 'PAID' if value in PAID_STATUSES else 'PENDING'


def recorded(cur, member_refs):
    """{member_ref: [payment]} for every recorded payment of these members.

    A payment: ref ('payment_transaction:<id>'), state (PAID, PENDING or VOID), category and period
    when they map, and mapped. Raises Unavailable when the history cannot be read.
    """
    ids = {}
    for ref in member_refs:
        number = payment_id(ref)
        if number is not None:
            ids[number] = str(ref)
    found = defaultdict(list)
    numbers = sorted(ids)
    try:
        for start in range(0, len(numbers), CHUNK):
            chunk = numbers[start:start + CHUNK]
            cur.execute(f"""SELECT p.payment_transaction_id AS id,p.member_id,p.status,b.category,
                    COUNT(m.id) AS member_rows,MIN(m.academic_year) AS academic_year,
                    COUNT(DISTINCT COALESCE(m.academic_year,''),COALESCE(m.semester,0)) AS periods,
                    MIN(m.semester) AS semester
                FROM {SOURCE} p
                LEFT JOIN bcmd_batch b ON b.id=p.batch_id
                LEFT JOIN bcmd_batch_member m ON m.batch_id=p.batch_id AND m.member_ref=CAST(p.member_id AS CHAR)
                WHERE p.member_id IN ({','.join(['%s'] * len(chunk))})
                GROUP BY p.payment_transaction_id,p.member_id,p.status,b.category ORDER BY p.payment_transaction_id""", tuple(chunk))
            for row in cur.fetchall():
                category = row['category'] if row['category'] in CATEGORIES else None
                period = None
                if row['member_rows'] and row['periods'] == 1:
                    period = (row['academic_year'] or '', int(row['semester'] or 0))
                mapped = category is not None and (category != 'TA' or period is not None)
                found[ids[int(row['member_id'])]].append(dict(
                    ref=f"{SOURCE}:{row['id']}", id=row['id'], state=state(row['status']),
                    category=category, period=period, mapped=mapped))
    except Error as exc:
        if exc.errno in UNREADABLE:
            raise Unavailable() from None
        raise
    return found


def findings(category, academic_year, semester, payments):
    """[{code, payment_ref}] blocking a payment of this category and period, one per code."""
    target = (academic_year or '', int(semester or 0))
    result = {}
    for payment in payments:
        if payment['state'] == 'VOID':
            continue
        if not payment['mapped']:
            code = 'PAYMENT_UNMAPPED'
        else:
            code = PAID_CODE.get((category, payment['category']))
            if code == 'ALREADY_PAID_SAME_PERIOD' and payment['period'] != target:
                code = None
            if code and payment['state'] != 'PAID':
                code = 'PAYMENT_PENDING'
        if code and code not in result:
            result[code] = payment['ref']
    return [{'code': code, 'payment_ref': ref} for code, ref in sorted(result.items())]


def check(cur, category, members):
    """{member_ref: findings} for members ({member_ref, academic_year, semester}) of one category."""
    history = recorded(cur, {m['member_ref'] for m in members if m.get('member_ref')})
    return {m['member_ref']: findings(category, m.get('academic_year'), m.get('semester'), history.get(m['member_ref'], ()))
            for m in members if m.get('member_ref')}


def blocking_sql(category, member, academic_year, semester):
    """SQL predicate, true when a recorded payment blocks this source row; the same rules as findings().

    member, academic_year and semester are trusted SQL expressions from fixed call sites.
    """
    if category not in CATEGORIES:
        raise ValueError('Unknown category')
    params = []
    live = ''
    if VOID_STATUSES:
        live = f" AND COALESCE(TRIM(CAST(p.status AS CHAR)),'') NOT IN ({','.join(['%s'] * len(VOID_STATUSES))})"
        params += sorted(VOID_STATUSES)
    one_period = ("(SELECT COUNT(DISTINCT COALESCE(pm.academic_year,''),COALESCE(pm.semester,0)) FROM bcmd_batch_member pm "
                  "WHERE pm.batch_id=p.batch_id AND pm.member_ref=CAST(p.member_id AS CHAR))=1")
    unmapped = f"(pb.category IS NULL OR pb.category NOT IN ('TK','TA','TT') OR (pb.category='TA' AND NOT {one_period}))"
    if category == 'TA':
        same_period = ("EXISTS (SELECT 1 FROM bcmd_batch_member pm WHERE pm.batch_id=p.batch_id "
                       "AND pm.member_ref=CAST(p.member_id AS CHAR) AND COALESCE(pm.academic_year,'')=COALESCE("
                       f"{academic_year},'') AND COALESCE(pm.semester,0)=COALESCE({semester},0))")
        rule = f"pb.category='TT' OR (pb.category='TA' AND {one_period} AND {same_period})"
    else:
        rule = 'pb.category=%s'
        params.append(category)
    sql = (f'EXISTS (SELECT 1 FROM {SOURCE} p LEFT JOIN bcmd_batch pb ON pb.id=p.batch_id '
           f'WHERE p.member_id={member}{live} AND ({unmapped} OR {rule}))')
    return sql, params


def lock_members(cur, member_refs):
    """Serialize payment generation per beneficiary until the caller's transaction ends.

    One row per beneficiary in a BCMD-owned key table, locked exclusively in key order with
    INSERT ... ON DUPLICATE KEY UPDATE (no shared-to-exclusive upgrade). Keys are SHA-256 digests,
    never member IDs. Only writers that take these locks are serialized.
    """
    keys = sorted({hashlib.sha256(f'member:{ref}'.encode()).hexdigest() for ref in member_refs if ref})
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start:start + CHUNK]
        cur.execute('INSERT INTO bcmd_payment_member_lock (member_key) VALUES '
                    + ','.join(['(%s)'] * len(chunk)) + ' ON DUPLICATE KEY UPDATE member_key=member_key', tuple(chunk))
    return len(keys)
