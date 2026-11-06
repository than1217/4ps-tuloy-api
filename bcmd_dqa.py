"""Durable, leased DQA runs on immutable BCMD members. No source or payment writes."""
import json
import logging
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from bcmd_actor import actor_column
import bcmd_claims as claims
import bcmd_payment_history as payment_history
import bcmd_store as store
from bcmd_dqa_rules import CANONICAL, HASH, VERSION, claim_key, evaluate, public_checks
from errors import ApiError

MAX_ATTEMPTS = 3
logger = logging.getLogger('tuloy.dqa')


def log_worker_error(event, exc, run=None):
    """Log diagnostic metadata only; never driver messages, SQL or source values."""
    fields = {}
    if run:
        fields.update(run_id=str(run['public_id']), attempt=run['attempts'],
                      processed=run.get('processed', 0))
        fields['dqa_request_id'] = run.get('request_id', '-')
    errno = getattr(exc, 'errno', None)
    if isinstance(errno, int):
        fields['errno'] = errno
    # Readiness failures name the failed check (bcmd_readiness.unavailable); no account or grant text.
    fields.update(getattr(exc, 'readiness', None) or {})
    # JsonFormatter emits exception type and source frames, never message/locals.
    logger.error(event, extra=fields, exc_info=(type(exc), exc, exc.__traceback__))
CHUNK = 50
PUBLIC = ('public_id,run_no,status,ruleset_version,ruleset_sha256,evaluation_date,total,processed,'
          'passed,failed,review_count,excluded_count,attempts,last_error_code,created_at,completed_at')


