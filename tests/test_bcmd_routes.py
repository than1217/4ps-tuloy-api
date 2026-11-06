"""Authorization and local-only feature guards without network access."""
from dataclasses import replace
from uuid import uuid4

import pytest

from config import ConfigError, get_settings
from conftest import bearer
from routers.bcmd import validate_local_candidate
from scopes import LEGACY_CHED_READ, KNOWN_SCOPES

BASE = '/v1/api/tuloy/staff/bcmd'
ID = str(uuid4())
RUN = str(uuid4())
OPERATIONS = [('GET', BASE+'/status'), ('GET', BASE+'/candidates'), ('GET', BASE+'/batches'),
              ('POST', BASE+'/batches'), ('GET', BASE+'/batches/'+ID), ('GET', BASE+'/batches/'+ID+'/payroll-preview'),
              *[('GET', BASE+'/batches/'+ID+'/'+part) for part in ('members', 'issues', 'audit', 'export')]]
OPERATIONS += [('GET', BASE+'/batches/'+ID+'/dqa-runs'), ('POST', BASE+'/batches/'+ID+'/dqa-runs'),
               ('GET', BASE+'/batches/'+ID+'/dqa-runs/'+RUN), ('GET', BASE+'/batches/'+ID+'/dqa-runs/'+RUN+'/results')]


@pytest.mark.parametrize('method,path', OPERATIONS)
def test_authentication_is_required_before_batch_access(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize('agency', ['CHED', 'TESDA'])
@pytest.mark.parametrize('method,path', OPERATIONS)
def test_napa_requires_matching_dswd_read_authority(client, fake_db, agency, method, path):
    fake_db.scopes[4] = [(LEGACY_CHED_READ, agency, agency, 'active')]
    response = client.request(method, path, headers=bearer('no-grants'))
    assert response.status_code == 403
    assert not fake_db.queries('bcmd_batch')


def test_disabled_service_reports_unavailable_without_opening_batch_tables(client, fake_db):
    fake_db.scopes[4] = [(LEGACY_CHED_READ, 'DSWD', 'DSWD', 'active')]
    headers = bearer('no-grants')
    assert client.get(BASE+'/status', headers=headers).json()['available'] is False
    response = client.get(BASE+'/batches', headers=headers)
    assert response.status_code == 503
    assert response.json()['error_code'] == 'BCMD_UNAVAILABLE'
    assert not fake_db.queries('bcmd_batch')


@pytest.mark.parametrize('state', ['expired', 'revoked'])
@pytest.mark.parametrize('agency', ['DSWD', 'TEST'])
def test_inactive_grants_do_not_enable_napa(client, fake_db, state, agency):
    fake_db.scopes[4] = [(LEGACY_CHED_READ, agency, agency, state)]
    assert client.get(BASE+'/status', headers=bearer('no-grants')).status_code == 403


def test_unknown_affiliation_is_not_inferred_from_username(client):
    assert client.get(BASE+'/status', headers=bearer('no-grants')).status_code == 403


@pytest.mark.parametrize('scope', sorted(KNOWN_SCOPES))
@pytest.mark.parametrize('agency', ['DSWD', 'TEST'])
def test_all_dswd_and_test_staff_are_admitted_without_a_module_scope(client, fake_db, scope, agency):
    fake_db.scopes[4] = [(scope, agency, agency, 'active')]
    response = client.get(BASE+'/status', headers=bearer('no-grants'))
    assert response.status_code == 200
    # Feature availability is separate from staff access; no real DB is opened.
    assert response.json()['available'] is False


@pytest.mark.parametrize('agency,partner', [('DSWD','TEST'), ('TEST','DSWD'), ('CHED','DSWD')])
def test_mismatched_bindings_are_not_dswd_affiliation(client, fake_db, agency, partner):
    fake_db.scopes[4] = [('tuloy:docs:internal', partner, agency, 'active')]
    assert client.get(BASE+'/status', headers=bearer('no-grants')).status_code == 403


@pytest.mark.parametrize('change', [dict(app_env='staging'), dict(ssh_host='gateway.invalid'),
    dict(ods_host='10.10.10.133'), dict(database='dswd_4ps_tuloy_dev'), dict(bcmd_user=''), dict(bcmd_user='synthetic_user')])
def test_candidate_cannot_target_staging_or_use_shared_write_credentials(change):
    settings = get_settings()
    settings = replace(settings, bcmd_enabled=True, rw_user='synthetic_write',
                       bcmd_use_existing_account=False,
                       bcmd_user='synthetic_bcmd', bcmd_password='synthetic',
                       ods=replace(settings.ods, database='tuloy_napa_test_guard'))
    if 'ods_host' in change:
        settings = replace(settings, ods=replace(settings.ods, host=change['ods_host']))
    elif 'database' in change:
        settings = replace(settings, ods=replace(settings.ods, database=change['database']))
    else:
        settings = replace(settings, **change)
    with pytest.raises(ConfigError):
        validate_local_candidate(settings)
