from uuid import uuid4

import pytest
from pydantic import ValidationError

from conftest import bearer
from errors import ApiError
from routers.cor_review import Decision, parse_ref, snapshot
from scopes import LEGACY_CHED_READ

BASE = '/v1/api/tuloy/staff/cor/enrollments'
ROW = {'id': 7, 'tuloy_id': 'SYNTHETIC-7', 'member_id': 7,
       'details': {'program_name': 'Synthetic course', 'enrollment_status': 'ENROLLED', 'unreviewed_secret': 'omit'}}


@pytest.mark.parametrize('ref', ['CHED:1:diploma','TESDA:1:degree','CHED:0:degree','CHED:1:degree/../','evil:1:degree'])
def test_bad_references(ref):
    with pytest.raises(ApiError):
        parse_ref(ref)


def test_hash_covers_source_and_drops_unknown_fields():
    item = snapshot(ROW, 'CHED', 'degree')
    assert 'unreviewed_secret' not in item['details']
    assert snapshot({**ROW, 'details': {**ROW['details'], 'program_name': 'Changed'}}, 'CHED', 'degree')['source_sha256'] != item['source_sha256']


@pytest.mark.parametrize('path,method', [(BASE,'GET'),(BASE+'/CHED:7:degree','GET'),(BASE+'/CHED:7:degree/decisions','POST')])
def test_requires_authentication(client, path, method):
    assert client.request(method,path).status_code == 401


@pytest.mark.parametrize('agency', ['CHED','TESDA'])
def test_partner_cannot_review(client, fake_db, agency):
    fake_db.scopes[4] = [(LEGACY_CHED_READ, agency, agency, 'active')]
    assert client.get(BASE,headers=bearer('no-grants')).status_code == 403
    assert not fake_db.queries('cor_review')


def setup(fake_db):
    fake_db.scopes[4] = [(LEGACY_CHED_READ, 'DSWD', 'DSWD', 'active')]
    fake_db.on(r'SELECT s.id,s.tuloy_id', lambda sql, params: [ROW])
    fake_db.on(r'SELECT revision,decision,source_sha256 FROM cor_review', lambda sql, params: [{'revision':0,'decision':None,'source_sha256':None}])
    return bearer('no-grants')


def payload(**changes):
    return {'decision':'APPROVED','reason':'Synthetic COR checked','expected_revision':0,
            'source_sha256':snapshot(ROW,'CHED','degree')['source_sha256'],'request_key':str(uuid4()), **changes}


def test_decision_writes_only_review_and_audit(client, fake_db):
    headers = setup(fake_db)
    response = client.post(BASE+'/CHED:7:degree/decisions', headers=headers,json=payload())
    assert response.status_code == 200, response.text
    assert response.json() == {'revision':1,'decision':'APPROVED'}
    assert len(fake_db.queries('INSERT INTO cor_review_event')) == 1
    writes = [sql for sql,_ in fake_db.executed if sql.startswith(('INSERT','UPDATE','DELETE'))]
    assert len(writes) == 3 and all('cor_review' in sql for sql in writes)
    assert not any('payment' in sql.lower() for sql in writes)


@pytest.mark.parametrize('changes',[{'expected_revision':1},{'source_sha256':'0'*64}])
def test_changed_source_or_revision_cannot_save(client,fake_db,changes):
    headers = setup(fake_db)
    response = client.post(BASE+'/CHED:7:degree/decisions',headers=headers,json=payload(**changes))
    assert response.status_code == 409
    assert not fake_db.queries('INSERT INTO cor_review_event')


def test_list_is_bounded_and_does_not_write(client,fake_db):
    response = client.get(BASE,headers=setup(fake_db))
    assert response.status_code == 200
    assert response.json()['items'][0]['reference'] == 'CHED:7:degree'
    assert not fake_db.queries('INSERT')
    assert client.get(BASE+'?limit=1000',headers=bearer('no-grants')).status_code == 422


def test_reason_and_unknown_fields_are_validated():
    for changes in ({'reason':'  '},{'decision':'PAID'},{'payroll_status':'PAID'}):
        with pytest.raises(ValidationError):
            Decision(**payload(**changes))


def test_search_is_parameterized_and_literal(client,fake_db):
    response=client.get(BASE,params={'search':'SYN_%'},headers=setup(fake_db))
    assert response.status_code == 200
    sql,params=next((sql,params) for sql,params in fake_db.executed if sql.startswith('SELECT s.id'))
    assert 'SYN_' not in sql and params[:2] == ('%SYN!_!%%','%SYN!_!%%')
