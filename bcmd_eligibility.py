"""Downstream eligibility: who may be handed to NAPA, put in a new NAPA file, or paid, right now.

Eligible = PASS in the batch's latest DQA run (completed, current ruleset, current claim epoch,
with no newer queued, running or failed run) and no recorded payment that blocks it now
(bcmd_payment_history), and not excluded by an effective duplicate-selection decision.
The snapshot status saved with the batch (bcmd_batch_member.dqa_status) never authorizes anything.
"""
import bcmd_payment_history as history
import bcmd_store as store
from bcmd_claims import NOT_EXCLUDED_BY_REVIEW
from bcmd_export import latest_run, run_reasons

MEMBER_COLUMNS = ('m.id,m.line_no,m.member_ref,m.hhid,m.sex,m.academic_year,m.semester,'
                  'm.first_name,m.middle_name,m.last_name,m.source_agency,m.course_type')


def batch_reasons(batch, run):
    """Why no member of this batch can be eligible now; empty when the latest run can be used."""
    if batch['status'] == 'PURGED':
        return ['BATCH_PURGED']
    reasons = []
    if not store.policy_supported(batch) or batch['member_count'] < 1:
        reasons.append('VERSION_UNSUPPORTED')
    return reasons + run_reasons(run, batch)


def evaluate(cur, public_id, *, lock=False, check_history=True, columns=MEMBER_COLUMNS):
    """The batch, its latest run, batch-level reasons and its members with their run outcome.

    lock=True reads the batch FOR SHARE, so DQA, reviews and exports cannot change it until the
    caller's transaction ends. Members excluded by review are left out. Each member gets
    outcome (None without a usable run) and, for PASS members, payment findings.
    Returns None when the batch does not exist. Raises history.Unavailable.
    """
    cur.execute('SELECT * FROM bcmd_batch WHERE public_id=%s' + (' FOR SHARE' if lock else ''), (str(public_id),))
    batch = cur.fetchone()
    if not batch:
        return None
    run = None if batch['status'] == 'PURGED' else latest_run(cur, batch)
    reasons = batch_reasons(batch, run)
    usable = run['id'] if run and not reasons else None
    if usable:
        cur.execute('SELECT COUNT(*) AS n FROM bcmd_dqa_result WHERE run_id=%s', (usable,))
        if cur.fetchone()['n'] != run['total'] or run['total'] != batch['member_count']:
            reasons, usable = ['DQA_RESULTS_INCONSISTENT'], None
    cur.execute(f'SELECT {columns},r.outcome FROM bcmd_batch_member m '
                'LEFT JOIN bcmd_dqa_result r ON r.member_id=m.id AND r.run_id=%s '
                'WHERE m.batch_id=%s AND ' + NOT_EXCLUDED_BY_REVIEW + ' ORDER BY m.line_no',
                (usable or 0, batch['id']))
    members = cur.fetchall()
    for member in members:
        member['payment_findings'] = []
    result = {'batch': batch, 'run': run, 'reasons': reasons, 'members': members}
    if check_history:
        apply_history(cur, result)
    return result


def apply_history(cur, result):
    """Attach current payment findings to every PASS member. Raises history.Unavailable."""
    passed = [m for m in result['members'] if m['outcome'] == 'PASS']
    found = history.check(cur, result['batch']['category'], passed)
    for member in passed:
        member['payment_findings'] = found.get(member['member_ref'], [])
    return result


def eligible(result, member):
    return not result['reasons'] and member['outcome'] == 'PASS' and not member['payment_findings']


def finding_counts(members):
    counts = {}
    for member in members:
        for finding in member['payment_findings']:
            counts[finding['code']] = counts.get(finding['code'], 0) + 1
    return dict(sorted(counts.items()))
