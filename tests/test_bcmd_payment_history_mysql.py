"""Payment history and shared payroll eligibility on the isolated synthetic MySQL only.

payment_transaction here is the synthetic table this module's pytest database creates
(tests/test_bcmd_mysql.py). Every person, batch and payment is synthetic.
"""
import itertools
import json
import logging
import threading
import time
from uuid import uuid4

import mysql.connector
import pytest

import bcmd_eligibility as eligibility
import bcmd_payment_history as history
import database
from test_bcmd_mysql import mysql_candidate, real_stack, BASE, create, isolate
from test_bcmd_export_mysql import approve, handoff, new_export, csv_rows
import bcmd_export

PAY = '/api/payments'
_terms = itertools.count(1)
NAMES = ('Synthetic', 'Example', 'Student')


@pytest.fixture
def stack(real_stack, mysql_candidate):
    """Payments routes on their own connections to this module's synthetic database."""
    client, db, app = real_stack

    def connection():
        conn = mysql.connector.connect(**mysql_candidate, autocommit=False)
        try:
            yield conn
        finally:
            conn.rollback()
            conn.close()

    app.dependency_overrides[database.get_ods_connection] = connection
    app.dependency_overrides[database.get_app_connection] = connection
    yield client, db, app
    cur = db.cursor()
    cur.execute('DELETE FROM payment_transaction')
    db.commit()
    cur.close()


def add_people(db, numbers, *, status='ENROLLED', semester=1):
    """Synthetic eligible people: one household each, CHED matched, age 25."""
    cur = db.cursor()
    for n in numbers:
        cur.execute("INSERT INTO tbl_person VALUES (%s,'Synthetic','Example','Student','2001-02-03',1,2,0)", (n,))
        cur.execute('INSERT INTO tbl_hh_roster VALUES (%s,%s,%s)', (n, n, n + 100000))
        cur.execute('INSERT INTO tbl_household VALUES (%s,1)', (n + 100000,))
        cur.execute("INSERT INTO enroll_matched_CHED_tbl VALUES (%s,%s,%s,1,'2026-2027',%s,'1',%s,'Synthetic course')",
                    (n, f'SYN-{n}', n, semester, status))
        cur.execute('INSERT INTO tbl_family_selected VALUES (%s,%s)', (n + 100000, f'SYN-FAMILY-{n}'))
        cur.execute('INSERT INTO tbl_family_assoc_ops VALUES (%s,%s,%s,%s,0)', (n, f'SYN-FAMILY-{n}', n, n))
    db.commit()
    cur.close()


def complete_dqa(client, db, batch, *, isolated=True):
    """Run DQA on this batch alone: other batches are retired, this one is made live again first."""
    import test_bcmd_mysql
    revive(db, batch)
    return test_bcmd_mysql.complete_dqa(client, db, batch, isolated=isolated)


def revive(db, batch):
    cur = db.cursor()
    cur.execute("UPDATE bcmd_batch SET status='DQA_COMPLETED',purged_at=NULL,purged_by=NULL "
                "WHERE public_id=%s AND status='PURGED'", (batch,))
    db.commit()
    cur.close()


def batch_of(client, ids, category='TK'):
    made = create(client, ids=ids, category=category)
    assert made.status_code == 201, made.text
    return made.json()['public_id']


def internal_id(db, public_id):
    cur = db.cursor()
    cur.execute('SELECT id FROM bcmd_batch WHERE public_id=%s', (public_id,))
    value = cur.fetchone()[0]
    db.rollback()
    cur.close()
    return value


def record_payment(db, batch, member_ref, status=1):
    """A synthetic recorded payment, as payment generation writes it."""
    cur = db.cursor()
    cur.execute('INSERT INTO payment_transaction (batch_id,member_id,academic_year_term,assistance_type,amount,status) '
                'VALUES (%s,%s,%s,1,5000,%s)', (internal_id(db, batch) if batch else None, int(member_ref),
                                                  9000 + next(_terms), status))
    db.commit()
    payment = cur.lastrowid
    cur.close()
    return f'payment_transaction:{payment}'


