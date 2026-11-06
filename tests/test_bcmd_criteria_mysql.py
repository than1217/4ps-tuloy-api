"""Local synthetic MySQL: criteria preview -> frozen batch -> NAPA -> payroll estimate."""
from uuid import uuid4
import json
import pytest
from test_bcmd_mysql import mysql_candidate, real_stack, BASE, complete_dqa
from test_bcmd_export_mysql import approve, new_export


def criteria_create(client, *, key=None, criteria=None, count=2, category='TK', name=None):
    key = key or str(uuid4())
    return client.post(BASE+'/batches', headers={'Idempotency-Key': key},
        json={'name': name or f'Synthetic criteria batch {key}', 'category': category, 'criteria': criteria or {},
              'record_count': count})


def test_filtered_preview_counts_and_paging(real_stack):
    client, db, app = real_stack
    first = client.get(BASE+'/candidates?limit=1&academic_year=2026-2027').json()
    assert first['total_count'] == 4 and len(first['items']) == 1 and first['has_more'] is True
    assert 'total_count_capped' not in first and first['total_status'] == 'EXACT' and first['category'] == 'TK'
    assert (first['payment_excluded_count'], first['short_course_excluded_count']) == (0, 0)
    assert [r['key'] for r in first['rules']] == ['source', 'enrollment_status', 'year_level', 'semester', 'beneficiary',
                                                  'age', 'payment_history']
    assert first['items'][0]['enrollment_status'] == 'ENROLLED' and first['items'][0]['year_level'] == '1'
    second = client.get(BASE+'/candidates?limit=1&offset=1&academic_year=2026-2027').json()
    assert first['items'][0]['source_record_id'] != second['items'][0]['source_record_id']
    for query in ['academic_year=1900-1901', 'program_name=missing', 'region_name=Missing', 'search=%27%20OR%201=1--', 'search=SYN_']:
        body = client.get(BASE+'/candidates?'+query).json()
        assert body['total_count'] == 0 and body['items'] == []
    assert client.get(BASE+'/candidates?search=SYN-2').json()['total_count'] == 1
    assert client.get(BASE+'/candidates?region_name=Synthetic%20Region').json()['total_count'] == 4



def test_candidate_count_is_exact_above_5000_and_pages_go_past_it(real_stack):
    """No 5,000 cap: total_count is exact, and offsets beyond 10,000 still page the stable order."""
    client, db, app = real_stack
    cur = db.cursor()
    cur.execute('SET SESSION cte_max_recursion_depth=20000')
    year, first, last = '2031-2032', 100001, 110002
    numbers = f'WITH RECURSIVE s(n) AS (SELECT {first} UNION ALL SELECT n+1 FROM s WHERE n<{last}) '
    try:
        cur.execute('INSERT INTO tbl_person ' + numbers + "SELECT n,'Synthetic','Cap','Student','2001-02-03',1,NULL,0 FROM s")
        cur.execute('INSERT INTO enroll_matched_CHED_tbl ' + numbers +
                    f"SELECT n,CONCAT('SYN-CAP-',n),n,1,'{year}',1,'1','ENROLLED','Synthetic course' FROM s")
        db.commit()
        body = client.get(BASE+f'/candidates?limit=200&academic_year={year}').json()
        assert (body['total_count'], body['total_status'], len(body['items']), body['has_more']) == (10002, 'EXACT', 200, True)
        deep = client.get(BASE+f'/candidates?limit=200&offset=10000&academic_year={year}').json()
        assert [r['source_record_id'] for r in deep['items']] == [110001, 110002] and deep['has_more'] is False
        assert deep['items'][0]['source_ref'] == 'CHED:110001:degree'
        past = client.get(BASE+f'/candidates?limit=200&offset=20000&academic_year={year}').json()
        assert past['items'] == [] and past['total_count'] == 10002
    finally:
        cur.execute('DELETE FROM enroll_matched_CHED_tbl WHERE id>100000 AND id<=110002')
        cur.execute('DELETE FROM tbl_person WHERE person_id>100000 AND person_id<=110002')
        db.commit(); cur.close()

