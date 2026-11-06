from conftest import bearer
from test_keycloak import keycloak, keys, token, headers, ISSUER, VERIFY  # noqa: F401

SESSION = "/v1/api/tuloy/staff/session"
ME = "/v1/api/tuloy/staff/me"


def test_registration_creates_staff_identity_without_api_account_or_grants(client, keycloak, keys, fake_db):
    fake_db.staff_users.clear()
    auth = headers(token(keys[0]))
    response = client.post(SESSION, headers=auth)
    assert response.status_code == 200
    assert client.get(ME, headers=auth).status_code == 200
    data = client.get(ME, headers=auth).json()
    assert data["active_grants"] == []
    assert not fake_db.queries("api_users") and not fake_db.queries("api_user_identities")
    assert not fake_db.queries("INSERT INTO staff_user_scopes")
    assert client.post("/VerifyMemberBene", json=VERIFY, headers=auth).status_code == 403


def test_registration_is_idempotent_and_cannot_reenable_disabled_staff(client, keycloak, keys, fake_db):
    auth = headers(token(keys[0]))
    assert client.post(SESSION, headers=auth).status_code == 200
    assert client.post(SESSION, headers=auth).status_code == 200
    assert len(fake_db.staff_users) == 1
    fake_db.staff_users[(ISSUER, "synthetic-subject")]["disabled"] = 1
    assert client.post(SESSION, headers=auth).status_code == 403
    assert client.get(ME, headers=auth).status_code == 403
    assert not fake_db.queries("UPDATE staff_users")


def test_invalid_or_api_token_cannot_register_staff(client, keycloak):
    assert client.post(SESSION).status_code == 401
    assert client.post(SESSION, headers=bearer()).status_code == 401


def test_registration_body_cannot_choose_identity_or_permissions(client, keycloak, keys, fake_db):
    fake_db.staff_users.clear()
    response = client.post(SESSION, headers=headers(token(keys[0])), json={
        "subject": "admin", "disabled": 0, "scopes": ["tuloy:legacy:member:verify"], "role": "ADMIN"})
    assert response.status_code == 200
    assert (ISSUER, "synthetic-subject") in fake_db.staff_users
    assert (ISSUER, "admin") not in fake_db.staff_users
    assert not fake_db.queries("INSERT INTO staff_user_scopes")


def test_api_account_disable_does_not_disable_staff(client, keycloak, keys, fake_db):
    fake_db.users["partner-a"]["disabled"] = 1
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 200


def test_api_grants_cannot_authorize_staff_with_same_numeric_id(client, keycloak, keys, fake_db):
    fake_db.staff_scopes[1] = []
    assert client.get(ME, headers=headers(token(keys[0]))).status_code == 200
    assert client.post("/VerifyMemberBene", json=VERIFY, headers=headers(token(keys[0]))).status_code == 403


def test_staff_cannot_use_service_import_even_with_write_grant(client, keycloak, keys, fake_db):
    from scopes import LEGACY_CHED_WRITE
    fake_db.staff_scopes[1] = [(LEGACY_CHED_WRITE, "TEST")]
    response = client.post('/PostEnrolleeMatchedCHED', headers=headers(token(keys[0])),
                           json={"status": False, "message": "NOT FOUND"})
    assert response.status_code == 401
    assert not fake_db.queries("staff_user_scopes")
    assert not fake_db.queries("INSERT INTO matching_transaction")


def test_concurrent_first_login_uses_existing_staff_record(client, keycloak, keys, fake_db):
    from mysql.connector import Error
    fake_db.staff_users.clear()

    def concurrent_insert(sql, params):
        fake_db.staff_users[params] = {"id": 55, "disabled": 0}
        raise Error(errno=1062, msg="synthetic concurrent duplicate")

    fake_db.on(r"INSERT INTO staff_users", concurrent_insert)
    auth = headers(token(keys[0]))
    assert client.post(SESSION, headers=auth).status_code == 200
    assert fake_db.staff_users[(ISSUER, "synthetic-subject")]["id"] == 55
    assert fake_db.rollbacks == 1


def test_registration_failure_rolls_back_without_leaking_database_details(client, keycloak, keys, fake_db):
    from conftest import db_error
    fake_db.staff_users.clear()
    fake_db.fail(db_error(1142, "sensitive database detail"), match="INSERT INTO staff_users")
    response = client.post(SESSION, headers=headers(token(keys[0])))
    assert response.status_code == 500
    assert "sensitive database detail" not in response.text
    assert fake_db.rollbacks == 1 and fake_db.commits == 0


def test_unknown_and_partner_staff_grants_never_authorize_staff(client, keycloak, keys, fake_db):
    fake_db.staff_scopes[1] = [('tuloy:member:verify', 'TEST'), ('tuloy:made:up', 'TEST')]
    auth = headers(token(keys[0]))
    data = client.get(ME, headers=auth).json()
    assert data['active_grants'] == []
    assert client.post('/VerifyMemberBene', json=VERIFY, headers=auth).status_code == 403