def set_period(db, batch, academic_year=None, semester=None):
    cur = db.cursor()
    if academic_year:
        cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.academic_year=%s '
                    'WHERE b.public_id=%s', (academic_year, batch))
    if semester:
        cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.semester=%s '
                    'WHERE b.public_id=%s', (semester, batch))
    db.commit()
    cur.close()


def compute(client, batch):
    response = client.post(PAY + '/compute', json={'batch_id': batch})
    assert response.status_code == 200, response.text
    return response.json()


def generate(client, batch):
    return client.post(PAY + '/generate', json={'batch_id': batch, 'academic_year_term': next(_terms), 'assistance_type': 1})


def payment_count(db, member_ref=None):
    cur = db.cursor()
    if member_ref is None:
        cur.execute('SELECT COUNT(*) FROM payment_transaction')
    else:
        cur.execute('SELECT COUNT(*) FROM payment_transaction WHERE member_id=%s', (int(member_ref),))
    value = cur.fetchone()[0]
    db.rollback()
    cur.close()
    return value


def test_201_members_1_pass_200_review_pay_and_export_only_the_pass(stack):
    client, db, _ = stack
    add_people(db, range(1000, 1201))
    review = batch_of(client, list(range(1001, 1201)))
    batch = batch_of(client, list(range(1000, 1201)))
    # Every record passed the checks at save: the snapshot status alone would make all 201 payable.
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id "
                "WHERE b.public_id=%s AND m.dqa_status='PASS'", (batch,))
    assert cur.fetchone()[0] == 201
    db.rollback(); cur.close()
    isolate(db, batch)
    revive(db, review)
    run = complete_dqa(client, db, batch, isolated=False)
    assert (run['total'], run['passed'], run['review_count'], run['failed']) == (201, 1, 200, 0)

    totals = compute(client, batch)
    assert (totals['payable_count'], totals['pending_count'], totals['capped_count']) == (1, 200, 0)
    assert totals['dqa_run_id'] == run['public_id'] and totals['blocking_reasons'] == []
    listing = client.get(PAY + '/beneficiaries', params={'batch_id': batch, 'page_size': 200}).json()
    assert listing['total'] == 201
    states = [(item['fourps_member_id'], item['dqa_status'], item['payment']['state']) for item in listing['items']]
    assert states[0] == ('1000', 'PASS', 'payable')
    assert {s[1:] for s in states[1:]} == {('PENDING REVIEW', 'hold')}

    approve(db)
    # BCMD hands off the run with its 1 PASS record; NAPA lists 201 members, 1 passed.
    proceed = handoff(client, batch)
    assert proceed.status_code == 201, proceed.text
    assert proceed.json()['status'] == 'VALID' and proceed.json()['current']['passed_count'] == 1
    files = client.get(f'{BASE}/batches/{batch}/export-readiness').json()['files']
    assert {t: (f['ready'], f['member_count'], f['data_rows']) for t, f in files.items()} == {
        'DETAIL': (True, 1, 1), 'SUMMARY': (True, 1, 2), 'FAILED': (True, 200, 200)}
    register = client.get(f'{BASE}/batches', params={'register': 'napa'}).json()['items']
    row = next(item for item in register if item['public_id'] == batch)
    assert (row['member_count'], row['latest_dqa']['passed'], row['latest_dqa']['review_count']) == (201, 1, 200)
    assert row['napa_handoff']['status'] == 'VALID'
    assert {t: (f['ready'], f['member_count'], f['data_rows']) for t, f in row['napa_files'].items()} == {
        'DETAIL': (True, 1, 1), 'SUMMARY': (True, 1, 2), 'FAILED': (True, 200, 200)}
    assert review not in [item['public_id'] for item in register]  # never handed off

    detail = new_export(client, batch, hand_off=False)
    assert detail.status_code == 201, detail.text
    assert [row[10] for row in csv_rows(client.get(detail.json()['download_url']))[1:]] == ['1000']
    summary = new_export(client, batch, file_type='SUMMARY', hand_off=False)
    rows = csv_rows(client.get(summary.json()['download_url']))
    assert len(rows) == 3 and rows[-1][0] == 'TOTAL'  # header, one barangay, TOTAL
    assert rows[-1][10] == rows[-1][-1] == '1'  # the totals represent the 1 passed member
    failed = new_export(client, batch, file_type='FAILED', hand_off=False)
    assert len(csv_rows(client.get(failed.json()['download_url']))) == 201  # header + 200 for verification

    made = generate(client, batch)
    assert made.status_code == 200, made.text
    assert made.json()['created'] == 1 and made.json()['pending_review_count'] == 200
    cur = db.cursor()
    cur.execute('SELECT member_id FROM payment_transaction')
    assert cur.fetchall() == [(1000,)]
    db.rollback(); cur.close()


