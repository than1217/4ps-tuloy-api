"""Unified CHED/TESDA candidate source on the isolated synthetic MySQL only.

The same precedence as the unified matched view: CHED degree records, plus TESDA diploma records
only when that tuloy_id has no CHED match. TESDA short courses are counted, never selectable.
Every person, enrollment, batch and payment here is synthetic.
"""
import json
from uuid import uuid4

import pytest

from bcmd_candidates import parse_ref
from test_bcmd_mysql import BASE, complete_dqa, mysql_candidate, real_stack
from test_bcmd_criteria_mysql import criteria_create
from test_bcmd_export_mysql import approve, csv_rows, new_export

YEAR = '2035-2036'


def person(cur, n):
    """One synthetic eligible person (age 25) with one household and family."""
    cur.execute("INSERT INTO tbl_person VALUES (%s,'Synthetic','Unified','Student','2001-02-03',1,2,0)", (n,))
    cur.execute('INSERT INTO tbl_hh_roster VALUES (%s,%s,%s)', (n, n, n + 100000))
    cur.execute('INSERT INTO tbl_household VALUES (%s,1)', (n + 100000,))
    cur.execute('INSERT INTO tbl_family_selected VALUES (%s,%s)', (n + 100000, f'SYN-UFAM-{n}'))
    cur.execute('INSERT INTO tbl_family_assoc_ops VALUES (%s,%s,%s,%s,0)', (n, f'SYN-UFAM-{n}', n, n))


def ched(cur, n, *, status='ENROLLED', semester=1, year_level='1'):
    cur.execute("INSERT INTO enroll_matched_CHED_tbl VALUES (%s,%s,%s,1,%s,%s,%s,%s,'Synthetic course')",
                (n, f'SYN-U-{n}', n, YEAR, semester, year_level, status))


def tesda(cur, n, *, status='ENROLLED', semester=1, year_level='1', short=False):
    diploma = None if short else json.dumps({'program_name': 'Synthetic TESDA diploma', 'enrollment_status': status,
                                             'academic_year': YEAR, 'year_level': year_level, 'semester': semester})
    course = json.dumps({'uli_id': 'SYN-ULI', 'start_date': '2035-06-01', 'end_date': '2035-08-01'}) if short else None
    cur.execute('INSERT INTO enroll_matched_TESDA_tbl VALUES (%s,%s,%s,%s,%s)', (n, f'SYN-U-{n}', n, diploma, course))


@pytest.fixture(scope='module')
def unified(mysql_candidate):
    """70001 CHED; 70002 TESDA diploma; 70003 both (CHED wins); 70004 TESDA short course;
    70005 TESDA diploma, semester '2' as text and year level 2 (TA only); 70006 TESDA graduate (TT)."""
    import mysql.connector
    db = mysql.connector.connect(**mysql_candidate)
    cur = db.cursor()
    for n in range(70001, 70007):
        person(cur, n)
    ched(cur, 70001)
    tesda(cur, 70002)
    ched(cur, 70003)
    tesda(cur, 70003)
    tesda(cur, 70004, short=True)
    tesda(cur, 70005, semester='2', year_level='2')
    tesda(cur, 70006, status='GRADUATED')
    db.commit()
    cur.close()
    db.close()


def refs(body):
    return [row['source_ref'] for row in body['items']]


def save(client, record_refs, category='TK'):
    key = str(uuid4())
    return client.post(BASE + '/batches', headers={'Idempotency-Key': key},
                       json={'name': f'Synthetic unified {key}', 'category': category, 'record_refs': record_refs})


def code(response):
    return response.status_code, response.json().get('error_code')


def test_source_refs_are_strict():
    assert parse_ref('CHED:7:degree') == ('CHED', 7, 'degree')
    assert parse_ref('TESDA:7:diploma') == ('TESDA', 7, 'diploma')
    for bad in ('CHED:7:diploma', 'TESDA:7:degree', 'TESDA:7:short_course', 'CHED:0:degree', 'ched:7:degree',
                'CHED:7:degree ', 'CHED:07:degree', 'DEPED:7:degree', ''):
        with pytest.raises(ValueError):
            parse_ref(bad)


def test_candidates_unify_ched_and_tesda_with_ched_precedence(real_stack, unified):
    client, db, _ = real_stack
    body = client.get(BASE + f'/candidates?limit=200&academic_year={YEAR}').json()
    # CHED first, then TESDA, each in source-ID order; the TESDA row of 70003 is hidden by its CHED match.
    assert refs(body) == ['CHED:70001:degree', 'CHED:70003:degree', 'TESDA:70002:diploma']
    assert (body['total_count'], body['total_status'], body['count_unit']) == (3, 'EXACT', 'ENROLLMENT_RECORD')
    assert body['short_course_excluded_count'] == 1 and body['payment_excluded_count'] == 0
    row = body['items'][2]
    assert (row['source_agency'], row['course_type'], row['source_record_id'], row['semester'], row['year_level'],
            row['enrollment_status'], row['academic_year']) == ('TESDA', 'diploma', 70002, 1, '1', 'ENROLLED', YEAR)
    assert 'source_order' not in row
    ta = client.get(BASE + f'/candidates?category=TA&limit=200&academic_year={YEAR}&semester=2&year_level=2').json()
    assert refs(ta) == ['TESDA:70005:diploma'] and ta['items'][0]['semester'] == 2
    tt = client.get(BASE + f'/candidates?category=TT&limit=200&academic_year={YEAR}').json()
    assert refs(tt) == ['TESDA:70006:diploma']
    searched = client.get(BASE + f'/candidates?limit=200&academic_year={YEAR}&search=TESDA%20diploma').json()
    assert refs(searched) == ['TESDA:70002:diploma']


