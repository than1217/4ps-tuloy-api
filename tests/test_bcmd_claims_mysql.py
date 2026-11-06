"""Duplicate-selection review on the isolated synthetic MySQL only (docs/bcmd-duplicate-selection-review.md).

Every scenario uses its own academic period: a conflict is one claim identity (member and period)
for the life of the database, so periods keep scenarios in this module independent.
"""
import hashlib
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import mysql.connector
import pytest

import bcmd_claims as claims
import bcmd_dqa as dqa
import bcmd_export as exports
from errors import ApiError
from routers import payments
from database import get_readonly_connection
from routers.bcmd import get_bcmd_db
from scopes import BCMD_SELECTION_REVIEW, LEGACY_CHED_READ
from security import Grant, Principal, get_beneficiary_principal
from test_bcmd_mysql import mysql_candidate, real_stack, BASE, complete_dqa, create, isolate
from test_bcmd_export_mysql import approve, handoff, new_export, csv_rows

REVIEW = BASE + '/duplicate-selections'
REASON = 'Synthetic review: batch prepared first for this period.'
PERSONAL_KEYS = {'member_ref', 'member_id', 'hhid', 'first_name', 'middle_name', 'last_name', 'ext_name',
                 'date_of_birth', 'date_of_birth_raw', 'fourps_member_id'}
_years = iter(range(2040, 2099))
HHID_FAILS = ['MISSING_HHID', 'NAPA_FIELD_INVALID']  # a missing HHID also fails the NAPA field check


def principal(user_id=11, *, kind='api', review=True, agency='DSWD'):
    grants = {Grant(LEGACY_CHED_READ, 'DSWD', 'DSWD')}
    if review:
        grants.add(Grant(BCMD_SELECTION_REVIEW, agency, agency))
    return Principal(user_id, f'synthetic-{kind}-reviewer-{user_id}', frozenset(grants), kind=kind)


def act_as(app, who):
    app.dependency_overrides[get_beneficiary_principal] = lambda: who


@pytest.fixture
def stack(real_stack):
    client, db, app = real_stack
    act_as(app, principal())
    return client, db, app


def scenario(client, db, *selections):
    """Batches (record id lists) isolated from earlier scenarios and moved to a fresh academic period."""
    year = next(_years)
    made = [create(client, ids=selections[0]).json()['public_id']]
    isolate(db, made[0])
    made += [create(client, ids=ids).json()['public_id'] for ids in selections[1:]]
    move(db, made, year)
    return (*made, year)


def add_batch(client, db, ids, year):
    batch = create(client, ids=ids).json()['public_id']
    move(db, [batch], year)
    return batch


def move(db, batches, year):
    cur = db.cursor()
    cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.academic_year=%s '
                f"WHERE b.public_id IN ({','.join(['%s'] * len(batches))})", (f'{year}-{year + 1}', *batches))
    db.commit(); cur.close()


def null_hhid(db, batch, line_no):
    cur = db.cursor()
    cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.hhid=NULL '
                'WHERE b.public_id=%s AND m.line_no=%s', (batch, line_no))
    db.commit(); cur.close()


def results(client, batch, run):
    items = client.get(f"{BASE}/batches/{batch}/dqa-runs/{run['public_id']}/results?limit=50").json()['items']
    return {i['member_ref']: (i['outcome'], sorted(f['rule_code'] for f in i['findings'])) for i in items}


def conflict_of(client, batch, run, member_ref='2'):
    items = client.get(f"{BASE}/batches/{batch}/dqa-runs/{run['public_id']}/results?limit=50").json()['items']
    item = next(i for i in items if i['member_ref'] == member_ref)
    return next(f['conflict_id'] for f in item['findings'] if f.get('conflict_id'))


def resolve(client, conflict, batch, version, *, key=None, reason=REASON):
    return client.post(f'{REVIEW}/{conflict}/resolution', headers={'Idempotency-Key': key or str(uuid4())},
                       json={'retained_batch_id': batch, 'reason': reason, 'expected_version': version})


def reverse(client, conflict, version, *, key=None, reason='Synthetic review: the other batch should keep it.'):
    return client.post(f'{REVIEW}/{conflict}/reversal', headers={'Idempotency-Key': key or str(uuid4())},
                       json={'reason': reason, 'expected_version': version})


