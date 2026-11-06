"""Durable BCMD-to-NAPA handoff ("Proceed to NAPA").

A handoff records the exact DQA run whose PASS records NAPA may put in new files. It is allowed
only when that run is the batch's latest, completed, current and not invalidated, at least one
PASS record exists and no recorded payment blocks any PASS record now. A later run or a duplicate-
selection decision makes it stale; earlier handoffs are kept as history and never changed.
"""
from uuid import uuid4

from mysql.connector import Error

import bcmd_eligibility as eligibility
import bcmd_export as exports
import bcmd_payment_history as history
import bcmd_store as store
from bcmd_actor import actor_column
from bcmd_access import affiliation
from errors import ApiError

SELECT = ('SELECT h.*,r.public_id AS dqa_run_public_id,r.run_no AS dqa_run_no FROM bcmd_napa_handoff h '
          'JOIN bcmd_dqa_run r ON r.id=h.dqa_run_id ')


def refusal(reasons):
    if 'BATCH_PURGED' in reasons:
        return ApiError(410, 'BATCH_PURGED', 'Batch has been purged')
    if 'DQA_RESULTS_INCONSISTENT' in reasons:
        return ApiError(409, 'NAPA_BATCH_BLOCKED', 'Stored DQA results are inconsistent with the run')
    return exports.refusal(reasons)


def read(cur, batch):
    """The latest handoff with its current status, and every earlier one (newest first)."""
    run = exports.latest_run(cur, batch)
    cur.execute(SELECT + 'WHERE h.batch_id=%s ORDER BY h.id DESC', (batch['id'],))
    rows = cur.fetchall()
    state = exports.handoff_view(rows[0] if rows else None, run, batch)
    return {'batch_id': batch['public_id'], **state, 'history': [exports.handoff_public(row) for row in rows]}


def record(db, public_id, run_public_id, key, actor, request_id):
    """Record the handoff of run_public_id, or return the handoff already recorded for it."""
    actor_ref = f'{actor_column(actor)}:{actor.id}'
    request_hash = store.digest({'dqa_run_id': str(run_public_id)})
    cur = db.cursor(dictionary=True)
    try:
        # The batch row is held FOR SHARE: DQA runs, duplicate-selection decisions and exports
        # need it exclusively, so none can change the run or its population until this commits.
        result = eligibility.evaluate(cur, public_id, lock=True)
        if result is None:
            raise ApiError(404, 'NOT_FOUND', 'Batch not found')
        batch, run = result['batch'], result['run']
        if batch['status'] == 'PURGED':
            raise ApiError(410, 'BATCH_PURGED', 'Batch has been purged')
        cur.execute(SELECT + 'WHERE h.batch_id=%s AND h.actor_ref=%s AND h.idempotency_key=%s',
                    (batch['id'], actor_ref, str(key)))
        previous = cur.fetchone()
        if previous:
            if previous['request_hash'] != request_hash:
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'Key was used for a different request')
            return _response(cur, batch, run, previous)
        if not run or run['public_id'] != str(run_public_id):
            raise ApiError(409, 'HANDOFF_RUN_NOT_LATEST', 'This is not the batch\'s latest DQA run; reload the batch')
        if result['reasons']:
            raise refusal(result['reasons'])
        passed = [m for m in result['members'] if m['outcome'] == 'PASS']
        if not passed:
            raise ApiError(409, 'NAPA_NO_RECORDS', 'The latest DQA run has no passed records')
        blocked = sum(1 for m in passed if m['payment_findings'])
        if blocked:
            raise ApiError(409, 'PAYMENT_HISTORY_CHANGED', f'A recorded payment now blocks {blocked} passed record(s). '
                           'Nothing was handed off. Run DQA again.')
        cur.execute(SELECT + 'WHERE h.dqa_run_id=%s', (run['id'],))
        existing = cur.fetchone()
        if existing:  # already handed off by another request: the same run is never recorded twice
            return _response(cur, batch, run, existing)
        handoff_id = store._insert(cur, 'bcmd_napa_handoff', dict(
            public_id=str(uuid4()), batch_id=batch['id'], dqa_run_id=run['id'], claim_epoch=batch['claim_epoch'],
            passed_count=len(passed), actor_ref=actor_ref, actor_agency=affiliation(actor), request_id=request_id,
            idempotency_key=str(key), request_hash=request_hash))
        store._audit(cur, batch['id'], actor, request_id, 'NAPA_HANDOFF_RECORDED')
        cur.execute(SELECT + 'WHERE h.id=%s', (handoff_id,))
        response = _response(cur, batch, run, cur.fetchone())
        db.commit()
        return response
    except history.Unavailable:
        raise history.unavailable() from None
    except Error as exc:
        if exc.errno != 1062:
            raise
        # A concurrent request recorded this run first: return that handoff.
        db.rollback()
        cur.execute(SELECT + 'WHERE h.dqa_run_id=(SELECT id FROM bcmd_dqa_run WHERE public_id=%s)', (str(run_public_id),))
        existing = cur.fetchone()
        if not existing:
            raise
        cur.execute('SELECT * FROM bcmd_batch WHERE id=%s', (existing['batch_id'],))
        batch = cur.fetchone()
        return _response(cur, batch, exports.latest_run(cur, batch), existing)
    finally:
        db.rollback()
        cur.close()


def _response(cur, batch, run, handoff):
    return {'batch_id': batch['public_id'], **exports.handoff_view(handoff, run, batch)}