def test_explicit_refs_save_per_member_source(real_stack, unified):
    client, db, _ = real_stack
    mixed = save(client, ['TESDA:70002:diploma', 'CHED:70001:degree'])
    assert mixed.status_code == 201, mixed.text
    assert mixed.json()['source_agency'] == 'MIXED' and mixed.json()['payment_excluded_count'] is None
    members = client.get(BASE + f"/batches/{mixed.json()['public_id']}/members").json()['items']
    assert [(m['line_no'], m['source_agency'], m['course_type']) for m in members] == [
        (1, 'CHED', 'degree'), (2, 'TESDA', 'diploma')]
    only = save(client, ['TESDA:70002:diploma'])
    assert only.status_code == 201 and only.json()['source_agency'] == 'TESDA'
    # Hidden by CHED precedence, a short course, a wrong category: none can be saved.
    assert code(save(client, ['TESDA:70003:diploma'])) == (422, 'SOURCE_NOT_ELIGIBLE')
    assert code(save(client, ['TESDA:70004:diploma'])) == (422, 'SOURCE_NOT_ELIGIBLE')
    assert code(save(client, ['TESDA:70006:diploma'])) == (422, 'SOURCE_NOT_ELIGIBLE')
    assert save(client, ['TESDA:70004:short_course']).status_code == 422
    assert save(client, ['CHED:70001:degree', 'CHED:70001:degree']).status_code == 422
    both = client.post(BASE + '/batches', headers={'Idempotency-Key': str(uuid4())},
                       json={'name': 'Synthetic both', 'category': 'TK', 'record_ids': [70001],
                             'record_refs': ['CHED:70001:degree']})
    assert both.status_code == 422
    # record_ids still name CHED records only.
    assert code(client.post(BASE + '/batches', headers={'Idempotency-Key': str(uuid4())},
                            json={'name': 'Synthetic ids', 'category': 'TK', 'record_ids': [70002]})) == (422, 'SOURCE_NOT_ELIGIBLE')


def test_criteria_save_matches_preview_and_records_payment_exclusions(real_stack, unified):
    client, db, _ = real_stack
    paid = save(client, ['CHED:70001:degree'])
    assert paid.status_code == 201
    cur = db.cursor()
    cur.execute('SELECT id FROM bcmd_batch WHERE public_id=%s', (paid.json()['public_id'],))
    batch_id = cur.fetchone()[0]
    cur.execute('INSERT INTO payment_transaction (batch_id,member_id,academic_year_term,assistance_type,amount,status) '
                'VALUES (%s,70001,9101,1,5000,1)', (batch_id,))
    db.commit()
    try:
        preview = client.get(BASE + f'/candidates?limit=200&academic_year={YEAR}').json()
        assert refs(preview) == ['CHED:70003:degree', 'TESDA:70002:diploma']
        assert (preview['total_count'], preview['payment_excluded_count']) == (2, 1)
        # TK payment: TA is not blocked.
        ta = client.get(BASE + f'/candidates?category=TA&limit=200&academic_year={YEAR}&semester=1').json()
        assert 'CHED:70001:degree' in refs(ta) and ta['payment_excluded_count'] == 0
        assert code(criteria_create(client, criteria={'academic_year': YEAR}, count=3)) == (422, 'RECORD_COUNT_EXCEEDS_ELIGIBLE')
        made = criteria_create(client, criteria={'academic_year': YEAR}, count=2)
        assert made.status_code == 201, made.text
        assert (made.json()['source_agency'], made.json()['payment_excluded_count']) == ('MIXED', 1)
        members = client.get(BASE + f"/batches/{made.json()['public_id']}/members").json()['items']
        assert [(m['source_agency'], m['course_type']) for m in members] == [('CHED', 'degree'), ('TESDA', 'diploma')]
        cur.execute('SELECT m.source_record_id FROM bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id '
                    'WHERE b.public_id=%s ORDER BY m.line_no', (made.json()['public_id'],))
        assert [r[0] for r in cur.fetchall()] == [70003, 70002]
        cur.execute('SELECT criteria FROM bcmd_batch WHERE public_id=%s', (made.json()['public_id'],))
        assert json.loads(cur.fetchone()[0])['record_refs'] == ['CHED:70003:degree', 'TESDA:70002:diploma']
        db.rollback()
        # Explicit selection of the blocked record is rechecked at save.
        assert code(save(client, ['CHED:70001:degree'])) == (422, 'SOURCE_NOT_ELIGIBLE')
    finally:
        cur.execute('DELETE FROM payment_transaction WHERE member_id=70001')
        db.commit()
        cur.close()


def test_tesda_members_run_dqa_and_export_their_own_source(real_stack, unified):
    client, db, _ = real_stack
    made = save(client, ['CHED:70001:degree', 'TESDA:70002:diploma'])
    assert made.status_code == 201, made.text
    batch = made.json()['public_id']
    run = complete_dqa(client, db, batch)
    assert (run['passed'], run['total']) == (2, 2), run
    approve(db)
    summary = new_export(client, batch, file_type='SUMMARY')
    detail = new_export(client, batch)
    assert detail.status_code == 201 and summary.status_code == 201, detail.text + summary.text
    rows = csv_rows(client.get(detail.json()['download_url']))
    source = rows[0].index(next(h for h in rows[0] if 'source' in h.lower() or 'agency' in h.lower()))
    assert [row[source] for row in rows[1:]] == ['CHED', 'TESDA']
    text = '\n'.join(','.join(row) for row in csv_rows(client.get(summary.json()['download_url'])))
    assert 'CHED 1 / TESDA 1' in text