def keys_of(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_of(item)


def selections(view):
    return {s['batch_id']: s for s in view['selections']}


def connection(mysql_candidate):
    """A separate session configured like get_bcmd_db, for concurrency tests."""
    db = mysql.connector.connect(**mysql_candidate)
    cur = db.cursor()
    cur.execute('SET SESSION innodb_lock_wait_timeout=5')
    cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
    cur.close()
    return db


def test_two_batches_resolution_then_fresh_dqa_excludes_the_other_selection_everywhere(stack):
    client, db, app = stack
    keep, lose, _ = scenario(client, db, [1, 2], [2, 3, 4])
    null_hhid(db, lose, 2)  # member 3: an unrelated FAIL that stays in effect
    approve(db)
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (keep, lose)}
    assert results(client, keep, runs[keep])['2'] == ('REVIEW', ['SELECTED_IN_OTHER_BATCH'])
    assert results(client, lose, runs[lose]) == {'2': ('REVIEW', ['SELECTED_IN_OTHER_BATCH']),
                                                 '3': ('FAIL', HHID_FAILS), '4': ('PASS', [])}
    conflict = conflict_of(client, lose, runs[lose])
    assert conflict == conflict_of(client, keep, runs[keep])
    listed = client.get(REVIEW, params={'batch_id': lose}).json()
    assert [c['conflict_id'] for c in listed['items']] == [conflict] and listed['has_more'] is False
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert view['status'] == 'UNRESOLVED' and view['version'] == 0 and view['history'] == {'items': [], 'has_more': False}
    assert {b: (s['line_no'], s['decision'], s['can_be_retained'], s['exported']) for b, s in selections(view).items()} == {
        keep: (2, None, True, False), lose: (1, None, True, False)}
    assert view['actions'] == {'resolve': {'allowed': True, 'reason': None},
                               'reverse': {'allowed': False, 'reason': 'CONFLICT_NOT_RESOLVED'}}
    assert not PERSONAL_KEYS & set(keys_of(view)) and not PERSONAL_KEYS & set(keys_of(listed))

    resolved = resolve(client, conflict, keep, 0)
    assert resolved.status_code == 201, resolved.text
    body = resolved.json()
    assert not PERSONAL_KEYS & set(keys_of(body))
    assert body['decision']['action'] == 'RESOLVE' and body['decision']['version'] == 1
    assert body['decision']['retained_batch_id'] == keep
    assert [b['batch_id'] for b in body['decision']['excluded_batches']] == [lose]
    assert body['decision']['reason'] == REASON and body['decision']['by_you'] is True
    assert body['conflict']['status'] == 'RESOLVED' and body['conflict']['retained_batch_id'] == keep
    assert {b: (s['decision'], s['dqa_refresh_required']) for b, s in selections(body['conflict']).items()} == {
        keep: ('RETAINED', True), lose: ('EXCLUDED', True)}

    # Stored results are never edited: both batches need fresh DQA before any file or the preview.
    assert results(client, lose, runs[lose])['2'] == ('REVIEW', ['SELECTED_IN_OTHER_BATCH'])
    for batch in (keep, lose):
        readiness = client.get(f'{BASE}/batches/{batch}/export-readiness').json()
        for file_type in ('DETAIL', 'SUMMARY', 'FAILED'):
            assert 'DQA_REFRESH_REQUIRED' in readiness['files'][file_type]['blocking_reasons']
        assert client.get(f'{BASE}/batches/{batch}').json()['dqa_refresh_required'] is True
        refused = new_export(client, batch)
        assert refused.status_code == 409 and refused.json()['error_code'] == 'NAPA_DQA_REQUIRED'
        preview = client.get(f'{BASE}/batches/{batch}/payroll-preview')
        assert preview.status_code == 422 and preview.json()['error_code'] == 'NAPA_DQA_REQUIRED'

    fresh = {b: complete_dqa(client, db, b, isolated=False) for b in (keep, lose)}
    assert (fresh[keep]['passed'], fresh[keep]['review_count'], fresh[keep]['excluded_count']) == (2, 0, 0)
    assert (fresh[lose]['passed'], fresh[lose]['failed'], fresh[lose]['review_count'], fresh[lose]['excluded_count'],
            fresh[lose]['processed']) == (1, 1, 0, 1, 3)
    assert results(client, keep, fresh[keep])['2'] == ('PASS', [])
    assert results(client, lose, fresh[lose]) == {'2': ('EXCLUDED', ['SELECTION_EXCLUDED']),
                                                  '3': ('FAIL', HHID_FAILS), '4': ('PASS', [])}
    assert conflict_of(client, lose, fresh[lose]) == conflict
    excluded = client.get(f"{BASE}/batches/{lose}/dqa-runs/{fresh[lose]['public_id']}/results?outcome=EXCLUDED").json()
    assert [i['member_ref'] for i in excluded['items']] == ['2']
    assert client.get(f'{BASE}/batches/{lose}').json()['dqa_refresh_required'] is False

    # The excluded record is in no NAPA file, not in the payroll preview, not in the payments listing.
    assert handoff(client, lose).status_code == 201
    readiness = client.get(f'{BASE}/batches/{lose}/export-readiness').json()
    assert {t: (f['ready'], f['row_count']) for t, f in readiness['files'].items()} == {
        'DETAIL': (True, 1), 'SUMMARY': (True, 1), 'FAILED': (True, 1)}
    assert client.get(f'{BASE}/batches/{lose}/payroll-preview').json()['member_count'] == 1
    assert client.get(f'{BASE}/batches/{keep}/payroll-preview').json()['member_count'] == 2
    assert [r['fourps_member_id'] for r in payments._fetch_batch_beneficiaries(db, lose)[0]] == ['3', '4']
    assert [r['fourps_member_id'] for r in payments._fetch_batch_beneficiaries(db, keep)[0]] == ['1', '2']
    db.rollback()
    detail = new_export(client, lose)
    assert detail.status_code == 201, detail.text
    assert [r[10] for r in csv_rows(client.get(detail.json()['download_url']))[1:]] == ['4']
    summary = new_export(client, lose, file_type='SUMMARY')
    assert csv_rows(client.get(summary.json()['download_url']))[-1][8] == '1'
    failed = new_export(client, lose, file_type='FAILED')
    rows = csv_rows(client.get(failed.json()['download_url']))
    assert [(r[0], r[13]) for r in rows[1:]] == [('2', 'FAILED')]  # line 2 = member 3; the excluded line 1 is absent
    assert rows[0] == list(exports.FAILED_HEADERS)  # approved columns unchanged; no conflict ID in any file


