import pytest

from conftest import bearer
from scopes import LEGACY_BENEFICIARY_READ
from security import Principal
from utils import cache_key_builder


def target():
    pass


def test_cache_key_is_scoped_per_user():
    a = cache_key_builder(target, "ns", kwargs={"current_user": Principal(1, "a"), "db": object(), "limit": 5})
    b = cache_key_builder(target, "ns", kwargs={"current_user": Principal(2, "b"), "db": object(), "limit": 5})
    a_again = cache_key_builder(target, "ns", kwargs={"current_user": Principal(1, "a"), "db": object(), "limit": 5})
    other_page = cache_key_builder(target, "ns", kwargs={"current_user": Principal(1, "a"), "limit": 6})
    assert a != b and a == a_again and a != other_page
    assert a.startswith("ns:")


def test_cached_list_not_shared_between_users(client, fake_db):
    calls = []

    def rows(sql, params):
        calls.append(params)
        return [{"tuloy_id": f"synthetic-{len(calls)}"}]

    fake_db.on(r"FROM vw_unmatched_member_bene WHERE `member_id` NOT IN", rows)
    first_a = client.get("/GetEligibleBene?limit=1", headers=bearer("partner-a")).json()
    second_a = client.get("/GetEligibleBene?limit=1", headers=bearer("partner-a")).json()
    first_b = client.get("/GetEligibleBene?limit=1", headers=bearer("partner-b")).json()
    assert first_a == second_a  # served from cache for the same user
    assert first_b["items"] != first_a["items"]  # user b got its own query, not a's cache entry
    assert len(calls) == 2


def test_cache_hit_still_requires_auth(client, fake_db):
    fake_db.on(r"FROM vw_unmatched_member_bene WHERE `member_id` NOT IN", lambda s, p: [{"tuloy_id": "x"}])
    assert client.get("/GetEligibleBene?limit=1", headers=bearer()).status_code == 200
    assert client.get("/GetEligibleBene?limit=1").status_code == 401
    assert client.get("/GetEligibleBene?limit=1", headers=bearer("disabled-user")).status_code == 403


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_cache_hit_rechecks_grant(client, fake_db, state):
    """L07: a grant revoked or expired after the cache is warm denies the next request; no cached rows leak."""
    fake_db.on(r"FROM vw_unmatched_member_bene WHERE `member_id` NOT IN", lambda s, p: [{"tuloy_id": "synthetic-1"}])
    assert client.get("/GetEligibleBene?limit=1", headers=bearer("partner-a")).status_code == 200
    fake_db.scopes[1] = [(LEGACY_BENEFICIARY_READ, "P-TEST", "TEST", state)]
    r = client.get("/GetEligibleBene?limit=1", headers=bearer("partner-a"))
    assert r.status_code == 403 and "synthetic-1" not in r.text