def test_payroll_uses_only_the_latest_usable_run(stack):
    client, db, _ = stack
    add_people(db, [2001, 2002])
    batch = batch_of(client, [2001, 2002])
    isolate(db, batch)
    blocked = compute(client, batch)
    assert blocked['blocking_reasons'] == ['DQA_NOT_RUN'] and blocked['payable_count'] == 0
    refused = generate(client, batch)
    assert refused.status_code == 409 and refused.json()['error_code'] == 'NAPA_DQA_REQUIRED'
    run = complete_dqa(client, db, batch)
    assert compute(client, batch)['payable_count'] == 2

    cur = db.cursor()
    # A newer run that is queued, failed, outdated or out of date with review decisions blocks payroll;
    # the older completed run never stands in for it.
    queued = client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())})
    assert queued.status_code == 202
    assert compute(client, batch)['blocking_reasons'] == ['DQA_IN_PROGRESS']
    cur.execute("UPDATE bcmd_dqa_run SET status='FAILED',attempts=3 WHERE public_id=%s", (queued.json()['public_id'],))
    db.commit()
    assert compute(client, batch)['blocking_reasons'] == ['DQA_FAILED']
    assert generate(client, batch).json()['error_code'] == 'NAPA_DQA_REQUIRED'
    cur.execute('DELETE FROM bcmd_dqa_run WHERE public_id=%s', (queued.json()['public_id'],))
    cur.execute("UPDATE bcmd_dqa_run SET ruleset_sha256=REPEAT('0',64) WHERE public_id=%s", (run['public_id'],))
    db.commit()
    assert compute(client, batch)['blocking_reasons'] == ['DQA_RULESET_OUTDATED']
    cur.execute("UPDATE bcmd_dqa_run r JOIN bcmd_batch b ON b.id=r.batch_id SET r.ruleset_sha256=%s,b.claim_epoch=b.claim_epoch+1 "
                'WHERE r.public_id=%s', (history_hash(), run['public_id']))
    db.commit()
    assert compute(client, batch)['blocking_reasons'] == ['DQA_REFRESH_REQUIRED']
    listing = client.get(PAY + '/beneficiaries', params={'batch_id': batch}).json()
    assert {item['payment']['state'] for item in listing['items']} == {'locked'}
    assert listing['eligibility']['blocking_reasons'] == ['DQA_REFRESH_REQUIRED']
    cur.close()
    assert payment_count(db) == 0


def history_hash():
    from bcmd_dqa_rules import HASH
    return HASH


def payable(client, batch):
    totals = compute(client, batch)
    return totals['payable_count'], totals['payment_findings']


def payable_now(client, db, batch):
    """Payroll totals now; complete_dqa retired the other batches, so this one is made live again first."""
    revive(db, batch)
    return payable(client, batch)


def set_status(db, number, status):
    cur = db.cursor()
    cur.execute('UPDATE enroll_matched_CHED_tbl SET enrollment_status=%s WHERE id=%s', (status, number))
    db.commit()
    cur.close()


def refused_save(client, ids, category='TK'):
    made = create(client, ids=ids, category=category)
    return made.status_code == 422 and made.json()['error_code'] == 'SOURCE_NOT_ELIGIBLE'