def test_payments_member_queries_all_carry_the_exclusion_guard():
    import bcmd_eligibility
    # Every payments member read goes through the shared eligibility, whose one member query carries the guard.
    assert 'eligibility.evaluate(' in inspect.getsource(payments._eligibility)
    assert 'eligibility.evaluate(' in inspect.getsource(payments.generate_payments)
    for function in (payments.list_beneficiaries, payments.compute_payments, payments._export_rows):
        assert '_fetch_batch_beneficiaries(' in inspect.getsource(function), function.__name__
    assert 'NOT_EXCLUDED_BY_REVIEW' in inspect.getsource(bcmd_eligibility.evaluate)
    assert 'FROM bcmd_batch_member' not in inspect.getsource(payments)
    assert "x.decision='EXCLUDED'" in claims.NOT_EXCLUDED_BY_REVIEW


def test_three_batches_new_selection_and_unrelated_failures(stack):
    client, db, app = stack
    first, kept, third, year = scenario(client, db, [1, 2], [2, 3], [2, 4])
    null_hhid(db, third, 1)  # member 2 in the third batch also fails a check unrelated to the review
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (first, kept, third)}
    assert results(client, third, runs[third])['2'] == ('FAIL', sorted(HHID_FAILS + ['SELECTED_IN_OTHER_BATCH']))
    conflict = conflict_of(client, first, runs[first])
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert view['selection_count'] == 3 and set(selections(view)) == {first, kept, third}
    body = resolve(client, conflict, kept, 0).json()
    assert sorted(b['batch_id'] for b in body['decision']['excluded_batches']) == sorted([first, third])
    fresh = {b: complete_dqa(client, db, b, isolated=False) for b in (first, kept, third)}
    assert results(client, first, fresh[first])['2'] == ('EXCLUDED', ['SELECTION_EXCLUDED'])
    assert results(client, kept, fresh[kept])['2'] == ('PASS', [])
    # FAIL keeps precedence over EXCLUDED; the unrelated finding stays.
    assert results(client, third, fresh[third])['2'] == ('FAIL', sorted(HHID_FAILS + ['SELECTION_EXCLUDED']))
    assert (fresh[third]['failed'], fresh[third]['excluded_count']) == (1, 0)

    # A batch created after the decision is not covered: it and the kept batch go back to verification.
    later = add_batch(client, db, [2], year)
    later_run = complete_dqa(client, db, later, isolated=False)
    assert results(client, later, later_run)['2'] == ('REVIEW', ['SELECTED_IN_OTHER_BATCH'])
    assert conflict_of(client, later, later_run) == conflict
    assert results(client, kept, complete_dqa(client, db, kept, isolated=False))['2'] == (
        'REVIEW', ['SELECTED_IN_OTHER_BATCH'])
    assert results(client, first, complete_dqa(client, db, first, isolated=False))['2'] == (
        'EXCLUDED', ['SELECTION_EXCLUDED'])
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert view['status'] == 'NEEDS_REVIEW'
    assert {b: (s['decision'], s['can_be_retained']) for b, s in selections(view).items()} == {
        first: ('EXCLUDED', False), kept: ('RETAINED', True), third: ('EXCLUDED', False), later: ('NOT_COVERED', False)}
    switched = resolve(client, conflict, later, 1)
    assert switched.status_code == 409 and switched.json()['error_code'] == 'REVERSAL_REQUIRED'
    confirmed = resolve(client, conflict, kept, 1)
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()['conflict']['status'] == 'RESOLVED' and confirmed.json()['conflict']['version'] == 2
    assert results(client, later, complete_dqa(client, db, later, isolated=False))['2'] == (
        'EXCLUDED', ['SELECTION_EXCLUDED'])
    assert results(client, kept, complete_dqa(client, db, kept, isolated=False))['2'] == ('PASS', [])


