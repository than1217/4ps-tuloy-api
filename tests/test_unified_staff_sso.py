"""The new staff alias retains the existing verified SSO authorization path."""
from test_keycloak import full_staff_access, keycloak, keys, headers, token


def test_sso_full_staff_can_read_unified_alias(client, full_staff_access, keys, fake_db):
    fake_db.on(r'FROM vw_unified_matched_enrollment', lambda sql, p: [{
        'agency_name': 'TESDA', 'tuloy_id': 'SYNTHETIC', 'member_id': 1001,
        'enrollment_details': '{"short_course":{"program_name":"Synthetic course"}}',
        'created_at': None, 'updated_at': None}])
    response = client.get('/v1/api/tuloy/staff/unified-matched-beneficiaries?limit=50&offset=0',
                          headers=headers(token(keys[0])))
    assert response.status_code == 200
    assert response.json()['data'][0]['agency_name'] == 'TESDA'
    assert fake_db.queries('FROM vw_unified_matched_enrollment')[0][1] == (51, 0)


def test_sso_full_staff_can_read_unified_overview(client, full_staff_access, keys, fake_db):
    from test_unified_overview import seed
    seed(fake_db)
    response = client.get('/v1/api/tuloy/staff/overview/unified?agency=TESDA&offset=10', headers=headers(token(keys[0])))
    assert response.status_code == 200
    assert response.json()['counts']['tesda_records'] == 3
    assert response.json()['recent_offset'] == 10