def test_batch_size_has_no_fixed_cap_only_the_eligible_count(real_stack):
    """250 records (above the old 200 cap) save, run DQA and export; one more than match saves nothing."""
    client, db, app = real_stack
    cur = db.cursor()
    year, first, last = '2033-2034', 200001, 200250
    numbers = f'WITH RECURSIVE s(n) AS (SELECT {first} UNION ALL SELECT n+1 FROM s WHERE n<{last}) '
    has_household = ' FROM s WHERE MOD(n,10)<>0'  # every tenth record has no household: issues and DQA failures
    try:
        cur.execute('SET SESSION cte_max_recursion_depth=10000')
        cur.execute('INSERT INTO tbl_person ' + numbers + "SELECT n,'Synthetic','Example','Student','2001-02-03',1,2,0 FROM s")
        cur.execute('INSERT INTO enroll_matched_CHED_tbl ' + numbers +
                    f"SELECT n,CONCAT('SYN-BIG-',n),n,1,'{year}',1,'1','ENROLLED','Synthetic course' FROM s")
        cur.execute('INSERT INTO tbl_hh_roster ' + numbers + 'SELECT n,n,n+1000000' + has_household)
        cur.execute('INSERT INTO tbl_household ' + numbers + 'SELECT n+1000000,1' + has_household)
        cur.execute('INSERT INTO tbl_family_selected ' + numbers + "SELECT n+1000000,CONCAT('SYN-BIG-FAM-',n)" + has_household)
        cur.execute('INSERT INTO tbl_family_assoc_ops ' + numbers + "SELECT n,CONCAT('SYN-BIG-FAM-',n),n,n,0" + has_household)
        db.commit()
        cur.execute('SELECT COUNT(*) FROM bcmd_batch'); before = cur.fetchone()[0]; db.rollback()
        too_many = criteria_create(client, criteria={'academic_year': year}, count=251)
        assert too_many.status_code == 422 and too_many.json()['error_code'] == 'RECORD_COUNT_EXCEEDS_ELIGIBLE'
        assert too_many.json()['detail'] == 'Only 250 eligible records match; 251 were requested'
        cur.execute('SELECT COUNT(*) FROM bcmd_batch'); assert cur.fetchone()[0] == before; db.rollback()

        made = criteria_create(client, criteria={'academic_year': year}, count=250)
        assert made.status_code == 201, made.text
        batch = made.json()
        assert batch['member_count'] == 250 and batch['dqa_block_count'] == 25
        cur.execute('SELECT m.line_no,m.source_record_id,COUNT(i.id) FROM bcmd_batch b JOIN bcmd_batch_member m ON m.batch_id=b.id '
                    'LEFT JOIN bcmd_dqa_issue i ON i.member_id=m.id WHERE b.public_id=%s GROUP BY m.id ORDER BY m.line_no',
                    (batch['public_id'],))
        rows = cur.fetchall(); db.rollback()
        assert [r[0] for r in rows] == list(range(1, 251)) and [r[1] for r in rows] == list(range(first, last + 1))
        assert all((issues > 0) == (source % 10 == 0) for _, source, issues in rows)  # issues sit on their own member

        approve(db)
        run = complete_dqa(client, db, batch['public_id'])
        assert (run['total'], run['passed'], run['failed']) == (250, 225, 25)
        detail = new_export(client, batch['public_id'])
        assert detail.status_code == 201 and detail.json()['row_count'] == 225, detail.text
        assert client.get(detail.json()['download_url']).status_code == 200
        failed = new_export(client, batch['public_id'], file_type='FAILED')
        assert failed.status_code == 201 and failed.json()['row_count'] == 25, failed.text
        preview = client.get(f"{BASE}/batches/{batch['public_id']}/payroll-preview")
        assert preview.status_code == 200 and preview.json()['member_count'] == 225, preview.text
        assert client.get(BASE+'/status').json()['max_records'] is None
    finally:
        db.rollback()
        for table, column, offset in [('tbl_family_assoc_ops', 'fam_assoc_id', 0), ('tbl_family_selected', 'household_id', 1000000),
                                      ('tbl_household', 'household_id', 1000000), ('tbl_hh_roster', 'hh_roster_id', 0),
                                      ('enroll_matched_CHED_tbl', 'id', 0), ('tbl_person', 'person_id', 0)]:
            cur.execute(f'DELETE FROM {table} WHERE {column} BETWEEN %s AND %s', (first + offset, last + offset))
        db.commit(); cur.close()


