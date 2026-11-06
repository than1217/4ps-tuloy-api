import pytest

from config import ConfigError, load_settings

BASE = {
    "APP_ENV": "prod",
    "DB_HOST": "127.0.0.1",
    "DB_DATABASE": "d",
    "DB_USER": "runtime",
    "DB_PASSWORD": "p",
    "SECRET_KEY": "k" * 40,
    "CORS_ORIGINS": "",
    "ALLOW_SIGNUP": "false",
    "SSH_HOST": "",
    "TRUSTED_HOSTS": "api.example.test",
    # Any existing file satisfies the path check; TLS itself is not exercised here.
    "DB_SSL_CA": __file__,
    "DB_RW_USER": "",
    "DB_RW_PASSWORD": "",
    "REDIS_URL": "",
    "LOG_LEVEL": "",
}


@pytest.fixture
def env(monkeypatch):
    def apply(**overrides):
        for key, value in {**BASE, **overrides}.items():
            monkeypatch.setenv(key, value)
        return load_settings()
    return apply


@pytest.mark.parametrize("app_env", ["dev", "uat", "staging", "prod"])
def test_direct_database_connection_without_ca(env, app_env):
    settings = env(APP_ENV=app_env, SSH_HOST="", DB_SSL_CA="")
    assert settings.db_ssl_ca is None
    assert not settings.ssh_host


def test_valid_production_settings(env):
    s = env()
    assert s.app_env == "prod" and not s.allow_signup and not s.expose_docs


def test_explicit_bcmd_reuse_uses_existing_runtime_credentials(env):
    settings = env(BCMD_USE_EXISTING_ACCOUNT='true', BCMD_DB_USER='', BCMD_DB_PASSWORD='')
    assert settings.bcmd_user == settings.ods.user
    assert settings.bcmd_password == settings.ods.password
    settings = env(BCMD_USE_EXISTING_ACCOUNT='true', BCMD_DB_USER='', BCMD_DB_PASSWORD='',
                   DB_RW_USER='existing_writer', DB_RW_PASSWORD='synthetic-writer-password')
    assert settings.bcmd_user == 'existing_writer'
    assert settings.bcmd_password == 'synthetic-writer-password'


def test_reviewed_staging_profile_needs_no_new_environment_flags(env, monkeypatch):
    for name in ('BCMD_ENABLED', 'BCMD_USE_EXISTING_ACCOUNT', 'BCMD_EXPECT_DATABASE', 'BCMD_EXPECT_HOSTNAME'):
        monkeypatch.delenv(name, raising=False)
    settings = env(APP_ENV='staging', DB_DATABASE='dswd_4ps_tuloy_dev')
    assert settings.bcmd_enabled and settings.bcmd_use_existing_account
    assert settings.bcmd_user == settings.ods.user
    assert settings.bcmd_password == settings.ods.password
    assert settings.bcmd_expected_database == 'dswd_4ps_tuloy_dev'
    # Either node of the staging multi-master cluster (behind one load-balanced address).
    assert settings.bcmd_expected_hostname == 'ppisv2-v3Master,ppisv2-v3slaveG13'
    assert settings.bcmd_hostnames == ('ppisv2-v3Master', 'ppisv2-v3slaveG13')


def test_explicit_hostname_list_is_trimmed_and_overrides_the_profile(env):
    settings = env(APP_ENV='staging', DB_DATABASE='dswd_4ps_tuloy_dev', BCMD_EXPECT_HOSTNAME=' node-a , node-b ,')
    assert settings.bcmd_hostnames == ('node-a', 'node-b')
    settings = env(APP_ENV='staging', DB_DATABASE='dswd_4ps_tuloy_dev', BCMD_EXPECT_HOSTNAME='ppisv2-v3Master')
    assert settings.bcmd_hostnames == ('ppisv2-v3Master',)


def test_staging_profile_preserves_explicit_feature_off_rollback(env, monkeypatch):
    monkeypatch.delenv('BCMD_USE_EXISTING_ACCOUNT', raising=False)
    settings = env(APP_ENV='staging', DB_DATABASE='dswd_4ps_tuloy_dev', BCMD_ENABLED='false')
    assert not settings.bcmd_enabled
    assert settings.bcmd_use_existing_account


@pytest.mark.parametrize('app_env,database', [('local','dswd_4ps_tuloy_dev'), ('staging','other_db')])
def test_profile_does_not_auto_enable_local_or_other_databases(env, monkeypatch, app_env, database):
    monkeypatch.delenv('BCMD_ENABLED', raising=False)
    assert not env(APP_ENV=app_env, DB_DATABASE=database).bcmd_enabled


def test_secrets_not_in_repr(env):
    s = env(DB_PASSWORD="db-pass-should-hide", SECRET_KEY="z" * 40)
    text = repr(s)
    assert "db-pass-should-hide" not in text and "z" * 40 not in text