def test_program_rules_block_only_their_own_scope(stack):
    client, db, _ = stack
    add_people(db, [3001, 3002, 3003])
    add_people(db, [3011, 3012], status='GRADUATED')
    # Every batch is saved and checked before any payment is recorded: payroll rechecks payment history.
    tk, tk_again, ta = batch_of(client, [3001]), batch_of(client, [3001]), batch_of(client, [3001], category='TA')
    tt, tt_again = batch_of(client, [3011], category='TT'), batch_of(client, [3011], category='TT')
    ta_paid, same, other_semester = (batch_of(client, [3002], category='TA') for _ in range(3))
    tk_after_ta = batch_of(client, [3002])
    complete_dqa(client, db, tt_again)
    set_status(db, 3011, 'ENROLLED')
    ta_after_tt, tk_after_tt = batch_of(client, [3011], category='TA'), batch_of(client, [3011])
    set_period(db, other_semester, semester=2)
    for batch in (ta_after_tt, tk_after_tt, tk_again, ta, same, other_semester, tk_after_ta):
        complete_dqa(client, db, batch)
    record_payment(db, tk, '3001')
    record_payment(db, tt, '3011')
    record_payment(db, ta_paid, '3002')
    # TK payment: blocks TK only; TA stays payable. Pending: never called paid.
    assert payable_now(client, db, tk_again) == (0, {'PAYMENT_PENDING': 1})
    listing = client.get(PAY + '/beneficiaries', params={'batch_id': tk_again}).json()['items'][0]
    assert listing['payment'] == {'payable': 0, 'potential': 0, 'state': 'recorded',
                                  'note': 'Payment recorded, not yet confirmed paid'}
    assert listing['payment_findings'][0]['code'] == 'PAYMENT_PENDING'
    assert payable_now(client, db, ta) == (1, {})
    # The same rules at save: a new TK batch for this member is refused, a TA batch is not.
    assert refused_save(client, [3001]) and batch_of(client, [3001], category='TA')
    # TT payment: blocks TT and TA, not TK.
    assert payable_now(client, db, tt_again) == (0, {'PAYMENT_PENDING': 1})
    assert payable_now(client, db, ta_after_tt) == (0, {'PAYMENT_PENDING': 1})
    assert payable_now(client, db, tk_after_tt) == (1, {})
    assert refused_save(client, [3011], category='TA') and batch_of(client, [3011])
    # TA payment: blocks the same academic year and semester only.
    assert payable_now(client, db, same) == (0, {'PAYMENT_PENDING': 1})
    assert payable_now(client, db, other_semester) == (1, {})
    assert payable_now(client, db, tk_after_ta) == (1, {})
    assert refused_save(client, [3002], category='TA') and batch_of(client, [3002])


def test_paid_void_and_unmapped_statuses(stack, monkeypatch):
    client, db, _ = stack
    add_people(db, [4001, 4002])
    paid_tk, again = batch_of(client, [4001]), batch_of(client, [4001])
    unmapped = {category: batch_of(client, [4002], category=category) for category in ('TK', 'TA')}
    complete_dqa(client, db, again)
    record_payment(db, paid_tk, '4001', status=7)
    # With no confirmed status values, every recorded payment is pending.
    assert payable(client, again) == (0, {'PAYMENT_PENDING': 1})
    monkeypatch.setattr(history, 'PAID_STATUSES', frozenset({'7'}))
    assert payable(client, again) == (0, {'ALREADY_PAID_TK': 1})
    listing = client.get(PAY + '/beneficiaries', params={'batch_id': again}).json()['items'][0]
    assert listing['payment']['state'] == 'locked' and listing['payment']['note'] == 'Already paid: TK is paid once'
    monkeypatch.setattr(history, 'PAID_STATUSES', frozenset())
    monkeypatch.setattr(history, 'VOID_STATUSES', frozenset({'7'}))
    assert payable(client, again) == (1, {})  # a void payment blocks nothing
    assert batch_of(client, [4001])  # nor a save
    monkeypatch.setattr(history, 'VOID_STATUSES', frozenset())
    # A payment that maps to no batch blocks every category.
    for batch in unmapped.values():
        complete_dqa(client, db, batch)
    record_payment(db, None, '4002')
    for category, batch in unmapped.items():
        assert payable_now(client, db, batch) == (0, {'PAYMENT_UNMAPPED': 1})
        assert refused_save(client, [4002], category=category)