def test_criteria_retry_reuses_snapshot_after_source_changes(real_stack):
    client, db, app = real_stack
    key = str(uuid4())
    first = criteria_create(client, key=key)
    assert first.status_code == 201, first.text
    cur = db.cursor(dictionary=True)
    cur.execute("UPDATE enroll_matched_CHED_tbl SET enrollment_status='GRADUATED' WHERE id=1")
    db.commit()
    try:
        again = criteria_create(client, key=key)
        assert again.status_code == 201 and again.json() == first.json()
        conflict = criteria_create(client, key=key, count=3)
        assert conflict.status_code == 409
        cur.execute('SELECT criteria FROM bcmd_batch WHERE public_id=%s', (first.json()['public_id'],))
        stored = json.loads(cur.fetchone()['criteria'])
        assert stored['record_ids'] == [1, 2] and stored['record_count'] == 2
    finally:
        cur.execute("UPDATE enroll_matched_CHED_tbl SET enrollment_status='ENROLLED' WHERE id=1")
        db.commit(); cur.close()


def test_not_enough_candidates_writes_nothing(real_stack):
    client, db, app = real_stack
    cur = db.cursor();cur.execute('SELECT COUNT(*) FROM bcmd_batch'); before=cur.fetchone()[0];db.rollback()
    response = criteria_create(client, count=5)
    assert response.status_code == 422 and response.json()['error_code'] == 'RECORD_COUNT_EXCEEDS_ELIGIBLE'
    assert response.json()['detail'] == 'Only 4 eligible records match; 5 were requested'
    cur.execute('SELECT COUNT(*) FROM bcmd_batch');assert cur.fetchone()[0] == before;db.rollback();cur.close()


@pytest.mark.parametrize('change', [{'record_count': 0}, {'record_count': True}, {'record_ids': [1]}, {'criteria': {'agency': 'TESDA'}}, {'criteria': None}])
def test_invalid_criteria_refused_before_writes(real_stack, change):
    client, db, app = real_stack
    response=client.post(BASE+'/batches',headers={'Idempotency-Key':str(uuid4())},json={'name':'Synthetic','category':'TK','criteria':{},'record_count':1,**change})
    assert response.status_code == 422


def test_criteria_to_napa_to_readonly_payroll_preview(real_stack):
    client, db, app = real_stack
    response = criteria_create(client)
    assert response.status_code == 201, response.text
    batch = response.json()['public_id']
    approve(db)
    preview = client.get(f'{BASE}/batches/{batch}/payroll-preview')
    assert preview.status_code == 422 and preview.json()['error_code'] == 'NAPA_DQA_REQUIRED'
    complete_dqa(client, db, batch)
    exported = new_export(client, batch)
    assert exported.status_code == 201, exported.text
    csv = client.get(exported.json()['download_url'])
    assert csv.status_code == 200 and 'SYN-1' not in csv.text  # CSV follows approved NAPA columns, not source IDs.
    cur = db.cursor();cur.execute('SELECT COUNT(*) FROM bcmd_audit_event');events=cur.fetchone()[0];db.rollback()
    preview = client.get(f'{BASE}/batches/{batch}/payroll-preview?months=2')
    assert preview.status_code == 200, preview.text
    assert preview.json()['preview_only'] is True and preview.json()['total_amount'] == '20000.00'
    assert preview.json()['member_count'] == 2 and preview.json()['payable_count'] == 2
    assert preview.json()['source'] == 'NAPA passed records' and preview.json()['dqa_run_no'] == 1
    cur.execute('SELECT COUNT(*) FROM bcmd_audit_event');assert cur.fetchone()[0] == events
    cur.execute('SELECT COUNT(*) FROM payment_transaction')
    assert cur.fetchone()[0] == 0  # Payment history is read for eligibility, never written by BCMD.
    db.rollback();cur.close()


