"""Resolve ODS references without importing legacy web-app lookup meanings.

Pure mapping helpers for the future snapshot adapter. They perform no queries or
authorization and never turn unknown source values into a guessed CSV value.
Reference catalogs must come from the approved, versioned ODS source.
"""
from dataclasses import dataclass
from typing import Mapping


class MappingBlocked(ValueError):
    def __init__(self, field: str, code: str):
        self.field = field
        self.code = code
        # Do not put source identifiers or personal values in errors/logs.
        super().__init__(f'{field}: {code}')


@dataclass(frozen=True)
class ReferenceEntry:
    value: str
    active: bool


def _lookup(field: str, source_id, catalog: Mapping[int, ReferenceEntry]):
    if type(source_id) is not int or source_id < 0:
        raise MappingBlocked(field, 'LOOKUP_ID_INVALID')
    entry = catalog.get(source_id)
    if entry is None:
        raise MappingBlocked(field, 'LOOKUP_NOT_FOUND')
    if not entry.active:
        raise MappingBlocked(field, 'LOOKUP_INACTIVE')
    if not isinstance(entry.value, str) or not entry.value.strip():
        raise MappingBlocked(field, 'LOOKUP_VALUE_MISSING')
    return entry.value.strip()


def resolve_sex(source_id, catalog: Mapping[int, ReferenceEntry]) -> str:
    """lib_sex (copied from ODS lib_sex_v3): 1=MALE, 2=FEMALE; NONE must not become FEMALE."""
    value = _lookup('sex', source_id, catalog).upper()
    if value not in ('MALE', 'FEMALE'):
        raise MappingBlocked('sex', 'SEX_NOT_EXPORTABLE')
    return value


def resolve_extension(source_id, catalog: Mapping[int, ReferenceEntry]):
    """A genuinely absent suffix stays absent; an unknown ID is an error."""
    if source_id is None:
        return None
    return _lookup('member_ext_name', source_id, catalog)


def resolve_ip(*, person_group_id, household_group_id, source: str,
               catalog: Mapping[int, ReferenceEntry], approved_no_ids=frozenset({0})) -> str | None:
    """Martin's 2026-10-30 rule: 0=NO, NULL remains unknown.

    There is no fallback between person and household membership. A recognized
    nonzero membership maps to YES; invalid references still block the mapping.
    """
    if source not in ('person', 'household'):
        raise MappingBlocked('ip_indicator', 'IP_SOURCE_UNAPPROVED')
    source_id = person_group_id if source == 'person' else household_group_id
    if source_id is None:
        return None
    if type(source_id) is not int or source_id < 0:
        raise MappingBlocked('ip_indicator', 'LOOKUP_ID_INVALID')
    if any(type(item) is not int or item < 0 for item in approved_no_ids):
        raise MappingBlocked('ip_indicator', 'IP_NO_POLICY_INVALID')
    if source_id in approved_no_ids:
        if source_id in catalog:
            raise MappingBlocked('ip_indicator', 'IP_POLICY_CONFLICT')
        return 'NO'
    _lookup('ip_indicator', source_id, catalog)
    return 'YES'


def resolve_hhid(household_id: int) -> str:
    """The approved HHID source is the internal household_id, not Listahanan ID."""
    if type(household_id) is not int or household_id <= 0:
        raise MappingBlocked('hhid', 'HOUSEHOLD_ID_INVALID')
    return str(household_id)


def resolve_family(*, person_id: int, roster_id: int, household_id: int,
                   candidates: list[dict]) -> str | None:
    """Use household_id for a confirmed family; missing association stays NULL.

    The adapter joins selected family by household, then verifies the association
    by family_id, person_id and hh_roster_id. The output is household_id, not the
    CBMS family_id, per Martin's 2026-10-30 decision. Multiple matches still block.
    """
    hhid = resolve_hhid(household_id)
    matches = [row for row in candidates
               if row.get('person_id') == person_id
               and row.get('hh_roster_id') == roster_id
               and row.get('household_id') == household_id
               and row.get('archived') == 0]
    if not matches:
        return None
    if len(matches) != 1:
        raise MappingBlocked('family_id', 'FAMILY_ASSOCIATION_AMBIGUOUS')
    value = matches[0].get('family_id')
    if not isinstance(value, str) or not value.strip():
        raise MappingBlocked('family_id', 'FAMILY_VALUE_MISSING')
    return hhid
