"""Duplicate-selection review: which batch keeps a claim identity selected in several batches.

docs/bcmd-duplicate-selection-review.md. A decision never edits stored DQA results: the next
DQA run of each affected batch reads it. No purge, delete, payment or approval writes.
Views return no beneficiary names, HHIDs, member IDs or birth dates; a selection is a batch
and a line number in that batch's frozen member list.
"""
from collections import defaultdict
from uuid import uuid4

from mysql.connector import Error

import bcmd_store as store
from bcmd_access import affiliation
from bcmd_actor import actor_column, actor_values
from bcmd_dqa_rules import claim_key
from errors import ApiError

ACTIVE_RUNS = ('QUEUED', 'RUNNING', 'RETRYING')
INVALIDATED = 'CLAIM_DECISION_CHANGED'
MAX_SELECTIONS = 50
HISTORY_LIMIT = 50
LOCK_ATTEMPTS = 3

# Payments guard (routers/payments.py): members an effective decision EXCLUDED, over alias m.
NOT_EXCLUDED_BY_REVIEW = ("NOT EXISTS (SELECT 1 FROM bcmd_claim_resolution_member x "
                          "JOIN bcmd_claim_conflict xc ON xc.current_resolution_id=x.resolution_id "
                          "WHERE x.batch_id=m.batch_id AND x.member_id=m.id AND x.decision='EXCLUDED')")


def _marks(values):
    return ','.join(['%s'] * len(values))


def decisions(cur, refs):
    """Effective decisions for these member refs: claim key -> conflict, retained row, covered rows."""
    if not refs:
        return {}
    cur.execute(f'''SELECT c.public_id,c.member_ref,c.academic_year,c.semester,r.retained_member_id,rm.member_id
        FROM bcmd_claim_conflict c JOIN bcmd_claim_resolution r ON r.id=c.current_resolution_id
        JOIN bcmd_claim_resolution_member rm ON rm.resolution_id=r.id
        WHERE c.member_ref IN ({_marks(refs)})''', tuple(refs))
    result = {}
    for row in cur.fetchall():
        entry = result.setdefault(claim_key(row), {'conflict_id': row['public_id'],
                                                   'retained': row['retained_member_id'], 'covered': set()})
        entry['covered'].add(row['member_id'])
    return result


def ensure_conflicts(cur, keys):
    """Upsert one conflict per claim key (sorted, so concurrent workers lock in the same order)."""
    for key in keys:
        cur.execute('INSERT INTO bcmd_claim_conflict (public_id,member_ref,academic_year,semester) '
                    'VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id', (str(uuid4()), *key))
    if not keys:
        return {}
    refs = sorted({key[0] for key in keys})
    cur.execute(f'SELECT public_id,member_ref,academic_year,semester FROM bcmd_claim_conflict '
                f'WHERE member_ref IN ({_marks(refs)})', tuple(refs))
    wanted = set(keys)
    return {claim_key(row): row['public_id'] for row in cur.fetchall() if claim_key(row) in wanted}


def _selections(cur, conflict_ids):
    """Every non-purged batch row holding each conflict's claim, with export and DQA state."""
    cur.execute(f'''SELECT c.id AS conflict_id,m.id AS member_id,m.batch_id,m.line_no,
        b.public_id AS batch_public_id,b.name AS batch_name,b.category,b.status AS batch_status,
        b.export_seq,b.claim_epoch,
        EXISTS (SELECT 1 FROM bcmd_batch_export e WHERE e.batch_id=b.id AND e.status='COMPLETED') AS has_export,
        lr.status AS run_status,lr.claim_epoch AS run_epoch
        FROM bcmd_claim_conflict c
        JOIN bcmd_batch_member m ON m.member_ref=c.member_ref AND m.academic_year=c.academic_year AND m.semester=c.semester
        JOIN bcmd_batch b ON b.id=m.batch_id
        LEFT JOIN bcmd_dqa_run lr ON lr.id=(SELECT r2.id FROM bcmd_dqa_run r2 WHERE r2.batch_id=b.id ORDER BY r2.run_no DESC LIMIT 1)
        WHERE c.id IN ({_marks(conflict_ids)}) AND b.status<>'PURGED'
        ORDER BY c.id,b.id,m.line_no''', tuple(conflict_ids))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        row['exported'] = bool(row['export_seq'] or row['batch_status'] == 'EXPORTED' or row['has_export'])
        grouped[row['conflict_id']].append(row)
    return grouped