def public(row):
    result = {key: row[key] for key in PUBLIC.split(',')}
    for key in ('created_at', 'completed_at', 'evaluation_date'):
        if result[key] is not None:
            result[key] = result[key].isoformat()
    result['percent'] = (100 * row['processed'] // row['total']) if row['total'] else (100 if row['status'] == 'COMPLETED' else 0)
    result['ruleset_current'] = row['ruleset_version'] == VERSION and row['ruleset_sha256'] == HASH
    # The ruleset stays a draft until the DQA_RULES export gate is approved; DQA never authorizes payment.
    result['approval'] = 'DRAFT'
    result['payment_eligible'] = False
    return result


def summary(row):
    """Compact latest-run view used by batch lists and export readiness."""
    if not row:
        return None
    full = public(row)
    return {key: full[key] for key in ('public_id', 'run_no', 'status', 'total', 'processed', 'passed', 'failed',
                                       'review_count', 'excluded_count', 'percent', 'completed_at', 'ruleset_version',
                                       'ruleset_current', 'last_error_code')}


def history(db, batch_id, limit=20, offset=0):
    cur = db.cursor(dictionary=True)
    try:
        batch = store.batch_row(cur, batch_id)
        cur.execute(f'SELECT {PUBLIC} FROM bcmd_dqa_run WHERE batch_id=%s ORDER BY run_no DESC LIMIT %s OFFSET %s',
                    (batch['id'], limit + 1, offset))
        rows = cur.fetchall()
        return {'items': [public(row) for row in rows[:limit]], 'has_more': len(rows) > limit}
    finally:
        db.rollback(); cur.close()


def start(db, batch_id, key, actor, request_id):
    """Batch row serializes run numbering and prevents overlapping new requests."""
    cur = db.cursor(dictionary=True)
    try:
        cur.execute('SELECT * FROM bcmd_batch WHERE public_id=%s FOR UPDATE', (str(batch_id),))
        batch = cur.fetchone()
        if not batch:
            raise ApiError(404, 'NOT_FOUND', 'Batch not found')
        if batch['status'] == 'PURGED':
            raise ApiError(410, 'BATCH_PURGED', 'Batch was purged')
        actor_ref = f'{actor_column(actor)}:{actor.id}'
        cur.execute('SELECT * FROM bcmd_dqa_run WHERE batch_id=%s AND actor_ref=%s AND idempotency_key=%s',
                    (batch['id'], actor_ref, str(key)))
        previous = cur.fetchone()
        if previous:
            db.rollback(); return public(previous)
        # Do not alter already-exported records or an in-progress export.
        if batch['status'] == 'EXPORTED' or batch['export_seq']:
            raise ApiError(409, 'DQA_BATCH_EXPORTED', 'Use a new batch after export')
        cur.execute('SELECT status,run_no FROM bcmd_dqa_run WHERE batch_id=%s ORDER BY run_no DESC LIMIT 1', (batch['id'],))
        latest = cur.fetchone()
        if latest and latest['status'] in ('QUEUED', 'RUNNING', 'RETRYING'):
            raise ApiError(409, 'DQA_RUN_ACTIVE', 'This batch already has an active run')
        cur.execute('SELECT COUNT(*) AS n FROM bcmd_batch_member WHERE batch_id=%s', (batch['id'],))
        total = cur.fetchone()['n']
        if total != batch['member_count'] or total < 1:
            raise ApiError(409, 'DQA_MEMBERSHIP_INVALID', 'Frozen membership is inconsistent or unsupported')
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        values = dict(public_id=str(uuid4()), batch_id=batch['id'], run_no=latest['run_no'] + 1 if latest else 1,
            actor_ref=actor_ref, request_id=request_id, idempotency_key=str(key),
            ruleset_version=VERSION, ruleset_sha256=HASH, ruleset_json=CANONICAL,
            evaluation_date=datetime.now(ZoneInfo('Asia/Manila')).date(), total=total,
            next_attempt_at=now, created_at=now, claim_epoch=batch['claim_epoch'])
        run_id = store._insert(cur, 'bcmd_dqa_run', values)
        store._audit(cur, batch['id'], actor, request_id, 'DQA_RUN_QUEUED')
        cur.execute('SELECT * FROM bcmd_dqa_run WHERE id=%s', (run_id,))
        result = public(cur.fetchone())
        db.commit(); return result
    except Exception:
        db.rollback(); raise
    finally:
        cur.close()


def read(db, batch_id, run_id, *, results=False, limit=50, offset=0, outcome=None):
    cur = db.cursor(dictionary=True)
    try:
        batch = store.batch_row(cur, batch_id)
        cur.execute('SELECT * FROM bcmd_dqa_run WHERE public_id=%s AND batch_id=%s', (str(run_id), batch['id']))
        run = cur.fetchone()
        if not run:
            raise ApiError(404, 'NOT_FOUND', 'DQA run not found')
        if not results:
            return {**public(run), **public_checks()}
        params = [run['id'], batch['id']]
        where = ''
        if outcome:
            where = ' AND r.outcome=%s'
            params.append(outcome)
        cur.execute('SELECT r.line_no,r.outcome,r.findings_json,m.member_ref,m.hhid,m.last_name,m.first_name '
                    'FROM bcmd_dqa_result r JOIN bcmd_batch_member m ON m.id=r.member_id AND m.batch_id=%s '
                    'WHERE r.run_id=%s' + where + ' ORDER BY r.line_no LIMIT %s OFFSET %s',
                    (batch['id'], run['id'], *params[2:], limit + 1, offset))
        rows = cur.fetchall()
        return {'items': [dict(line_no=row['line_no'], member_ref=row['member_ref'], hhid=row['hhid'],
                               last_name=row['last_name'], first_name=row['first_name'], outcome=row['outcome'],
                               findings=json.loads(row['findings_json'])) for row in rows[:limit]],
                'has_more': len(rows) > limit}
    finally:
        db.rollback(); cur.close()


def other_batches(cur, batch_id, members):
    """Same member and period in other live batches: with a NAPA DETAIL export, or not yet exported.

    A member counts as claimed only when that export included it (legacy exports included
    every member; newer exports only the members that passed the export's DQA run).
    Purged batches no longer hold member references and cannot be matched.
    Returns the claimed keys and, per key, the member rows selecting it in unexported batches.
    """
    refs = sorted({m['member_ref'] for m in members if m['member_ref']})
    if not refs:
        return set(), {}
    marks = ','.join(['%s'] * len(refs))
    cur.execute(f"""SELECT m2.id,m2.member_ref,m2.academic_year,m2.semester,
        EXISTS (SELECT 1 FROM bcmd_batch_export e LEFT JOIN bcmd_dqa_result r ON r.run_id=e.dqa_run_id AND r.member_id=m2.id
                WHERE e.batch_id=m2.batch_id AND e.status='COMPLETED' AND e.file_type='DETAIL'
                AND (e.dqa_run_id IS NULL OR r.outcome='PASS')) AS claimed,
        EXISTS (SELECT 1 FROM bcmd_batch_export e WHERE e.batch_id=m2.batch_id
                AND e.status='COMPLETED' AND e.file_type='DETAIL') AS exported
        FROM bcmd_batch_member m2 JOIN bcmd_batch b2 ON b2.id=m2.batch_id
        WHERE m2.batch_id<>%s AND b2.status<>'PURGED' AND m2.member_ref IN ({marks})""", (batch_id, *refs))
    claimed, selected = set(), defaultdict(set)
    for row in cur.fetchall():
        key = claim_key(row)
        if row['claimed']:
            claimed.add(key)
        elif not row['exported']:
            selected[key].add(row['id'])
    return claimed, selected


def selection_flags(member, selected, decided):
    """(other_selection, excluded) for one member from the effective duplicate-selection decisions.

    A decision EXCLUDED this row: excluded. It RETAINED this row and covers every other current
    selection: no finding. Otherwise any other current selection stays for verification.
    """
    key = claim_key(member)
    others = selected.get(key, set())
    decision = decided.get(key)
    excluded = bool(decision) and member['id'] in decision['covered'] and member['id'] != decision['retained']
    kept = bool(decision) and member['id'] == decision['retained'] and others <= decision['covered']
    return bool(others) and not excluded and not kept, excluded


def claim(db):
    cur = db.cursor(dictionary=True)
    try:
        # Recovery is persisted and bounded. Expired owners cannot commit using old tokens.
        cur.execute("UPDATE bcmd_dqa_run SET status=IF(attempts >= %s,'FAILED','RETRYING'), lease_token=NULL, lease_until=NULL, "
                    "last_error_code='WORKER_LEASE_EXPIRED', next_attempt_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 3 SECOND) "
                    "WHERE status='RUNNING' AND lease_until < UTC_TIMESTAMP(6)", (MAX_ATTEMPTS,))
        cur.execute("SELECT * FROM bcmd_dqa_run WHERE status IN ('QUEUED','RETRYING') AND next_attempt_at <= UTC_TIMESTAMP(6) "
                    'ORDER BY next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED')
        run = cur.fetchone()
        if run:
            token = str(uuid4())
            cur.execute("UPDATE bcmd_dqa_run SET status='RUNNING',attempts=attempts+1,lease_token=%s,"
                        'lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 60 SECOND) WHERE id=%s', (token, run['id']))
            run.update(lease_token=token, status='RUNNING', attempts=run['attempts'] + 1)
        db.commit(); return run
    except Exception:
        db.rollback(); raise
    finally:
        cur.close()


def process_chunk(db, run):
    """Bounded compute outside locks, then fenced results/cursor/counts in one commit."""
    if run['ruleset_sha256'] != HASH or run['ruleset_version'] != VERSION:
        raise ValueError('Unsupported ruleset version')
    if json.loads(run['ruleset_json']) != json.loads(CANONICAL):
        raise ValueError('Ruleset configuration changed')
    cur = db.cursor(dictionary=True)
    try:
        cur.execute('SELECT * FROM bcmd_batch WHERE id=%s', (run['batch_id'],))
        batch = cur.fetchone()
        if not batch or batch['status'] in ('PURGED', 'EXPORTED') or batch['export_seq']:
            raise ValueError('Batch is no longer available')
        cur.execute('SELECT * FROM bcmd_batch_member WHERE batch_id=%s AND id>%s ORDER BY id LIMIT %s',
                    (run['batch_id'], run['checkpoint_member_id'], CHUNK))
        members = cur.fetchall()
        cur.execute('SELECT member_ref,COUNT(*) AS n FROM bcmd_batch_member WHERE batch_id=%s GROUP BY member_ref HAVING COUNT(*)>1',
                    (run['batch_id'],))
        duplicates = {row['member_ref'] for row in cur.fetchall()}
        cur.execute('SELECT member_id,field_no,rule_code FROM bcmd_dqa_issue WHERE batch_id=%s', (run['batch_id'],))
        issues = defaultdict(list)
        for row in cur.fetchall():
            issues[row['member_id']].append((f"column_{row['field_no']}", row['rule_code']))
        claimed, selected = other_batches(cur, run['batch_id'], members)
        decided = claims.decisions(cur, sorted({m['member_ref'] for m in members if m['member_ref']}))
        # Unreadable payment history raises here: the chunk fails and the run retries, then fails.
        payments = payment_history.check(cur, batch['category'], members)
        db.rollback()
        evaluated = []
        for member in members:
            other_selection, excluded = selection_flags(member, selected, decided)
            evaluated.append((member, *evaluate(member, batch, run['evaluation_date'],
                duplicate_member=member['member_ref'] in duplicates, legacy_issues=issues[member['id']],
                prior_claim=claim_key(member) in claimed, other_selection=other_selection,
                excluded_selection=excluded, payment_findings=payments.get(member['member_ref'], ()))))
        # Batch lock matches export's order, keeping a queued DQA and export mutually exclusive.
        cur.execute('SELECT status,export_seq,claim_epoch FROM bcmd_batch WHERE id=%s FOR UPDATE', (run['batch_id'],))
        current_batch = cur.fetchone()
        if current_batch['status'] in ('PURGED', 'EXPORTED') or current_batch['export_seq']:
            raise ValueError('Batch changed during evaluation')
        cur.execute('SELECT *,lease_until > UTC_TIMESTAMP(6) AS valid_lease FROM bcmd_dqa_run WHERE id=%s FOR UPDATE', (run['id'],))
        owner = cur.fetchone()
        if owner['status'] != 'RUNNING' or owner['lease_token'] != run['lease_token'] or not owner['valid_lease']:
            db.rollback(); return None
        if owner['checkpoint_member_id'] != run['checkpoint_member_id']:
            db.rollback(); return None
        # A duplicate-selection decision since this run started invalidates it (the decision also
        # marks the run FAILED in the same batch-locked transaction); never commit stale findings.
        if current_batch['claim_epoch'] != owner['claim_epoch']:
            raise ValueError('Duplicate-selection decisions changed during evaluation')
        # Conflict rows lock after the batch and run rows, in claim-key order, like every other writer.
        conflict_ids = claims.ensure_conflicts(cur, sorted({claim_key(member) for member, _, findings in evaluated
            if any(f['rule_code'] == 'SELECTED_IN_OTHER_BATCH' for f in findings)}))
        for member, _, findings in evaluated:
            for finding in findings:
                if finding['rule_code'] == 'SELECTED_IN_OTHER_BATCH':
                    finding['conflict_id'] = conflict_ids[claim_key(member)]
                elif finding['rule_code'] == 'SELECTION_EXCLUDED':
                    finding['conflict_id'] = decided[claim_key(member)]['conflict_id']
        for member, outcome, findings in evaluated:
            store._insert(cur, 'bcmd_dqa_result', dict(run_id=run['id'], member_id=member['id'],
                line_no=member['line_no'], outcome=outcome, findings_json=json.dumps(findings)))
        counts = Counter(outcome for _, outcome, _ in evaluated)
        processed = owner['processed'] + len(evaluated)
        if processed > owner['total'] or (not evaluated and processed < owner['total']):
            raise ValueError('Membership count mismatch')
        done = processed == owner['total']
        if done:
            cur.execute('SELECT COUNT(*) AS n FROM bcmd_dqa_result WHERE run_id=%s', (run['id'],))
            if cur.fetchone()['n'] != owner['total']:
                raise ValueError('Result count mismatch')
        cur.execute('UPDATE bcmd_dqa_run SET processed=%s,passed=passed+%s,failed=failed+%s,review_count=review_count+%s,'
                    'excluded_count=excluded_count+%s,checkpoint_member_id=%s,status=%s,completed_at=%s,last_error_code=NULL,'
                    'lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 60 SECOND) WHERE id=%s',
                    (processed, counts['PASS'], counts['FAIL'], counts['REVIEW'], counts['EXCLUDED'],
                     members[-1]['id'] if members else owner['checkpoint_member_id'],
                     'COMPLETED' if done else 'RUNNING', datetime.now(timezone.utc).replace(tzinfo=None) if done else None, run['id']))
        cur.execute('SELECT * FROM bcmd_dqa_run WHERE id=%s', (run['id'],))
        updated = cur.fetchone()
        if done:
            store._insert(cur, 'bcmd_audit_event', dict(batch_id=run['batch_id'], event_type='DQA_RUN_COMPLETED',
                actor_label='DQA worker', request_id=run['request_id']))
        db.commit(); return updated
    except Exception:
        db.rollback(); raise
    finally:
        cur.close()


def failed(db, run):
    cur = db.cursor()
    try:
        cur.execute("UPDATE bcmd_dqa_run SET status=IF(attempts >= %s,'FAILED','RETRYING'),"
                    "last_error_code='DQA_WORKER_FAILED',lease_token=NULL,lease_until=NULL,"
                    'next_attempt_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND) '
                    "WHERE id=%s AND status='RUNNING' AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6)",
                    (MAX_ATTEMPTS, min(60, 2 ** run['attempts']) + random.randint(0, 3), run['id'], run['lease_token']))
        if cur.rowcount and run['attempts'] >= MAX_ATTEMPTS:
            store._insert(cur, 'bcmd_audit_event', dict(batch_id=run['batch_id'], event_type='DQA_RUN_FAILED',
                actor_label='DQA worker', request_id=run['request_id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        cur.close()


def work_once(db):
    run = claim(db)
    if not run:
        return False
    try:
        while run and run['status'] == 'RUNNING':
            run = process_chunk(db, run)
    except Exception as exc:
        log_worker_error('dqa_processing_failed', exc, run)
        if run:
            failed(db, run)
    return True