@pytest.mark.parametrize("overrides, message", [
    ({"DB_HOST": ""}, "DB_HOST"),
    ({"DB_PASSWORD": ""}, "DB_PASSWORD"),
    ({"DB_PORT": "abc"}, "DB_PORT"),
    ({"APP_ENV": "production"}, "APP_ENV"),
    ({"SECRET_KEY": ""}, "SECRET_KEY"),
    ({"SECRET_KEY": "your-super-secret-key-for-jwt"}, "SECRET_KEY"),
    ({"SECRET_KEY": "short"}, "SECRET_KEY"),
    ({"CORS_ORIGINS": "*"}, "CORS_ORIGINS"),
    ({"ALLOW_SIGNUP": "true"}, "ALLOW_SIGNUP"),
    ({"ALLOW_SIGNUP": "maybe"}, "ALLOW_SIGNUP"),
    ({"SSH_HOST": "203.0.113.10", "SSH_USER": "", "SSH_PASSWORD": ""}, "SSH_USER"),
    ({"SSH_HOST": "203.0.113.10", "SSH_USER": "u", "SSH_PASSWORD": "", "SSH_PKEY": ""}, "SSH_PASSWORD"),
    ({"SSH_HOST": "203.0.113.10", "SSH_USER": "u", "SSH_PASSWORD": "", "SSH_PKEY": "C:/no/such/key"}, "SSH_PKEY"),
    ({"MIGRATION_DB_USER": "runtime"}, "MIGRATION_DB_USER"),
    ({"APP_ENV": ""}, "APP_ENV"),
    ({"TRUSTED_HOSTS": ""}, "TRUSTED_HOSTS"),
    ({"TRUSTED_HOSTS": "*"}, "TRUSTED_HOSTS"),
    ({"DB_SSL_CA": "C:/no/such/ca.pem"}, "DB_SSL_CA"),
    ({"REDIS_URL": "redis://cache:6379"}, "REDIS_URL"),
    ({"DB_RW_USER": "writer"}, "DB_RW_PASSWORD"),
    ({"LOG_LEVEL": "loud"}, "LOG_LEVEL"),
])
def test_invalid_settings_fail_fast(env, overrides, message):
    with pytest.raises(ConfigError, match=message):
        env(**overrides)


def test_local_without_secret_uses_ephemeral_key(env):
    s = env(APP_ENV="local", SECRET_KEY="")
    assert s.secret_key_ephemeral and len(s.secret_key) >= 32


def test_known_hosts_required_with_ssh(env, tmp_path):
    with pytest.raises(ConfigError, match="SSH_KNOWN_HOSTS"):
        env(SSH_HOST="203.0.113.10", SSH_USER="u", SSH_PASSWORD="p", SSH_KNOWN_HOSTS=str(tmp_path / "missing"))


def test_separate_rw_account(env):
    s = env(DB_RW_USER="writer", DB_RW_PASSWORD="w")
    assert s.rw_user == "writer" and s.rw_password == "w" and s.ods.user == "runtime"


def test_rw_defaults_to_runtime_account(env):
    s = env()
    assert s.rw_user == "runtime" and s.rw_password == "p"


def test_local_allows_wildcard_hosts_and_plain_redis(env):
    s = env(APP_ENV="local", TRUSTED_HOSTS="*", DB_SSL_CA="", REDIS_URL="redis://127.0.0.1:6379")
    assert s.trusted_hosts == ("*",)


def test_ssl_identity_check_on_by_default_and_can_be_turned_off(env):
    assert env().db_ssl_verify_identity is True
    assert env(DB_SSL_VERIFY_IDENTITY="false").db_ssl_verify_identity is False


@pytest.mark.parametrize('issuer', ['http://127.0.0.1:8080/realms/test', 'https://id.example.invalid/realms/test/', 'https://user:secret@id.example.invalid', 'https://id.example.invalid?x=1'])
def test_keycloak_unsafe_production_issuer_rejected(env, issuer):
    with pytest.raises(ConfigError, match='KEYCLOAK_ISSUER'):
        env(STAFF_AUTH_MODE='keycloak', KEYCLOAK_ISSUER=issuer, KEYCLOAK_API_AUDIENCE='synthetic')


def test_keycloak_production_requires_explicit_audience(env):
    with pytest.raises(ConfigError, match='KEYCLOAK_API_AUDIENCE'):
        env(STAFF_AUTH_MODE='keycloak', KEYCLOAK_ISSUER='https://id.example.invalid/realms/test', KEYCLOAK_API_AUDIENCE='')
    settings = env(STAFF_AUTH_MODE='keycloak', KEYCLOAK_ISSUER='https://id.example.invalid/realms/test', KEYCLOAK_API_AUDIENCE='synthetic')
    assert settings.keycloak_api_audience == 'synthetic'

@pytest.mark.parametrize('environment', ['local', 'staging'])
def test_full_staff_access_can_be_enabled_only_for_local_staging_sso(env, environment):
    s = env(APP_ENV=environment, STAFF_AUTH_MODE='keycloak', STAFF_FULL_ACCESS_ENABLED='true',
            KEYCLOAK_ISSUER='https://id.example.invalid/realms/test', KEYCLOAK_API_AUDIENCE='synthetic')
    assert s.staff_full_access_enabled and s.staff_full_access


@pytest.mark.parametrize('environment', ['prod', 'dev', 'uat'])
def test_full_staff_access_refused_outside_local_staging(env, environment):
    with pytest.raises(ConfigError, match='STAFF_FULL_ACCESS_ENABLED'):
        env(APP_ENV=environment, STAFF_AUTH_MODE='keycloak', STAFF_FULL_ACCESS_ENABLED='true',
            KEYCLOAK_ISSUER='https://id.example.invalid/realms/test', KEYCLOAK_API_AUDIENCE='synthetic')


def test_full_staff_access_defaults_off_and_requires_keycloak(env):
    assert env().staff_full_access_enabled is False
    assert env().staff_full_access is False
    with pytest.raises(ConfigError, match='STAFF_FULL_ACCESS_ENABLED'):
        env(APP_ENV='local', STAFF_AUTH_MODE='legacy', STAFF_FULL_ACCESS_ENABLED='true')
    with pytest.raises(ConfigError, match='STAFF_FULL_ACCESS_ENABLED'):
        env(STAFF_FULL_ACCESS_ENABLED='invalid')