@pytest.mark.parametrize('payment_category,payment_period,status,target,target_period,expected', [
    ('TK', ('2026-2027', 1), 1, 'TK', ('2030-2031', 2), 'PAYMENT_PENDING'),
    ('TK', ('2026-2027', 1), 1, 'TA', ('2026-2027', 1), None),
    ('TK', ('2026-2027', 1), 1, 'TT', ('2026-2027', 0), None),
    ('TT', ('2026-2027', 0), 1, 'TT', ('2027-2028', 0), 'PAYMENT_PENDING'),
    ('TT', ('2026-2027', 0), 1, 'TA', ('2027-2028', 2), 'PAYMENT_PENDING'),
    ('TT', ('2026-2027', 0), 1, 'TK', ('2026-2027', 1), None),
    ('TA', ('2026-2027', 1), 1, 'TA', ('2026-2027', 1), 'PAYMENT_PENDING'),
    ('TA', ('2026-2027', 1), 1, 'TA', ('2026-2027', 2), None),
    ('TA', ('2026-2027', 1), 1, 'TK', ('2026-2027', 1), None),
    ('TA', ('2026-2027', 1), 1, 'TT', ('2026-2027', 0), None),
    ('TK', ('2026-2027', 1), 'PAID', 'TK', ('2026-2027', 1), 'ALREADY_PAID_TK'),
    ('TT', ('2026-2027', 0), 'PAID', 'TT', ('2026-2027', 0), 'ALREADY_PAID_TT'),
    ('TT', ('2026-2027', 0), 'PAID', 'TA', ('2026-2027', 1), 'TA_BLOCKED_BY_TT_PAYMENT'),
    ('TA', ('2026-2027', 1), 'PAID', 'TA', ('2026-2027', 1), 'ALREADY_PAID_SAME_PERIOD'),
    ('TK', ('2026-2027', 1), 'VOID', 'TK', ('2026-2027', 1), None),
    (None, None, 1, 'TK', ('2026-2027', 1), 'PAYMENT_UNMAPPED'),
    (None, None, 1, 'TA', ('2026-2027', 1), 'PAYMENT_UNMAPPED'),
    (None, None, 1, 'TT', ('2026-2027', 0), 'PAYMENT_UNMAPPED'),
    ('TA', None, 1, 'TK', ('2026-2027', 1), 'PAYMENT_UNMAPPED'),  # TA payment without a period
])
def test_rules_and_sql_predicate_agree(real_stack, monkeypatch, payment_category, payment_period, status,
                                       target, target_period, expected):
    """findings() (DQA, payroll, generation) and blocking_sql() (candidates) give the same answer."""
    client, db, _ = real_stack
    monkeypatch.setattr(history, 'PAID_STATUSES', frozenset({'5'}))
    monkeypatch.setattr(history, 'VOID_STATUSES', frozenset({'6'}))
    status = {'PAID': 5, 'VOID': 6}.get(status, status)
    member = 5000 + next(_terms)
    cur = db.cursor(dictionary=True)
    batch_id = None
    if payment_category:
        batch_id = synthetic_batch(cur, payment_category, member, payment_period)
    cur.execute('INSERT INTO payment_transaction (batch_id,member_id,status) VALUES (%s,%s,%s)', (batch_id, member, status))
    found = history.check(cur, target, [{'member_ref': str(member), 'academic_year': target_period[0],
                                         'semester': target_period[1]}])[str(member)]
    assert [f['code'] for f in found] == ([expected] if expected else [])
    sql, params = history.blocking_sql(target, 'src.member_id', 'src.academic_year', 'src.semester')
    cur.execute(f'SELECT {sql} AS blocked FROM (SELECT %s AS member_id,%s AS academic_year,%s AS semester) src',
                (*params, member, *target_period))  # the predicate's placeholders come first
    assert bool(cur.fetchone()['blocked']) == bool(expected)
    db.rollback()
    cur.close()


