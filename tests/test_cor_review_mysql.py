"""COR decision persistence and isolation on a disposable loopback schema only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import mysql.connector
import pytest

from database import get_app_connection
from scripts.migrate import statements
from test_bcmd_mysql import mysql_candidate, real_stack

BASE = '/v1/api/tuloy/staff/cor/enrollments'


@pytest.fixture
def cor_stack(real_stack, mysql_candidate):
    client, db, app = real_stack
    cur = db.cursor()
    for sql in statements(Path('migrations/0017_cor_review.sql').read_text(encoding='utf8')):
        cur.execute(sql)
    cur.execute('ALTER TABLE enroll_matched_CHED_tbl ADD degree_date_enrolled date NULL')
    # enroll_matched_TESDA_tbl is part of the shared synthetic schema (tests/test_bcmd_mysql.py).
    cur.execute("INSERT INTO enroll_matched_TESDA_tbl VALUES (501,'SYN-TESDA-501',501,JSON_OBJECT('program_name','Synthetic TESDA diploma','enrollment_status','ENROLLED'),NULL)")
    db.commit(); cur.close()

    def connection():
        separate = mysql.connector.connect(**mysql_candidate)
        try:
            yield separate
        finally:
            separate.rollback(); separate.close()
    app.dependency_overrides[get_app_connection] = connection
    yield client, db, app
    # The module-scoped schema is disposable. Remove only this fixture's additions.
    cur = db.cursor()
    cur.execute('DROP TABLE cor_review_event'); cur.execute('DROP TABLE cor_review')
    cur.execute('DELETE FROM enroll_matched_TESDA_tbl WHERE id=501')
    cur.execute('ALTER TABLE enroll_matched_CHED_tbl DROP COLUMN degree_date_enrolled')
    db.commit(); cur.close()


def request_body(item, **changes):
    return dict(decision='APPROVED',reason='Synthetic COR acceptance test',
                expected_revision=item['review']['revision'],source_sha256=item['source_sha256'],request_key=str(uuid4()), **changes)


def test_persist_retry_audit_and_source_unchanged(cor_stack):
    client, db, app = cor_stack
    item = client.get(BASE).json()['items'][0]
    ref = item['reference']
    body = request_body(item)
    before = client.get(BASE+'/'+ref).json()['details']
    first = client.post(BASE+'/'+ref+'/decisions',json=body)
    assert first.status_code == 200, first.text
    assert client.post(BASE+'/'+ref+'/decisions',json=body).json() == first.json()
    saved = client.get(BASE+'/'+ref).json()
    assert saved['details'] == before
    assert saved['review']['decision'] == 'APPROVED' and len(saved['history']) == 1
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT actor_api_user_id,actor_staff_user_id FROM cor_review_event')
    assert cur.fetchall() == [{'actor_api_user_id':11,'actor_staff_user_id':None}]
    cur.close()


def test_concurrent_decisions_one_wins(cor_stack):
    client, db, app = cor_stack
    item = client.get(BASE).json()['items'][0]
    ref = item['reference']
    bodies = [request_body(item), request_body(item)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda body: client.post(BASE+'/'+ref+'/decisions',json=body), bodies))
    assert sorted(response.status_code for response in responses) == [200,409]
    assert len(client.get(BASE+'/'+ref).json()['history']) == 1


def test_source_change_stales_decision_and_blocks_old_snapshot(cor_stack):
    client, db, app = cor_stack
    item = client.get(BASE).json()['items'][0]
    ref = item['reference']
    assert client.post(BASE+'/'+ref+'/decisions',json=request_body(item)).status_code == 200
    cur = db.cursor()
    cur.execute("UPDATE enroll_matched_CHED_tbl SET degree_program_name='Synthetic changed source' WHERE id=%s", (int(ref.split(':')[1]),))
    db.commit(); cur.close()
    assert client.get(BASE).json()['items'][0]['review']['stale'] is True
    response = client.post(BASE+'/'+ref+'/decisions',json={**request_body(item),'expected_revision':1})
    assert response.status_code == 409


def test_tesda_and_sso_decision(cor_stack):
    from security import Principal, Grant, get_beneficiary_principal
    from scopes import LEGACY_CHED_READ
    client, db, app = cor_stack
    app.dependency_overrides[get_beneficiary_principal] = lambda: Principal(11,'synthetic-sso',frozenset({Grant(LEGACY_CHED_READ,'DSWD','DSWD')}),kind='staff')
    item = client.get(BASE+'?agency=TESDA&course_kind=diploma').json()['items'][0]
    body = request_body(item); body['decision'] = 'DENIED'
    assert client.post(BASE+'/'+item['reference']+'/decisions',json=body).status_code == 200
    cur = db.cursor(dictionary=True)
    cur.execute('SELECT actor_api_user_id,actor_staff_user_id FROM cor_review_event')
    assert cur.fetchall() == [{'actor_api_user_id':None,'actor_staff_user_id':11}]
    cur.close()
