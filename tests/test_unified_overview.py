"""Bounded unified overview: aggregates, recent ordering, filtering and privacy."""
import pytest
from test_staff_web_contract import bearer, STAFF
from mysql.connector import Error

COUNTS = {'total_records': 4, 'ched_records': 1, 'tesda_records': 3, 'distinct_tuloy_ids': 3, 'ched_beneficiaries': 1, 'tesda_beneficiaries': 2}


def seed(fake_db):
    fake_db.on(r'SELECT COUNT\(\*\) AS total_records', lambda sql, p: [COUNTS])
    fake_db.on(r'SELECT `agency_name`.*FROM vw_unified_matched_enrollment', lambda sql, p: [{
        'agency_name': 'TESDA', 'tuloy_id': 'SYN-1', 'member_id': 1,
        'enrollment_details': '{"diploma":{"program_name":"Synthetic","mm_first_name":"private"}}',
        'created_at': None, 'updated_at': None}])


def test_unified_overview_bounded_global_counts_and_recent_filters(client, fake_db):
    seed(fake_db)
    r = client.get(f'{STAFF}/overview/unified?agency=TESDA&course_type=diploma&search=SYN', headers=bearer())
    assert r.status_code == 200
    body = r.json()
    assert body['counts'] == COUNTS and body['recent_limit'] == 10
    assert body['recent_matched'][0]['enrollment_details'] == {'diploma': {'program_name': 'Synthetic'}}
    queries = fake_db.queries('FROM vw_unified_matched_enrollment')
    assert len(queries) == 2
    assert 'WHERE' not in queries[0][0]
    sql, params = queries[1]
    assert sql.index('WHERE') < sql.index('ORDER BY created_at DESC,updated_at DESC')
    assert params[:2] == ('TESDA', '$.diploma') and params[-2:] == (11, 0)
    assert not fake_db.queries('vw_get_matched_CHED')


@pytest.mark.parametrize('query', ['agency=TEST', 'course_type=unknown', 'offset=-1', 'offset=10000001', 'search=' + 'x'*101])
def test_unified_overview_rejects_invalid_filters(client, fake_db, query):
    assert client.get(f'{STAFF}/overview/unified?{query}', headers=bearer()).status_code == 422
    assert not fake_db.queries('vw_unified_matched_enrollment')


def test_unified_overview_requires_staff_access(client, fake_db):
    assert client.get(f'{STAFF}/overview/unified').status_code == 401
    assert client.get(f'{STAFF}/overview/unified', headers=bearer('no-grants')).status_code == 403
    assert not fake_db.queries('vw_unified_matched_enrollment')


def test_unified_overview_database_errors_safe(client, fake_db):
    fake_db.fail(Error(msg='private table detail', errno=3024), 'vw_unified_matched_enrollment')
    response = client.get(f'{STAFF}/overview/unified', headers=bearer())
    assert response.status_code == 503 and 'private table' not in response.text


def test_unified_overview_paging_never_returns_extra_probe_row(client, fake_db):
    fake_db.on(r'SELECT COUNT\(\*\) AS total_records', lambda sql, p: [COUNTS])
    fake_db.on(r'SELECT `agency_name`.*FROM vw_unified_matched_enrollment', lambda sql, p: [
        {'agency_name': 'CHED', 'tuloy_id': f'SYN-{i}', 'member_id': i, 'enrollment_details': {'degree': {}}}
        for i in range(11)])
    r = client.get(f'{STAFF}/overview/unified?offset=20', headers=bearer())
    assert r.status_code == 200
    assert len(r.json()['recent_matched']) == 10 and r.json()['recent_has_more'] is True
    assert r.json()['recent_offset'] == 20
    assert fake_db.queries('ORDER BY created_at DESC')[0][1] == (11, 20)