def synthetic_batch(cur, category, member, period):
    """A frozen batch row with one member, written directly for rule tests (synthetic values only)."""
    cur.execute('''INSERT INTO bcmd_batch (public_id,name,category,criteria,policy_version,
        entitlement_def_version,dqa_ruleset_id,dqa_ruleset_version,dqa_ruleset_sha256,dqa_config_sha256,
        dqa_config_canonical,dqa_evaluation_date,snapshot_started_at,source_input_sha256,dqa_completed_at,
        candidate_count,member_count,excluded_not_eligible,excluded_no_tuloy_id,excluded_duplicate_episode,
        dqa_pass_count,dqa_warn_count,dqa_block_count,dqa_warn_issue_count,dqa_block_issue_count,dqa_outcome,
        created_by_agency,request_id,idempotency_key,request_hash,created_by_api_user_id)
        VALUES (%s,%s,%s,'{}','p','e','r','v',%s,SHA2('{}',256),'{}','2026-11-04','2026-11-04 00:00:00',%s,
        '2026-11-04 00:00:00',1,1,0,0,0,1,0,0,0,0,'CLEAN','DSWD','synthetic',%s,%s,11)''',
        (str(uuid4()), f'Synthetic rule batch {uuid4()}', category, '0' * 64, '0' * 64, str(uuid4()), '0' * 64))
    batch_id = cur.lastrowid
    if period:
        cur.execute("INSERT INTO bcmd_batch_member (batch_id,line_no,tuloy_id,academic_year,semester,source_record_id,"
                    "entitlement_hash,dqa_status,member_ref,last_name,first_name) VALUES "
                    "(%s,1,'SYN-RULE',%s,%s,%s,REPEAT('0',64),'PASS',%s,'Student','Synthetic')",
                    (batch_id, period[0], period[1], member, str(member)))
    return batch_id


def test_generation_skips_blocked_members_and_audits_codes_only(stack, caplog):
    client, db, _ = stack
    add_people(db, [6001, 6002])
    first, batch = batch_of(client, [6001]), batch_of(client, [6001, 6002])
    complete_dqa(client, db, batch)
    # Recorded after this batch passed DQA: generation must recheck and skip it.
    reference = record_payment(db, first, '6001')
    with caplog.at_level(logging.DEBUG):
        made = generate(client, batch)
    assert made.status_code == 200, made.text
    body = made.json()
    assert (body['created'], body['payment_blocked_count'], body['payment_findings']) == (1, 1, {'PAYMENT_PENDING': 1})
    assert payment_count(db, '6001') == 1 and payment_count(db, '6002') == 1
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT e.stage,e.line_no,e.finding_code,e.payment_ref,e.request_id FROM bcmd_payment_exclusion e '
                'JOIN bcmd_batch b ON b.id=e.batch_id WHERE b.public_id=%s', (batch,))
    audit = cur.fetchall()
    db.rollback(); cur.close()
    assert [(a['stage'], a['line_no'], a['finding_code'], a['payment_ref']) for a in audit] == [
        ('GENERATION', 1, 'PAYMENT_PENDING', reference)]
    # A second generation of the same batch (another term) pays nobody again.
    again = generate(client, batch)
    assert again.status_code == 400 and payment_count(db) == 2
    # Logs and the new audit rows hold no synthetic names, member IDs or household IDs.
    text = caplog.text + json.dumps(audit, default=str)
    for value in ('6001', '6002', *NAMES, 'SYN-6001', '106001'):
        assert value not in text


def test_duplicate_member_in_one_request_is_paid_once(stack):
    client, db, _ = stack
    add_people(db, [6101, 6102])
    batch = batch_of(client, [6101, 6102])
    complete_dqa(client, db, batch)
    # Frozen results stay PASS; the same member now appears on both lines of this request.
    cur = db.cursor()
    cur.execute("UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.member_ref='6101' "
                'WHERE b.public_id=%s', (batch,))
    db.commit(); cur.close()
    made = generate(client, batch)
    assert made.status_code == 200, made.text
    assert made.json()['created'] == 1 and made.json()['payment_findings'] == {'PAYMENT_PENDING': 1}
    assert payment_count(db, '6101') == 1


