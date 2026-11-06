"""Exact uploaded headers, synthetic values, and no live database or beneficiary rows."""
import csv
import io

import pytest

from napa_detail import DETAIL_HEADERS, FIELDS, NapaValidationError, serialize_detail


def member(line=1, **overrides):
    return {"line_no": line, "region_name": "Synthetic Region", "region_psgc": "0100000000",
            "province_name": "Synthetic Province", "province_psgc": "0101000000",
            "municipality_name": "Synthetic Municipality", "municipality_psgc": "0101010000",
            "barangay_name": "Synthetic Barangay", "barangay_psgc": "0101010001",
            "hhid": "00001234", "family_id": "00005678", "member_id": "00009012",
            "member_last_name": "Synthetic", "member_first_name": "Example", "member_middle_name": "Test",
            "member_ext_name": "Jr.", "sex": "FEMALE", "ip_indicator": "NO",
            "date_of_birth": "2001-02-03", "source_agency": "CHED", **overrides}


def read(data):
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def test_exact_header_strings_and_column_order():
    assert len(DETAIL_HEADERS) == 20
    assert DETAIL_HEADERS[:4] == ("REGION", "REGION(PSGC)", "PROVINCE", "PROVINCE(SURROGATE KEY PSGC))")
    assert DETAIL_HEADERS[8:15] == ("HHID", "FamilyID", "MemberID", "MEMBER Last name", "MEMBER First name", "MEMBER Middle name", "Ext. name")
    assert DETAIL_HEADERS[15:19] == ("Sex\nFEMALE / MALE", "IP (from DSWD)\nYES / NO", "Date of Birth\n25-12-2026 (day-month-year)", "VALIDATED SOURCE OF ORIGIN\nCHED/ TESDA")
    assert DETAIL_HEADERS[19] == "CATEGORY\n\nTK - Tuloy Kolehiyo\nTA - Tuloy Aral\nTT- Tuloy Trabaho"
    assert tuple(read(serialize_detail([], "TK"))[0]) == DETAIL_HEADERS


def test_format_preserves_zeroes_unicode_quoted_text_dates_and_frozen_order():
    data = serialize_detail([member(2, member_id="0002"), member(1, member_last_name='Syn, "Test"', member_first_name="Jose\u0301")], "TA")
    rows = read(data)
    assert len(rows) == 3 and all(len(row) == 20 for row in rows)
    assert rows[1][1] == "0100000000" and rows[1][8:11] == ["00001234", "00005678", "00009012"]
    assert rows[1][11:13] == ['Syn, "Test"', "José"]
    assert rows[1][17:] == ["03-02-2001", "CHED", "TA"]
    assert rows[2][10] == "0002"
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and data.endswith(b"\n")


@pytest.mark.parametrize("field", FIELDS)
def test_missing_mapping_never_filled_with_fabricated_data(field):
    row = member(); del row[field]
    with pytest.raises(NapaValidationError) as error:
        serialize_detail([row], "TK")
    assert error.value.code == "MAPPING_REQUIRED" and error.value.field == field


def test_family_and_ip_nulls_follow_approved_policy_without_literal_null_text():
    row = read(serialize_detail([member(family_id=None, ip_indicator=None)], 'TK'))[1]
    assert row[9] == '' and row[16] == ''
    assert len(row) == 20
    row = read(serialize_detail([member(ip_indicator='NO')], 'TK'))[1]
    assert row[16] == 'NO'


@pytest.mark.parametrize('value', [None, '', '   '])
def test_middle_name_and_suffix_are_optional_under_approved_policy(value):
    row = read(serialize_detail([member(member_middle_name=value, member_ext_name=value)], 'TK'))[1]
    assert row[13:15] == ['', '']


def test_first_and_last_names_remain_required():
    for field in ('member_first_name', 'member_last_name'):
        with pytest.raises(NapaValidationError):
            serialize_detail([member(**{field: None})], 'TK')


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠"])
def test_formula_input_rejected_unless_prefix_policy_is_explicit(prefix):
    row = member(member_first_name=prefix + "synthetic")
    with pytest.raises(NapaValidationError) as error:
        serialize_detail([row], "TK")
    assert error.value.code == "FORMULA_POLICY_REQUIRED"
    assert read(serialize_detail([row], "TK", formula_policy="prefix"))[1][12].startswith("'")


@pytest.mark.parametrize("overrides", [{"date_of_birth": "22-02-20010"}, {"region_psgc": 1},
                                      {"sex": "UNKNOWN"}, {"ip_indicator": "UNKNOWN"}, {"source_agency": "DSWD"}])
def test_invalid_fields_fail_without_leaking_values(overrides):
    with pytest.raises(NapaValidationError) as error:
        serialize_detail([member(**overrides)], "TK")
    for value in overrides.values():
        if isinstance(value, str):
            assert value not in str(error.value)


def test_duplicate_order_and_row_limit_rejected():
    with pytest.raises(NapaValidationError):
        serialize_detail([member(1), member(1)], "TK")
    with pytest.raises(NapaValidationError):
        serialize_detail([member(1), member(2)], "TK", max_rows=1)


@pytest.mark.parametrize('code', ['01', '010101001', '01010100011', 'abcdefghij', '０１０１０１０００１'])
def test_psgc_requires_the_ten_digit_source_code_without_guessing_or_padding(code):
    with pytest.raises(NapaValidationError) as exc:
        serialize_detail([member(barangay_psgc=code)], 'TK')
    assert exc.value.code == 'INVALID_CODE'


def test_approved_household_family_and_ip_rules_reach_csv_without_dropping_members():
    from napa_mapping import resolve_family, resolve_hhid, resolve_ip

    # A synthetic member without a family association and unknown IP remains in
    # the file with empty cells; the household identifier is still present.
    row = member(hhid=resolve_hhid(301), family_id=resolve_family(
        person_id=101, roster_id=201, household_id=301, candidates=[]),
        ip_indicator=resolve_ip(person_group_id=None, household_group_id=None,
                                source='person', catalog={}))
    output = read(serialize_detail([row], 'TK'))
    assert len(output) == 2
    assert output[1][8:10] == ['301', '']
    assert output[1][16] == ''
