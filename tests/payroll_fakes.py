"""Scripted FakeDB answers for the shared payroll eligibility (bcmd_eligibility). Synthetic values only."""
import bcmd_store as store
from bcmd_dqa_rules import HASH, VERSION


def batch_row(category='TK', member_count=2):
    version = store.POLICY_VERSIONS[category]
    _, canonical, sha = store.POLICIES[version]
    return dict(id=1, public_id='SYNTHETIC-BATCH-001', name='Synthetic batch', category=category,
                source_agency='CHED', status='DQA_COMPLETED', member_count=member_count, policy_version=version,
                dqa_config_canonical=canonical, dqa_config_sha256=sha, dqa_ruleset_sha256=sha, claim_epoch=0)


def run_row(members):
    outcomes = [m['outcome'] for m in members]
    return dict(id=7, public_id='77777777-7777-4777-8777-777777777777', run_no=1, status='COMPLETED',
                ruleset_version=VERSION, ruleset_sha256=HASH, claim_epoch=0, total=len(members),
                processed=len(members), passed=outcomes.count('PASS'), failed=outcomes.count('FAIL'),
                review_count=outcomes.count('REVIEW'), excluded_count=outcomes.count('EXCLUDED'))


def member(number, outcome='PASS', hhid='SYN-HH-001', sex='MALE', **extra):
    return dict(id=100 + number, line_no=number, member_ref=str(900000 + number), hhid=hhid, sex=sex,
                academic_year='2026-2027', semester=1, first_name='Synthetic', middle_name='Example',
                last_name=f'Student {number}', outcome=outcome, **extra)


def script(fake_db, members, *, category='TK', payments=()):
    """Answer the eligibility queries for one synthetic batch with these members and payments."""
    batch = batch_row(category, len(members))
    run = run_row(members)
    fake_db.on(r'FROM bcmd_batch WHERE public_id', lambda sql, params: [dict(batch)] if members else [])
    fake_db.on(r'FROM bcmd_dqa_run WHERE batch_id', lambda sql, params: [dict(run)])
    fake_db.on(r'COUNT\(\*\) AS n FROM bcmd_dqa_result', lambda sql, params: [{'n': len(members)}])
    fake_db.on(r'FROM bcmd_batch_member m LEFT JOIN bcmd_dqa_result',
               lambda sql, params: [dict(m) for m in members])
    fake_db.on(r'FROM payment_transaction p', lambda sql, params: [dict(p) for p in payments])
    return batch, run