def test_concurrent_generations_in_different_terms_pay_a_tt_member_once(stack, monkeypatch, mysql_candidate):
    """Different term and assistance type: the term GET_LOCK does not serialize these requests."""
    client, db, app = stack
    add_people(db, [7001], status='GRADUATED')
    first = batch_of(client, [7001], category='TT')
    complete_dqa(client, db, first)
    second = batch_of(client, [7001], category='TT')
    # The two TT batches select the same member and period; each is checked on its own run.
    complete_dqa(client, db, second)
    revive(db, first)
    assert compute(client, first)['payable_count'] == 1 and compute(client, second)['payable_count'] == 1

    holding, release = threading.Event(), threading.Event()
    original = history.recorded

    def slow_first(cur, refs):
        found = original(cur, refs)
        if not holding.is_set():
            holding.set()  # the first request holds its beneficiary lock here, uncommitted
            assert release.wait(20)
        return found

    monkeypatch.setattr(history, 'recorded', slow_first)
    outcomes = {}
    one = threading.Thread(target=lambda: outcomes.setdefault('one', generate(client, first)))
    one.start()
    assert holding.wait(20)
    two = threading.Thread(target=lambda: outcomes.setdefault('two', generate(client, second)))
    two.start()
    time.sleep(1.5)
    assert two.is_alive()  # waiting on the beneficiary lock, not reading history
    release.set()
    one.join(30); two.join(30)
    assert outcomes['one'].status_code == 200, outcomes['one'].text
    assert outcomes['two'].status_code == 400  # it saw the committed payment and paid nobody
    assert payment_count(db, '7001') == 1


def test_without_the_beneficiary_lock_the_race_pays_twice(stack, monkeypatch):
    """Control: shows the lock is what prevents the duplicate above."""
    client, db, _ = stack
    add_people(db, [7101], status='GRADUATED')
    first = batch_of(client, [7101], category='TT')
    complete_dqa(client, db, first)
    second = batch_of(client, [7101], category='TT')
    complete_dqa(client, db, second)
    revive(db, first)
    holding, release = threading.Event(), threading.Event()
    original = history.recorded

    def slow_first(cur, refs):
        found = original(cur, refs)
        if not holding.is_set():
            holding.set()
            assert release.wait(20)
        return found

    monkeypatch.setattr(history, 'recorded', slow_first)
    monkeypatch.setattr(history, 'lock_members', lambda cur, refs: 0)
    outcomes = {}
    one = threading.Thread(target=lambda: outcomes.setdefault('one', generate(client, first)))
    one.start()
    assert holding.wait(20)
    outcomes['two'] = generate(client, second)
    release.set()
    one.join(30)
    assert outcomes['one'].status_code == outcomes['two'].status_code == 200
    assert payment_count(db, '7101') == 2


def test_unreadable_payment_history_fails_closed(stack, monkeypatch):
    client, db, _ = stack
    add_people(db, [8001])
    batch = batch_of(client, [8001])
    complete_dqa(client, db, batch)
    monkeypatch.setattr(history, 'SOURCE', 'synthetic_missing_payment_history')
    for response in (client.post(PAY + '/compute', json={'batch_id': batch}),
                     client.get(PAY + '/beneficiaries', params={'batch_id': batch}),
                     client.get(f'{BASE}/batches/{batch}/payroll-preview'),
                     generate(client, batch)):
        assert response.status_code == 503, response.text
        assert response.json()['error_code'] == 'PAYMENT_HISTORY_UNAVAILABLE'
        assert 'synthetic_missing' not in response.text
    assert payment_count(db) == 0


def findings_of(client, batch, run):
    items = client.get(f"{BASE}/batches/{batch}/dqa-runs/{run['public_id']}/results?limit=50").json()['items']
    return {i['member_ref']: (i['outcome'], [(f['rule_code'], f.get('payment_ref')) for f in i['findings']]) for i in items}