def _effective(cur, conflicts):
    """conflict id -> effective decision (its resolution row plus covered rows)."""
    ids = [c['current_resolution_id'] for c in conflicts if c['current_resolution_id']]
    if not ids:
        return {}
    cur.execute(f'SELECT * FROM bcmd_claim_resolution WHERE id IN ({_marks(ids)})', tuple(ids))
    rows = {row['id']: {**row, 'covered': {}} for row in cur.fetchall()}
    cur.execute(f'SELECT resolution_id,member_id,decision FROM bcmd_claim_resolution_member '
                f'WHERE resolution_id IN ({_marks(ids)})', tuple(ids))
    for row in cur.fetchall():
        rows[row['resolution_id']]['covered'][row['member_id']] = row['decision']
    return {row['conflict_id']: row for row in rows.values()}


def _paid_batches(cur, batch_ids):
    """Batch ids with a recorded payment_transaction row; None when that table cannot be read."""
    if not batch_ids:
        return set()
    try:
        cur.execute(f'SELECT DISTINCT batch_id FROM payment_transaction WHERE batch_id IN ({_marks(batch_ids)})',
                    tuple(sorted(batch_ids)))
        return {row['batch_id'] for row in cur.fetchall()}
    except Error as exc:
        if exc.errno == 1146:  # no payment destination table: no payment can be recorded
            return set()
        if exc.errno in (1142, 1143):  # not readable: unknown, so decisions fail closed
            return None
        raise


def _same_actor(row, actor):
    column = actor_column(actor, 'actor')
    return row[column] == actor.id


def _assess(conflict, selections, decision, actor, paid):
    """Status, the selections still open to a decision and why each action is unavailable."""
    live = [s for s in selections if not s['exported']]
    covered = decision['covered'] if decision else {}
    retained_live = bool(decision) and any(s['member_id'] == decision['retained_member_id'] for s in live)
    if not decision:
        status = 'UNRESOLVED'
    elif not retained_live:
        status = 'RETAINED_UNAVAILABLE'
    elif any(s['member_id'] not in covered for s in live):
        status = 'NEEDS_REVIEW'
    else:
        status = 'RESOLVED'
    if any(s['exported'] for s in selections):
        blocked = 'CLAIM_EXPORTED'
    elif paid is None:
        blocked = 'CLAIM_PAYMENT_CHECK_UNAVAILABLE'
    elif any(s['batch_id'] in paid for s in selections):
        blocked = 'CLAIM_PAYMENT_RECORDED'
    else:
        blocked = None
    rows_per_batch = defaultdict(int)
    for s in live:
        rows_per_batch[s['batch_id']] += 1
    if status == 'NEEDS_REVIEW':
        choices = [decision['retained_batch_id']]
    else:
        choices = [batch for batch, rows in rows_per_batch.items() if rows == 1]
    resolve = (blocked or ('CONFLICT_NOT_ACTIVE' if len(rows_per_batch) < 2 else None)
               or {'RESOLVED': 'CONFLICT_ALREADY_RESOLVED', 'RETAINED_UNAVAILABLE': 'REVERSAL_REQUIRED'}.get(status)
               or (None if choices else 'CONFLICT_AMBIGUOUS'))
    reverse = (blocked or (None if decision else 'CONFLICT_NOT_RESOLVED')
               or ('SECOND_REVIEWER_REQUIRED' if _same_actor(decision, actor) else None))
    return {'status': status, 'live': live, 'choices': choices if not resolve else [],
            'resolve': resolve, 'reverse': reverse}


def _view(conflict, selections, decision, actor, paid):
    assessment = _assess(conflict, selections, decision, actor, paid)
    covered = decision['covered'] if decision else {}
    choices = set(assessment['choices'])
    return {
        'conflict_id': conflict['public_id'], 'version': conflict['version'], 'status': assessment['status'],
        'academic_year': conflict['academic_year'] or None, 'semester': conflict['semester'] or None,
        'selection_count': len(selections),
        'selections': [{
            'batch_id': s['batch_public_id'], 'batch_name': s['batch_name'], 'category': s['category'],
            'batch_status': s['batch_status'], 'exported': s['exported'], 'line_no': s['line_no'],
            'decision': covered.get(s['member_id']) or ('NOT_COVERED' if decision else None),
            'can_be_retained': s['batch_id'] in choices,
            'dqa_run_status': s['run_status'] or 'NOT_RUN',
            'dqa_refresh_required': s['run_epoch'] is not None and s['run_epoch'] != s['claim_epoch'],
        } for s in selections[:MAX_SELECTIONS]],
        'selections_truncated': len(selections) > MAX_SELECTIONS,
        'retained_batch_id': next((s['batch_public_id'] for s in selections
                                   if decision and s['member_id'] == decision['retained_member_id']), None),
        'actions': {'resolve': {'allowed': assessment['resolve'] is None, 'reason': assessment['resolve']},
                    'reverse': {'allowed': assessment['reverse'] is None, 'reason': assessment['reverse']}},
        'updated_at': conflict['updated_at'].isoformat() if conflict['updated_at'] else None,
    }