def test_batch_payroll_cap_and_blocked_states(real_stack):
    client, db, app = real_stack
    response=criteria_create(client,count=4)
    assert response.status_code == 201
    batch=response.json()['public_id']
    cur=db.cursor()
    cur.execute('UPDATE bcmd_batch_member m JOIN bcmd_batch b ON b.id=m.batch_id SET m.hhid=%s WHERE b.public_id=%s',('SYN-HOUSEHOLD',batch))
    db.commit()
    complete_dqa(client, db, batch)
    preview=client.get(f'{BASE}/batches/{batch}/payroll-preview').json()
    assert preview['payable_count']==3 and preview['capped_count']==1 and preview['total_amount']=='15000.00'
    cur.execute('UPDATE tbl_person SET ip_group_id=999 WHERE person_id=4');db.commit()
    try:
        blocked=criteria_create(client,criteria={'search':'SYN-4'},count=1)
        assert blocked.status_code==201 and blocked.json()['dqa_outcome']=='BLOCKED'
        assert client.get(f"{BASE}/batches/{blocked.json()['public_id']}/payroll-preview").status_code==422
    finally:
        cur.execute('UPDATE tbl_person SET ip_group_id=0 WHERE person_id=4');db.commit();cur.close()


def add_sources(db, rows):
    """Synthetic persons and CHED rows: (id, status, year_level, semester, date_of_birth)."""
    cur = db.cursor()
    for number, status, year, semester, dob in rows:
        cur.execute("INSERT INTO tbl_person VALUES (%s,'Synthetic','Example','Student',%s,2,NULL,0)", (number, dob))
        cur.execute('INSERT INTO tbl_hh_roster VALUES (%s,%s,%s)', (number, number, number+100))
        cur.execute('INSERT INTO tbl_household VALUES (%s,1)', (number+100,))
        cur.execute("INSERT INTO enroll_matched_CHED_tbl VALUES (%s,%s,%s,1,'2026-2027',%s,%s,%s,'Synthetic course')",
                    (number, f'SYN-{number}', number, semester, year, status))
    db.commit(); cur.close()


def remove_sources(db, numbers):
    cur = db.cursor()
    marks = ','.join(['%s'] * len(numbers))
    for table, column in [('enroll_matched_CHED_tbl', 'id'), ('tbl_hh_roster', 'hh_roster_id'),
                          ('tbl_household', 'household_id'), ('tbl_person', 'person_id')]:
        values = [n + 100 for n in numbers] if table == 'tbl_household' else numbers
        cur.execute(f'DELETE FROM {table} WHERE {column} IN ({marks})', values)
    db.commit(); cur.close()


def test_aral_and_trabaho_categories_apply_their_fixed_rules(real_stack):
    client, db, app = real_stack
    add_sources(db, [(60, 'ENROLLED', '2', 2, '2003-05-06'), (61, 'ENROLLED', '3', 1, '2004-05-06'),
                     (62, 'GRADUATED', '4', None, '2000-05-06')])
    try:
        ta = client.get(BASE+'/candidates?category=TA').json()
        # TA keeps every enrolled year level and semester, including the TK records.
        assert {r['source_record_id'] for r in ta['items']} == {1, 2, 3, 4, 60, 61}
        assert [r['key'] for r in ta['rules']] == ['source', 'enrollment_status', 'beneficiary', 'age', 'payment_history']
        narrowed = client.get(BASE+'/candidates?category=TA&semester=2&year_level=2').json()
        assert [r['source_record_id'] for r in narrowed['items']] == [60]
        tt = client.get(BASE+'/candidates?category=TT').json()
        assert [r['source_record_id'] for r in tt['items']] == [62] and tt['items'][0]['enrollment_status'] == 'GRADUATED'
        for query in ['category=TK&semester=1', 'category=TT&year_level=1', 'category=XX', 'academic_year=2026-2028',
                      'academic_year=2026/2027', 'search=%01']:
            assert client.get(BASE+'/candidates?'+query).status_code == 422, query
        aral = criteria_create(client, category='TA', criteria={'semester': 2}, count=1)
        assert aral.status_code == 201, aral.text
        assert aral.json()['category'] == 'TA' and aral.json()['member_count'] == 1
        trabaho = client.post(BASE+'/batches', headers={'Idempotency-Key': str(uuid4())},
                              json={'name': f'TLY-TRBH-{uuid4()}', 'category': 'TT', 'record_ids': [62]})
        assert trabaho.status_code == 201, trabaho.text
        run = complete_dqa(client, db, trabaho.json()['public_id'])
        assert run['passed'] == 1  # Graduates need no semester.
        wrong = client.post(BASE+'/batches', headers={'Idempotency-Key': str(uuid4())},
                            json={'name': f'Synthetic {uuid4()}', 'category': 'TT', 'record_ids': [60]})
        assert wrong.status_code == 422 and wrong.json()['error_code'] == 'SOURCE_NOT_ELIGIBLE'
        assert criteria_create(client, category='TK', criteria={'semester': 1}).status_code == 422
    finally:
        remove_sources(db, [60, 61, 62])