def test_retained_batch_purged_keeps_exclusions_until_reversed(stack):
    client, db, app = stack
    first, second, third, _ = scenario(client, db, [1, 2], [2, 3], [2, 4])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (first, second, third)}
    conflict = conflict_of(client, first, runs[first])
    assert resolve(client, conflict, first, 0).status_code == 201
    cur = db.cursor()  # synthetic purge marker on a pytest database, as isolate() does
    cur.execute("UPDATE bcmd_batch SET status='PURGED',purged_at=UTC_TIMESTAMP(3),purged_by='synthetic' WHERE public_id=%s",
                (first,))
    db.commit(); cur.close()
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert view['status'] == 'RETAINED_UNAVAILABLE' and set(selections(view)) == {second, third}
    assert view['actions']['resolve'] == {'allowed': False, 'reason': 'REVERSAL_REQUIRED'}
    assert resolve(client, conflict, second, 1).json()['error_code'] == 'REVERSAL_REQUIRED'
    assert results(client, second, complete_dqa(client, db, second, isolated=False))['2'] == (
        'EXCLUDED', ['SELECTION_EXCLUDED'])
    act_as(app, principal(11, kind='staff'))
    assert reverse(client, conflict, 1).status_code == 201
    assert resolve(client, conflict, third, 2).status_code == 201
    assert results(client, second, complete_dqa(client, db, second, isolated=False))['2'] == (
        'EXCLUDED', ['SELECTION_EXCLUDED'])
    assert results(client, third, complete_dqa(client, db, third, isolated=False))['2'] == ('PASS', [])