def _views(cur, conflicts, actor):
    if not conflicts:
        return []
    selections = _selections(cur, [c['id'] for c in conflicts])
    effective = _effective(cur, conflicts)
    paid = _paid_batches(cur, {s['batch_id'] for rows in selections.values() for s in rows})
    return [_view(c, selections.get(c['id'], []), effective.get(c['id']), actor, paid) for c in conflicts]


def _conflict(cur, public_id, lock=False):
    cur.execute('SELECT * FROM bcmd_claim_conflict WHERE public_id=%s' + (' FOR UPDATE' if lock else ''),
                (str(public_id),))
    row = cur.fetchone()
    if not row:
        raise ApiError(404, 'NOT_FOUND', 'Duplicate selection not found')
    return row


def list_conflicts(db, actor, *, batch_id=None, limit=50, offset=0):
    cur = db.cursor(dictionary=True)
    try:
        if batch_id is not None:
            batch = store.batch_row(cur, batch_id)
            cur.execute('''SELECT DISTINCT c.* FROM bcmd_claim_conflict c JOIN bcmd_batch_member m
                ON m.member_ref=c.member_ref AND m.academic_year=c.academic_year AND m.semester=c.semester
                WHERE m.batch_id=%s ORDER BY c.id LIMIT %s OFFSET %s''', (batch['id'], limit + 1, offset))
        else:
            cur.execute('SELECT * FROM bcmd_claim_conflict ORDER BY id DESC LIMIT %s OFFSET %s', (limit + 1, offset))
        rows = cur.fetchall()
        return {'items': _views(cur, rows[:limit], actor), 'has_more': len(rows) > limit}
    finally:
        db.rollback()
        cur.close()


EVENT_SQL = '''SELECT r.*,b.public_id AS retained_batch_public_id,b.name AS retained_batch_name,
    v.public_id AS reverses_public_id
    FROM bcmd_claim_resolution r LEFT JOIN bcmd_batch b ON b.id=r.retained_batch_id
    LEFT JOIN bcmd_claim_resolution v ON v.id=r.reverses_resolution_id '''


def _events(cur, rows, actor):
    ids = [row['id'] for row in rows]
    excluded = defaultdict(list)
    if ids:
        cur.execute(f'''SELECT rm.resolution_id,b.public_id,b.name FROM bcmd_claim_resolution_member rm
            JOIN bcmd_batch b ON b.id=rm.batch_id WHERE rm.resolution_id IN ({_marks(ids)})
            AND rm.decision='EXCLUDED' ORDER BY b.id''', tuple(ids))
        for row in cur.fetchall():
            excluded[row['resolution_id']].append({'batch_id': row['public_id'], 'batch_name': row['name']})
    return [_event(row, actor, excluded[row['id']]) for row in rows]


def _history(cur, conflict, actor, limit=HISTORY_LIMIT):
    cur.execute(EVENT_SQL + 'WHERE r.conflict_id=%s ORDER BY r.version DESC LIMIT %s', (conflict['id'], limit + 1))
    rows = cur.fetchall()
    return {'items': _events(cur, rows[:limit], actor), 'has_more': len(rows) > limit}


def _event(row, actor, excluded=()):
    return {'decision_id': row['public_id'], 'version': row['version'], 'action': row['action'],
            'retained_batch_id': row['retained_batch_public_id'], 'retained_batch_name': row['retained_batch_name'],
            'excluded_batches': list(excluded), 'reverses_decision_id': row['reverses_public_id'],
            'reason': row['reason'], 'reviewer': row['actor_label'], 'reviewer_agency': row['actor_agency'],
            'by_you': _same_actor(row, actor), 'recorded_at': row['created_at'].isoformat()}


def read_conflict(db, public_id, actor):
    cur = db.cursor(dictionary=True)
    try:
        conflict = _conflict(cur, public_id)
        return {**_views(cur, [conflict], actor)[0], 'history': _history(cur, conflict, actor)}
    finally:
        db.rollback()
        cur.close()