def test_failed_file_says_failed_and_issued_v1_files_still_download(stack, monkeypatch):
    """New Failed files label every row FAILED; a file issued as failed-v1 still downloads byte-identical."""
    client, db, _ = stack
    add_people(db, [9301, 9302, 9303, 9304])
    # Batches are saved before the payments are recorded; an exported batch is never re-run, so each file has its own.
    old_batch, new_batch = batch_of(client, [9301, 9302]), batch_of(client, [9303, 9304])
    paid_old, paid_new = batch_of(client, [9301]), batch_of(client, [9303])
    record_payment(db, paid_old, '9301')
    record_payment(db, paid_new, '9303')
    approve(db)
    run = complete_dqa(client, db, old_batch)
    assert (run['passed'], run['review_count']) == (1, 1)
    monkeypatch.setitem(bcmd_export.SERIALIZATION, 'FAILED', 'failed-v1')  # a file issued before this change
    old = new_export(client, old_batch, file_type='FAILED')
    assert old.status_code == 201, old.text
    monkeypatch.undo()
    first = client.get(old.json()['download_url'])
    assert first.status_code == 200, first.text
    assert csv_rows(first)[1][13] == 'FOR VERIFICATION'
    assert client.get(old.json()['download_url']).content == first.content
    run = complete_dqa(client, db, new_batch)
    assert (run['passed'], run['review_count']) == (1, 1)
    new = new_export(client, new_batch, file_type='FAILED')
    assert new.status_code == 201, new.text
    rows = csv_rows(client.get(new.json()['download_url']))
    assert rows[1][13:15] == ['FAILED', 'PAYMENT_PENDING']
    assert all('FOR VERIFICATION' not in cell for row in rows for cell in row)


def test_dqa_reports_payment_history_findings(stack, monkeypatch):
    client, db, _ = stack
    add_people(db, [9001, 9002])
    add_people(db, [9011], status='GRADUATED')
    # Saved before the payments are recorded (a save afterwards is refused); DQA runs after them.
    paid, again, tk_again = batch_of(client, [9001]), batch_of(client, [9001, 9002]), batch_of(client, [9001])
    tt = batch_of(client, [9011], category='TT')
    set_status(db, 9011, 'ENROLLED')
    ta, tk = batch_of(client, [9011], category='TA'), batch_of(client, [9011])
    tk_ref = record_payment(db, paid, '9001')
    tt_ref = record_payment(db, tt, '9011')
    run = complete_dqa(client, db, again)
    assert findings_of(client, again, run) == {'9001': ('REVIEW', [('PAYMENT_PENDING', tk_ref)]), '9002': ('PASS', [])}
    assert (run['passed'], run['review_count']) == (1, 1)
    # The FAILED file keeps its approved columns: the code and remark, never the payment reference.
    approve(db)
    failed = new_export(client, again, file_type='FAILED')
    assert failed.status_code == 201, failed.text
    rows = csv_rows(client.get(failed.json()['download_url']))
    assert rows[1][13:] == ['FAILED', 'PAYMENT_PENDING',
                            'Payment recorded, not yet confirmed paid. Another payment would be a duplicate under the program rules.']
    assert all('payment_transaction' not in cell for row in rows for cell in row)
    # Confirmed-paid statuses give the FAIL codes. TT ends TA but never TK.
    monkeypatch.setattr(history, 'PAID_STATUSES', frozenset({'1'}))
    assert findings_of(client, tk_again, complete_dqa(client, db, tk_again))['9001'] == ('FAIL', [('ALREADY_PAID_TK', tk_ref)])
    assert findings_of(client, ta, complete_dqa(client, db, ta))['9011'] == ('FAIL', [('TA_BLOCKED_BY_TT_PAYMENT', tt_ref)])
    assert findings_of(client, tk, complete_dqa(client, db, tk))['9011'] == ('PASS', [])


def test_dqa_run_that_cannot_read_payment_history_never_passes(stack, monkeypatch):
    import bcmd_dqa as dqa
    client, db, _ = stack
    add_people(db, [9101])
    batch = batch_of(client, [9101])
    isolate(db, batch)
    monkeypatch.setattr(history, 'SOURCE', 'synthetic_missing_payment_history')
    started = client.post(f'{BASE}/batches/{batch}/dqa-runs', headers={'Idempotency-Key': str(uuid4())}).json()
    for _ in range(3):
        assert dqa.work_once(db)
        cur = db.cursor()
        cur.execute("UPDATE bcmd_dqa_run SET next_attempt_at=UTC_TIMESTAMP(6) WHERE public_id=%s", (started['public_id'],))
        db.commit(); cur.close()
    run = client.get(f"{BASE}/batches/{batch}/dqa-runs/{started['public_id']}").json()
    assert (run['status'], run['processed'], run['passed'], run['last_error_code']) == ('FAILED', 0, 0, 'DQA_WORKER_FAILED')
    assert compute(client, batch)['blocking_reasons'] == ['DQA_FAILED']