def test_reversal_needs_a_second_reviewer_and_keeps_history_and_audit(stack):
    client, db, app = stack
    keep, other, _ = scenario(client, db, [1, 2], [2, 3])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (keep, other)}
    conflict = conflict_of(client, keep, runs[keep])
    first = resolve(client, conflict, keep, 0).json()['decision']
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert view['actions']['reverse'] == {'allowed': False, 'reason': 'SECOND_REVIEWER_REQUIRED'}
    same = reverse(client, conflict, 1)
    assert same.status_code == 403 and same.json()['error_code'] == 'SECOND_REVIEWER_REQUIRED'
    # Native user 11 and staff user 11 are different reviewers.
    act_as(app, principal(11, kind='staff'))
    assert client.get(f'{REVIEW}/{conflict}').json()['actions']['reverse'] == {'allowed': True, 'reason': None}
    started = client.post(f'{BASE}/batches/{other}/dqa-runs', headers={'Idempotency-Key': str(uuid4())})
    assert started.status_code == 202
    reversed_ = reverse(client, conflict, 1)
    assert reversed_.status_code == 201, reversed_.text
    decision = reversed_.json()['decision']
    assert decision['action'] == 'REVERSE' and decision['reverses_decision_id'] == first['decision_id']
    assert decision['retained_batch_id'] is None and decision['version'] == 2
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert view['status'] == 'UNRESOLVED' and view['retained_batch_id'] is None and view['version'] == 2
    assert [(h['version'], h['action'], h['by_you']) for h in view['history']['items']] == [
        (2, 'REVERSE', True), (1, 'RESOLVE', False)]
    assert view['history']['items'][1]['reviewer'] == 'synthetic-api-reviewer-11'
    assert view['history']['items'][1]['reason'] == REASON
    assert not PERSONAL_KEYS & set(keys_of(view))
    again = reverse(client, conflict, 2)
    assert again.status_code == 409 and again.json()['error_code'] == 'CONFLICT_NOT_RESOLVED'
    # The queued run that started before the reversal is stopped, never completed on stale decisions.
    run = client.get(f"{BASE}/batches/{other}/dqa-runs/{started.json()['public_id']}").json()
    assert run['status'] == 'FAILED' and run['last_error_code'] == 'CLAIM_DECISION_CHANGED'
    cur = db.cursor(dictionary=True)
    cur.execute('''SELECT b.public_id,e.event_type,e.reason_code,e.actor_api_user_id,e.actor_staff_user_id,e.request_id
        FROM bcmd_audit_event e JOIN bcmd_batch b ON b.id=e.batch_id
        WHERE b.public_id IN (%s,%s) AND e.event_type IN ('CLAIM_RESOLVED','CLAIM_REVERSED','DQA_RUN_INVALIDATED')
        ORDER BY e.id''', (keep, other))
    events = cur.fetchall()
    db.rollback(); cur.close()
    assert sorted((e['public_id'] == keep, e['event_type'], e['reason_code'], e['actor_api_user_id'],
                   e['actor_staff_user_id']) for e in events) == sorted([
        (True, 'CLAIM_RESOLVED', 'RETAINED', 11, None), (False, 'CLAIM_RESOLVED', 'EXCLUDED', 11, None),
        (False, 'DQA_RUN_INVALIDATED', 'CLAIM_DECISION_CHANGED', None, 11),
        (True, 'CLAIM_REVERSED', 'REVERSED', None, 11), (False, 'CLAIM_REVERSED', 'REVERSED', None, 11)])
    assert all(e['request_id'] for e in events)
    fresh = {b: complete_dqa(client, db, b, isolated=False) for b in (keep, other)}
    for batch in (keep, other):
        assert results(client, batch, fresh[batch])['2'] == ('REVIEW', ['SELECTED_IN_OTHER_BATCH'])


def test_exported_claim_cannot_be_reassigned_and_downloads_stay_byte_identical(stack):
    client, db, app = stack
    keep, other, _ = scenario(client, db, [1, 2], [2, 3])
    approve(db)
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (keep, other)}
    conflict = conflict_of(client, keep, runs[keep])
    assert resolve(client, conflict, keep, 0).status_code == 201
    complete_dqa(client, db, keep, isolated=False)
    export = new_export(client, keep)
    assert export.status_code == 201
    before = client.get(export.json()['download_url']).content
    assert hashlib.sha256(before).hexdigest() == export.json()['sha256']
    view = client.get(f'{REVIEW}/{conflict}').json()
    assert selections(view)[keep]['exported'] is True
    assert view['actions'] == {'resolve': {'allowed': False, 'reason': 'CLAIM_EXPORTED'},
                               'reverse': {'allowed': False, 'reason': 'CLAIM_EXPORTED'}}
    act_as(app, principal(11, kind='staff'))
    for refused in (reverse(client, conflict, 1), resolve(client, conflict, other, 1)):
        assert refused.status_code == 409 and refused.json()['error_code'] == 'CLAIM_EXPORTED'
    assert client.get(f'{REVIEW}/{conflict}').json()['version'] == 1
    # The other batch can never claim it: exported in the kept batch, excluded here.
    assert results(client, other, complete_dqa(client, db, other, isolated=False))['2'] == (
        'FAIL', ['DUPLICATE_CLAIM', 'SELECTION_EXCLUDED'])
    assert client.get(export.json()['download_url']).content == before