def _replay(cur, actor, key, request_hash):
    cur.execute(f'SELECT * FROM bcmd_claim_resolution WHERE {actor_column(actor, "actor")}=%s AND idempotency_key=%s',
                (actor.id, str(key)))
    row = cur.fetchone()
    if row and row['request_hash'] != request_hash:
        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Key was used for a different request')
    return row


def _holders(cur, conflict):
    cur.execute('''SELECT DISTINCT m.batch_id FROM bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id
        WHERE m.member_ref=%s AND m.academic_year=%s AND m.semester=%s AND b.status<>'PURGED' ''',
                (conflict['member_ref'], conflict['academic_year'], conflict['semester']))
    return sorted(row['batch_id'] for row in cur.fetchall())


def _refresh(cur, batch_ids, actor, request_id, event, reason_codes):
    """Mark affected batches as needing fresh DQA and invalidate their in-flight runs (batches are locked)."""
    agency = affiliation(actor)
    for batch_id in sorted(batch_ids):
        cur.execute('UPDATE bcmd_batch SET claim_epoch=claim_epoch+1 WHERE id=%s', (batch_id,))
        cur.execute(f'SELECT id FROM bcmd_dqa_run WHERE batch_id=%s AND status IN ({_marks(ACTIVE_RUNS)}) FOR UPDATE',
                    (batch_id, *ACTIVE_RUNS))
        for run in cur.fetchall():
            cur.execute("UPDATE bcmd_dqa_run SET status='FAILED',last_error_code=%s,lease_token=NULL,lease_until=NULL "
                        'WHERE id=%s', (INVALIDATED, run['id']))
            store._insert(cur, 'bcmd_audit_event', dict(batch_id=batch_id, event_type='DQA_RUN_INVALIDATED',
                **actor_values(actor, 'actor'), actor_agency=agency, request_id=request_id, reason_code=INVALIDATED))
        store._insert(cur, 'bcmd_audit_event', dict(batch_id=batch_id, event_type=event,
            **actor_values(actor, 'actor'), actor_agency=agency, request_id=request_id, reason_code=reason_codes[batch_id]))


def _result(cur, resolution_id, actor):
    cur.execute(EVENT_SQL + 'WHERE r.id=%s', (resolution_id,))
    row = cur.fetchone()
    cur.execute('SELECT * FROM bcmd_claim_conflict WHERE id=%s', (row['conflict_id'],))
    conflict = cur.fetchone()
    return {'decision': _events(cur, [row], actor)[0], 'conflict': _views(cur, [conflict], actor)[0]}


