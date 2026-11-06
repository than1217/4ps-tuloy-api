"""Pure checks for the BCMD selection dates, legacy hashes and CSV cell guard (no database)."""
from datetime import date, datetime, timedelta, timezone

import pytest

import bcmd_candidates as candidates
import bcmd_export as exports
import bcmd_store as store


def test_legacy_policy_and_header_hashes_are_pinned():
    # Recomputed from base commit cbfe09b: existing TK v1 batches and DETAIL exports must keep verifying.
    assert store.LEGACY_VERSION == 'tk-ched-enrolled-y1-s1-v1'
    assert store.POLICIES[store.LEGACY_VERSION][2] == 'b1075f44126c47974f0da55af644a963ad8b6e0966a4d2e81da415eaf6c3111e'
    assert exports.LEGACY_HEADER_SHA256 == 'bf9e27367098f87fe8f33dc7e6633d557d320f467bc62f357fcf2284e22b1a68'


def test_ched_only_policies_of_saved_batches_keep_their_hashes():
    # Recomputed from origin/main 697ccf2: batches saved before the unified source keep verifying.
    assert {version: store.POLICIES[version][2] for version in store.CHED_POLICY_VERSIONS.values()} == {
        'tk-ched-enrolled-y1-s1-age-v2': '2bc0e3f3b6f447b4dc0b4becae45ed16a3bff8cfcc213be51e169e232e1d3624',
        'ta-ched-enrolled-age-v1': 'c18cd902944658cdad7b4bd03f34f8d4428ca6df745a4db4efb9462fc5a8f5b4',
        'tt-ched-graduated-age-v1': '46818846a87251848b1846ff526c63d7950f9f48523cb44ff94629472ec64e98'}
    # New batches use the unified source with payment history at selection.
    assert all('unified' in version for version in store.POLICY_VERSIONS.values())
    assert set(store.POLICY_VERSIONS.values()).isdisjoint(store.CHED_POLICY_VERSIONS.values())


class FrozenDateTime(datetime):
    moment = None

    @classmethod
    def now(cls, tz=None):
        return cls.moment.astimezone(tz)


@pytest.mark.parametrize('utc,expected', [
    (datetime(2026, 11, 3, 15, 59, tzinfo=timezone.utc), date(2026, 11, 3)),  # 23:59 in Manila
    (datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc), date(2026, 11, 4)),   # midnight in Manila
    (datetime(2026, 11, 3, 23, 30, tzinfo=timezone.utc), date(2026, 11, 4)),  # 07:30 Manila; UTC date still 3 Nov
])
def test_today_is_the_manila_date(monkeypatch, utc, expected):
    FrozenDateTime.moment = utc
    monkeypatch.setattr(candidates, 'datetime', FrozenDateTime)
    assert candidates.manila_today() == expected


@pytest.mark.parametrize('today,dob,included', [
    (date(2026, 2, 28), date(2008, 2, 29), False),  # a 29 February birth turns 18 on 1 March in a common year
    (date(2026, 3, 1), date(2008, 2, 29), True),
    (date(2028, 2, 29), date(2010, 2, 28), True),
    (date(2028, 2, 29), date(2010, 3, 1), False),
    (date(2028, 2, 29), date(1997, 2, 28), False),  # 31
    (date(2028, 2, 29), date(1997, 3, 1), True),    # 30
])
def test_leap_day_age_bounds(today, dob, included):
    low, high = candidates.age_bounds(today)
    assert (low < dob <= high) is included
    assert (18 <= candidates.age_on(dob, today) <= 30) is included


def test_sql_bounds_and_dqa_age_agree_every_day_through_a_leap_cycle():
    today = date(2027, 12, 1)
    while today <= date(2029, 3, 31):
        low, high = candidates.age_bounds(today)
        for years in (17, 18, 30, 31):
            edge = candidates.years_before(today, years)
            for dob in (edge - timedelta(1), edge, edge + timedelta(1)):
                assert (low < dob <= high) == (18 <= candidates.age_on(dob, today) <= 30), (today, dob)
        today += timedelta(1)


@pytest.mark.parametrize('value,expected', [
    ('=1+1', "'=1+1"), ('+63', "'+63"), ('-1', "'-1"), ('@SUM(A1)', "'@SUM(A1)"),
    (' =1', "'=1"), ('\t=1', "'=1"), ('Line\r\nbreak', 'Line  break'), ('Dela Cruz', 'Dela Cruz'), (None, ''),
])
def test_summary_and_failed_cells_neutralise_formulas(value, expected):
    assert exports._cell(value) == expected


@pytest.mark.parametrize('value', ['＝1', '＋1', '－1', '＠1'])
def test_full_width_formula_prefixes_are_neutralised(value):
    assert exports._cell(value).startswith("'")