def test_resolution_idempotency_stale_versions_and_concurrency(stack, mysql_candidate):
    client, db, app = stack
    first, second, _ = scenario(client, db, [1, 2], [2, 3])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (first, second)}
    conflict = conflict_of(client, first, runs[first])
    key = str(uuid4())
    one = resolve(client, conflict, first, 0, key=key)
    assert one.status_code == 201
    replay = resolve(client, conflict, first, 0, key=key)
    assert replay.status_code == 201 and replay.json()['decision'] == one.json()['decision']
    for changed in (resolve(client, conflict, second, 0, key=key), reverse(client, conflict, 0, key=key),
                    resolve(client, conflict, first, 0, key=key, reason='Synthetic: a different reason text.')):
        assert changed.status_code == 409 and changed.json()['error_code'] == 'IDEMPOTENCY_CONFLICT'
    stale = resolve(client, conflict, first, 0)
    assert stale.status_code == 409 and stale.json()['error_code'] == 'CONFLICT_VERSION_STALE'
    done = resolve(client, conflict, first, 1)
    assert done.status_code == 409 and done.json()['error_code'] == 'CONFLICT_ALREADY_RESOLVED'
    cur = db.cursor()
    cur.execute('SELECT COUNT(*) FROM bcmd_claim_resolution r JOIN bcmd_claim_conflict c ON c.id=r.conflict_id '
                'WHERE c.public_id=%s', (conflict,))
    assert cur.fetchone()[0] == 1
    db.rollback(); cur.close()

    # Two different reviewers at the same version: exactly one decision is recorded.
    third, fourth, _ = scenario(client, db, [1, 2], [2, 3])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (third, fourth)}
    conflict = conflict_of(client, third, runs[third])
    barrier = threading.Barrier(2)

    def attempt(who, batch, key):
        session = connection(mysql_candidate)
        try:
            barrier.wait()
            return claims.resolve(session, conflict, retained_batch_id=batch, reason=REASON, expected_version=0,
                                  key=key, actor=who, request_id=f'synthetic-{uuid4().hex}')
        except ApiError as exc:
            return exc.error_code
        finally:
            session.close()

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(attempt, [principal(11), principal(11, kind='staff')], [third, fourth],
                                 [str(uuid4()), str(uuid4())]))
    assert sorted(type(o).__name__ for o in outcomes) == ['dict', 'str'], outcomes
    assert next(o for o in outcomes if isinstance(o, str)) in ('CONFLICT_VERSION_STALE', 'CLAIM_REVIEW_BUSY')
    assert client.get(f'{REVIEW}/{conflict}').json()['version'] == 1

    # The same key sent twice at once: one decision, and both callers receive it.
    fifth, sixth, _ = scenario(client, db, [1, 2], [2, 3])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (fifth, sixth)}
    conflict = conflict_of(client, fifth, runs[fifth])
    key = str(uuid4())
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(attempt, [principal(11)] * 2, [fifth] * 2, [key] * 2))
    assert all(isinstance(o, dict) for o in outcomes), outcomes
    assert outcomes[0]['decision']['decision_id'] == outcomes[1]['decision']['decision_id']