def _mutate(db, public_id, action, request_hash, *, key, actor, request_id, reason, expected_version,
            retained_batch_id=None):
    actor_column(actor, 'actor')  # supported identity before any query
    cur = db.cursor(dictionary=True)
    try:
        previous = _replay(cur, actor, key, request_hash)
        if previous:
            return _result(cur, previous['id'], actor)
        for _ in range(LOCK_ATTEMPTS):
            # Every re-read below runs after the locks and must see the latest committed rows.
            db.rollback()
            db.start_transaction(isolation_level='READ COMMITTED')
            conflict = _conflict(cur, public_id)
            batch_ids = _holders(cur, conflict)
            # Same order as DQA start, the worker and export: batch rows first, ascending.
            if batch_ids:
                cur.execute(f'SELECT id FROM bcmd_batch WHERE id IN ({_marks(batch_ids)}) ORDER BY id FOR UPDATE',
                            tuple(batch_ids))
                cur.fetchall()
            conflict = _conflict(cur, public_id, lock=True)
            if _holders(cur, conflict) == batch_ids:
                break
        else:
            raise ApiError(409, 'CLAIM_REVIEW_BUSY', 'The selections changed during review; refresh and try again')
        # A concurrent request with the same key may have committed while this one waited for the locks.
        previous = _replay(cur, actor, key, request_hash)
        if previous:
            db.rollback()
            return _result(cur, previous['id'], actor)
        if conflict['version'] != expected_version:
            raise ApiError(409, 'CONFLICT_VERSION_STALE', 'This duplicate selection changed; refresh and review again')
        selections = _selections(cur, [conflict['id']]).get(conflict['id'], [])
        if len(selections) > MAX_SELECTIONS:
            raise ApiError(409, 'CONFLICT_AMBIGUOUS', 'Too many selections to decide in one review')
        decision = _effective(cur, [conflict]).get(conflict['id'])
        assessment = _assess(conflict, selections, decision, actor, _paid_batches(cur, set(batch_ids)))
        blocked = assessment[action.lower()]
        if blocked == 'CLAIM_PAYMENT_CHECK_UNAVAILABLE':
            raise ApiError(503, blocked, 'Recorded payments cannot be checked; no decision was recorded')
        if blocked == 'SECOND_REVIEWER_REQUIRED':
            raise ApiError(403, blocked, 'Another reviewer must reverse this decision')
        if blocked:
            raise ApiError(409, blocked, 'This action is not available for this duplicate selection')
        version = conflict['version'] + 1
        values = dict(public_id=str(uuid4()), conflict_id=conflict['id'], version=version, action=action,
                      reason=reason, **actor_values(actor, 'actor'), actor_label=(actor.username or 'DSWD staff')[:64],
                      actor_agency=affiliation(actor), request_id=request_id, idempotency_key=str(key),
                      request_hash=request_hash)
        live = assessment['live']
        if action == 'RESOLVE':
            chosen = [s for s in live if s['batch_public_id'] == str(retained_batch_id)]
            if not chosen:
                raise ApiError(422, 'RETAINED_BATCH_NOT_IN_CONFLICT', 'Choose one of the competing unexported batches')
            if chosen[0]['batch_id'] not in assessment['choices']:
                raise ApiError(409, 'REVERSAL_REQUIRED' if assessment['status'] == 'NEEDS_REVIEW' else 'CONFLICT_AMBIGUOUS',
                               'This batch cannot be chosen to keep the claim')
            retained = chosen[0]
            values.update(retained_batch_id=retained['batch_id'], retained_member_id=retained['member_id'])
            resolution_id = store._insert(cur, 'bcmd_claim_resolution', values)
            store._insert_many(cur, 'bcmd_claim_resolution_member', ('resolution_id', 'batch_id', 'member_id', 'decision'),
                [(resolution_id, s['batch_id'], s['member_id'], 'RETAINED' if s is retained else 'EXCLUDED') for s in live])
            affected = {s['batch_id'] for s in live}
            codes = {batch: 'RETAINED' if batch == retained['batch_id'] else 'EXCLUDED' for batch in affected}
            event = 'CLAIM_RESOLVED'
        else:
            values.update(reverses_resolution_id=decision['id'])
            resolution_id = store._insert(cur, 'bcmd_claim_resolution', values)
            covered_batches = {s['batch_id'] for s in selections if s['member_id'] in decision['covered']}
            affected = {s['batch_id'] for s in live} | covered_batches
            codes = {batch: 'REVERSED' for batch in affected}
            event = 'CLAIM_REVERSED'
        cur.execute('UPDATE bcmd_claim_conflict SET version=%s,current_resolution_id=%s,updated_at=UTC_TIMESTAMP(6) '
                    'WHERE id=%s AND version=%s',
                    (version, resolution_id if action == 'RESOLVE' else None, conflict['id'], conflict['version']))
        if cur.rowcount != 1:
            raise ApiError(409, 'CONFLICT_VERSION_STALE', 'This duplicate selection changed; refresh and review again')
        _refresh(cur, affected, actor, request_id, event, codes)
        db.commit()
        return _result(cur, resolution_id, actor)
    except Error as exc:
        db.rollback()
        if exc.errno == 1062:
            # A concurrent request with the same key or for the same version committed first.
            previous = _replay(cur, actor, key, request_hash)
            if previous:
                return _result(cur, previous['id'], actor)
            raise ApiError(409, 'CONFLICT_VERSION_STALE', 'This duplicate selection changed; refresh and review again') from None
        if exc.errno in (1205, 1213):
            raise ApiError(409, 'CLAIM_REVIEW_BUSY', 'Another review or DQA run holds these batches; try again') from None
        raise
    finally:
        db.rollback()
        cur.close()


def resolve(db, public_id, *, retained_batch_id, reason, expected_version, key, actor, request_id):
    request_hash = store.digest({'action': 'RESOLVE', 'conflict_id': str(public_id),
                                 'retained_batch_id': str(retained_batch_id), 'reason': reason,
                                 'expected_version': expected_version})
    return _mutate(db, public_id, 'RESOLVE', request_hash, key=key, actor=actor, request_id=request_id,
                   reason=reason, expected_version=expected_version, retained_batch_id=retained_batch_id)


def reverse(db, public_id, *, reason, expected_version, key, actor, request_id):
    request_hash = store.digest({'action': 'REVERSE', 'conflict_id': str(public_id), 'reason': reason,
                                 'expected_version': expected_version})
    return _mutate(db, public_id, 'REVERSE', request_hash, key=key, actor=actor, request_id=request_id,
                   reason=reason, expected_version=expected_version)
