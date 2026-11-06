"""Synthetic source IDs only; no live queries or beneficiary values."""
import pytest

from napa_mapping import (MappingBlocked, ReferenceEntry, resolve_extension,
                          resolve_family, resolve_hhid, resolve_ip, resolve_sex)


SEX = {1: ReferenceEntry('MALE', True), 2: ReferenceEntry('FEMALE', True),
       3: ReferenceEntry('NONE', True)}
EXT = {1: ReferenceEntry('Sr.', True), 2: ReferenceEntry('Jr.', True)}
IP = {7: ReferenceEntry('Synthetic IP group', True)}


def test_ods_sex_ids_are_not_the_legacy_zero_one_lookup():
    assert resolve_sex(1, SEX) == 'MALE'
    assert resolve_sex(2, SEX) == 'FEMALE'
    with pytest.raises(MappingBlocked, match='LOOKUP_NOT_FOUND'):
        resolve_sex(0, SEX)
    with pytest.raises(MappingBlocked, match='SEX_NOT_EXPORTABLE'):
        resolve_sex(3, SEX)


@pytest.mark.parametrize('value', [None, True, False, '1', -1, 1.0])
def test_lookup_does_not_coerce_bad_identifiers(value):
    with pytest.raises(MappingBlocked, match='LOOKUP_ID_INVALID'):
        resolve_sex(value, SEX)


def test_extension_absence_is_different_from_an_unknown_suffix():
    assert resolve_extension(None, EXT) is None
    assert resolve_extension(2, EXT) == 'Jr.'
    with pytest.raises(MappingBlocked, match='LOOKUP_NOT_FOUND'):
        resolve_extension(99999, EXT)


@pytest.mark.parametrize('entry,code', [(ReferenceEntry('Jr.', False), 'LOOKUP_INACTIVE'),
                                       (ReferenceEntry(' ', True), 'LOOKUP_VALUE_MISSING')])
def test_reference_quality_errors(entry, code):
    with pytest.raises(MappingBlocked, match=code):
        resolve_extension(1, {1: entry})


def ip(**overrides):
    return resolve_ip(**{'person_group_id': 7, 'household_group_id': None,
                         'source': 'person', 'catalog': IP, **overrides})


def test_ip_uses_selected_source_without_fallback():
    assert ip() == 'YES'
    assert ip(source='household') is None
    with pytest.raises(MappingBlocked, match='IP_SOURCE_UNAPPROVED'):
        ip(source='auto')


def test_approved_ip_rule_keeps_null_distinct_from_no():
    assert ip(person_group_id=None) is None
    assert ip(person_group_id=0) == 'NO'
    with pytest.raises(MappingBlocked, match='LOOKUP_NOT_FOUND'):
        ip(person_group_id=99999)


def test_no_requires_explicit_nonconflicting_policy():
    assert ip(person_group_id=0, approved_no_ids=frozenset({0})) == 'NO'
    with pytest.raises(MappingBlocked, match='IP_POLICY_CONFLICT'):
        ip(approved_no_ids=frozenset({7}))
    with pytest.raises(MappingBlocked, match='IP_NO_POLICY_INVALID'):
        ip(approved_no_ids=frozenset({False}))


def family(**overrides):
    return {'person_id': 101, 'hh_roster_id': 201, 'household_id': 301,
            'family_id': '00001234', 'archived': 0, **overrides}


def choose(rows):
    return resolve_family(person_id=101, roster_id=201, household_id=301, candidates=rows)


def test_both_hhid_and_nonnull_family_use_household_id_not_cbms_or_listahanan():
    assert resolve_hhid(301) == '301'
    assert choose([family(), family(person_id=999)]) == '301'


@pytest.mark.parametrize('value', [None, 0, -1, True, '301', 301.0])
def test_household_ids_are_validated_before_serializing(value):
    with pytest.raises(MappingBlocked, match='HOUSEHOLD_ID_INVALID'):
        resolve_hhid(value)


@pytest.mark.parametrize('row', [family(archived=1), family(person_id=999),
                                family(household_id=999), family(hh_roster_id=999)])
def test_missing_family_is_null_instead_of_using_an_unrelated_association(row):
    assert choose([row]) is None


def test_no_family_candidate_is_null():
    assert choose([]) is None


def test_duplicate_family_associations_block_instead_of_selecting_first():
    with pytest.raises(MappingBlocked, match='FAMILY_ASSOCIATION_AMBIGUOUS'):
        choose([family(), family(family_id='00005678')])


def test_mapping_errors_do_not_include_personal_values():
    with pytest.raises(MappingBlocked) as exc:
        choose([family(family_id=123456789)])
    assert '123456789' not in str(exc.value)