def test_decisions_race_safely_with_running_dqa_and_export(stack, mysql_candidate, monkeypatch):
    client, db, app = stack
    first, second, _ = scenario(client, db, [1, 2], [2, 3])
    approve(db)
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (first, second)}
    conflict = conflict_of(client, first, runs[first])
    # A decision lands after the worker evaluated a chunk but before it locked the batch.
    started = client.post(f'{BASE}/batches/{second}/dqa-runs', headers={'Idempotency-Key': str(uuid4())}).json()
    original, fired = claims.decisions, []

    def decide_meanwhile(cur, refs):
        found = original(cur, refs)
        if not fired:
            fired.append(True)
            session = connection(mysql_candidate)
            try:
                claims.resolve(session, conflict, retained_batch_id=first, reason=REASON, expected_version=0,
                               key=str(uuid4()), actor=principal(11), request_id='synthetic-race')
            finally:
                session.close()
        return found

    monkeypatch.setattr(claims, 'decisions', decide_meanwhile)
    assert dqa.work_once(db)
    monkeypatch.setattr(claims, 'decisions', original)
    assert fired
    run = client.get(f"{BASE}/batches/{second}/dqa-runs/{started['public_id']}").json()
    assert run['status'] == 'FAILED' and run['last_error_code'] == 'CLAIM_DECISION_CHANGED' and run['processed'] == 0
    cur = db.cursor()
    cur.execute('SELECT COUNT(*) FROM bcmd_dqa_result r JOIN bcmd_dqa_run u ON u.id=r.run_id WHERE u.public_id=%s',
                (started['public_id'],))
    assert cur.fetchone()[0] == 0
    db.rollback(); cur.close()
    assert results(client, second, complete_dqa(client, db, second, isolated=False))['2'] == (
        'EXCLUDED', ['SELECTION_EXCLUDED'])
    complete_dqa(client, db, first, isolated=False)

    # An export of the kept batch and a reversal at the same moment: never both.
    assert handoff(client, first).status_code == 201
    barrier = threading.Barrier(2)

    def export_now():
        session = connection(mysql_candidate)
        try:
            barrier.wait()
            return exports.create(session, first, uuid4(), principal(11), 'synthetic-export')
        except ApiError as exc:
            return exc.error_code
        finally:
            session.close()

    def reverse_now():
        session = connection(mysql_candidate)
        try:
            barrier.wait()
            return claims.reverse(session, conflict, reason='Synthetic race: reverse while exporting.',
                                  expected_version=1, key=uuid4(), actor=principal(11, kind='staff'),
                                  request_id='synthetic-race')
        except ApiError as exc:
            return exc.error_code
        finally:
            session.close()

    with ThreadPoolExecutor(2) as pool:
        export_future, reverse_future = pool.submit(export_now), pool.submit(reverse_now)
        exported, reversed_ = export_future.result(), reverse_future.result()
    assert (isinstance(exported, dict), isinstance(reversed_, dict)) in ((True, False), (False, True)), (exported, reversed_)
    if isinstance(exported, dict):
        assert reversed_ == 'CLAIM_EXPORTED'
        assert client.get(f'{REVIEW}/{conflict}').json()['version'] == 1
    else:
        assert exported == 'NAPA_DQA_REQUIRED'
        assert client.get(f'{BASE}/batches/{first}').json()['export_seq'] == 0


def test_review_permission_is_required_before_any_database_access(stack):
    client, db, app = stack
    opened = []

    def tracking():
        opened.append(True)
        yield db
        db.rollback()

    app.dependency_overrides[get_bcmd_db] = tracking
    conflict, batch = uuid4(), str(uuid4())
    calls = [lambda: client.get(REVIEW), lambda: client.get(f'{REVIEW}/{conflict}'),
             lambda: resolve(client, conflict, batch, 0), lambda: reverse(client, conflict, 0)]
    for who in (principal(review=False), principal(agency='TEST'), principal(11, kind='staff', review=False)):
        act_as(app, who)
        for call in calls:
            response = call()
            assert response.status_code == 403 and response.json()['error_code'] == 'FORBIDDEN', response.text
    assert opened == []
    app.dependency_overrides.pop(get_beneficiary_principal)

    def no_auth_db():
        yield None

    app.dependency_overrides[get_readonly_connection] = no_auth_db  # missing token is refused first
    try:
        for call in calls:
            assert call().status_code == 401
    finally:
        app.dependency_overrides.pop(get_readonly_connection)
    assert opened == []
    act_as(app, principal())
    assert client.get(f'{REVIEW}/{conflict}').json()['error_code'] == 'NOT_FOUND'