def test_age_18_to_30_applies_at_selection(real_stack):
    from bcmd_candidates import manila_today, years_before
    from datetime import timedelta
    client, db, app = real_stack
    today = manila_today()
    add_sources(db, [(70, 'ENROLLED', '1', 1, years_before(today, 18)),               # 18 today: included
                     (71, 'ENROLLED', '1', 1, years_before(today, 18) + timedelta(1)),  # 17: excluded
                     (72, 'ENROLLED', '1', 1, years_before(today, 31) + timedelta(1)),  # 30: included
                     (73, 'ENROLLED', '1', 1, years_before(today, 31))])                # 31 today: excluded
    try:
        ids = {r['source_record_id'] for r in client.get(BASE+'/candidates?limit=200').json()['items']}
        assert {70, 72} <= ids and not {71, 73} & ids
        refused = client.post(BASE+'/batches', headers={'Idempotency-Key': str(uuid4())},
                              json={'name': f'Synthetic {uuid4()}', 'category': 'TK', 'record_ids': [71]})
        assert refused.status_code == 422 and refused.json()['error_code'] == 'SOURCE_NOT_ELIGIBLE'
    finally:
        remove_sources(db, [70, 71, 72, 73])


def test_batch_names_are_unique_case_insensitive(real_stack):
    client, db, app = real_stack
    name = f'TLY-KLHYO-{uuid4().hex}'
    key = str(uuid4())
    assert criteria_create(client, key=key, name=name, count=1).status_code == 201
    assert criteria_create(client, key=key, name=name, count=1).status_code == 201  # same-key replay
    clash = criteria_create(client, name=name.lower(), count=1)
    assert clash.status_code == 409 and clash.json()['error_code'] == 'BATCH_NAME_EXISTS'
    assert criteria_create(client, name='Bad\x00name', count=1).status_code == 422
    cur = db.cursor()
    with pytest.raises(Exception):
        cur.execute("UPDATE bcmd_batch SET name=%s WHERE name<>%s LIMIT 1", (name, name))
    db.rollback(); cur.close()


def test_migration_0020_renames_later_duplicates_and_keeps_the_oldest(real_stack):
    from scripts.migrate import discover, statements
    client, db, app = real_stack
    made = [criteria_create(client, count=1) for _ in range(3)]
    assert all(r.status_code == 201 for r in made)
    ids = [r.json()['public_id'] for r in made]
    name = f'Legacy duplicate {uuid4().hex[:8]}'
    cur = db.cursor()
    cur.execute('ALTER TABLE bcmd_batch DROP INDEX uq_bcmd_batch_name')
    try:
        for public_id, variant in zip(ids, [name, name.upper(), name.lower()]):
            cur.execute('UPDATE bcmd_batch SET name=%s WHERE public_id=%s', (variant, public_id))
        db.commit()
        sql = next(text for version, _, _, text in discover() if version == '0020')
        for _ in range(2):  # re-runnable: the second pass finds no duplicates, then fails only on the existing index
            for statement in statements(sql):
                try:
                    cur.execute(statement)
                except Exception as exc:
                    assert 'Duplicate key name' in str(exc)
            db.commit()
    finally:
        cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='bcmd_batch' AND INDEX_NAME='uq_bcmd_batch_name'")
        if not cur.fetchone()[0]:
            cur.execute('ALTER TABLE bcmd_batch ADD UNIQUE KEY uq_bcmd_batch_name (name)')
        db.commit()
    cur.execute('SELECT id,name FROM bcmd_batch WHERE public_id IN (%s,%s,%s) ORDER BY id', ids)
    rows = cur.fetchall()
    assert rows[0][1] == name
    assert [row[1] for row in rows[1:]] == [f'{name.upper()} #{rows[1][0]}', f'{name.lower()} #{rows[2][0]}']
    db.rollback(); cur.close()