def test_requests_are_validated_and_take_no_personal_identifiers(stack):
    client, db, app = stack
    first, second, _ = scenario(client, db, [1, 2], [2, 3])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (first, second)}
    conflict = conflict_of(client, first, runs[first])
    path = f'{REVIEW}/{conflict}/resolution'
    good = {'retained_batch_id': first, 'reason': REASON, 'expected_version': 0}
    for body in ({**good, 'reason': 'too short'}, {**good, 'reason': 'x' * 501},
                 {**good, 'reason': 'Synthetic reason\nwith a control character'},
                 {**good, 'member_ref': '2'}, {**good, 'retained_batch_id': 'batch-1'},
                 {**good, 'expected_version': '0'}, {**good, 'expected_version': -1},
                 {k: v for k, v in good.items() if k != 'reason'}):
        response = client.post(path, headers={'Idempotency-Key': str(uuid4())}, json=body)
        assert response.status_code == 422, body
    assert client.post(path, json=good).status_code == 422  # Idempotency-Key is required
    assert client.post(path, headers={'Idempotency-Key': 'not-a-uuid'}, json=good).status_code == 422
    stranger = resolve(client, conflict, str(uuid4()), 0)
    assert stranger.status_code == 422 and stranger.json()['error_code'] == 'RETAINED_BATCH_NOT_IN_CONFLICT'
    assert client.get(REVIEW, params={'limit': 51}).status_code == 422
    assert client.get(f'{REVIEW}/not-a-uuid').status_code == 422
    assert client.get(f'{REVIEW}/{conflict}').json()['version'] == 0


def test_recorded_payment_or_unreadable_payments_refuse_decisions(stack, monkeypatch):
    client, db, app = stack
    first, second, _ = scenario(client, db, [1, 2], [2, 3])
    runs = {b: complete_dqa(client, db, b, isolated=False) for b in (first, second)}
    conflict = conflict_of(client, first, runs[first])
    monkeypatch.setattr(claims, '_paid_batches', lambda cur, ids: None)
    unknown = resolve(client, conflict, first, 0)
    assert unknown.status_code == 503 and unknown.json()['error_code'] == 'CLAIM_PAYMENT_CHECK_UNAVAILABLE'
    monkeypatch.undo()
    cur = db.cursor()
    try:
        cur.execute('INSERT INTO payment_transaction (batch_id) SELECT id FROM bcmd_batch WHERE public_id=%s', (second,))
        db.commit()
        paid = resolve(client, conflict, first, 0)
        assert paid.status_code == 409 and paid.json()['error_code'] == 'CLAIM_PAYMENT_RECORDED'
        assert client.get(f'{REVIEW}/{conflict}').json()['actions']['resolve']['reason'] == 'CLAIM_PAYMENT_RECORDED'
    finally:
        db.rollback()
        cur.execute('DELETE FROM payment_transaction')  # synthetic table of this pytest database
        db.commit()
        cur.close()
    assert resolve(client, conflict, first, 0).status_code == 201


def test_rollback_0023_restores_the_previous_schema(mysql_candidate):
    from scripts import migrate
    args = {k: mysql_candidate[k] for k in ('host', 'port', 'user', 'password')}
    assert args['host'] == '127.0.0.1' and args['port'] == 13498
    database = 'tuloy_napa_test_' + uuid4().hex[:12]
    admin = mysql.connector.connect(**args, autocommit=True)
    cur = admin.cursor(dictionary=True)
    try:
        cur.execute(f'CREATE DATABASE `{database}`')
        cur.execute(f'USE `{database}`')
        migrate.ensure_ledger(cur)
        for version, name, checksum, sql in migrate.discover():
            for statement in migrate.statements(sql):
                cur.execute(statement)
            cur.execute('INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)', (version, name, checksum))
        # Later migrations roll back first, newest first.
        for path in sorted(Path('migrations/rollback').glob('*.sql'), reverse=True):
            if path.name >= '0023':
                for statement in migrate.statements(path.read_text(encoding='utf8')):
                    cur.execute(statement)
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME LIKE 'bcmd_claim%'")
        assert cur.fetchone()['n'] == 0
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND COLUMN_NAME IN ('claim_epoch','excluded_count')")
        assert cur.fetchone()['n'] == 0
        cur.execute("SELECT MAX(version) AS v FROM schema_migrations")
        assert cur.fetchone()['v'] == '0022'
        for statement in migrate.statements(next(Path('migrations').glob('0023_*.sql')).read_text(encoding='utf8')):
            cur.execute(statement)  # forward again after the rollback
    finally:
        assert database.startswith('tuloy_napa_test_') and len(database) == len('tuloy_napa_test_') + 12
        cur.execute(f'DROP DATABASE IF EXISTS `{database}`')
        cur.close(); admin.close()
