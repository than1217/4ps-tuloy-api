from datetime import date
import pytest
from test_napa_detail import member
from bcmd_dqa_rules import evaluate


def frozen(dob):
    row = member(date_of_birth=dob)
    return {**row, 'member_ref': row['member_id'], 'last_name': row['member_last_name'],
            'first_name': row['member_first_name'], 'middle_name': row['member_middle_name'],
            'ext_name': row['member_ext_name'], 'date_of_birth_raw': dob,
            'academic_year': '2026-2027', 'semester': 1}


TK = {'category': 'TK', 'source_agency': 'CHED'}


@pytest.mark.parametrize('dob,outside', [
    ('2008-11-03', False), ('2008-11-04', True),
    ('1996-11-03', False), ('1995-11-04', False), ('1995-11-03', True),
])
def test_inclusive_age_boundaries(dob, outside):
    outcome, findings = evaluate(frozen(dob), TK, date(2026, 11, 3))
    assert ('AGE_OUTSIDE_RANGE' in [f['rule_code'] for f in findings]) is outside
    assert outcome == ('FAIL' if outside else 'PASS')


def codes(findings):
    return [f['rule_code'] for f in findings]


@pytest.mark.parametrize('category,change,expected', [
    ('TK', {}, []),
    ('TA', {'semester': 0}, ['INVALID_INSTITUTIONAL_PERIOD']),
    ('TA', {'academic_year': '2026-2028'}, ['INVALID_INSTITUTIONAL_PERIOD']),
    ('TT', {'semester': 0}, []),
    ('TT', {'academic_year': 'NN'}, ['INVALID_INSTITUTIONAL_PERIOD']),
    ('TK', {'first_name': ' '}, ['MISSING_NAME', 'NAPA_FIELD_INVALID']),
    ('TK', {'last_name': '=HYPERLINK("x")'}, ['NAPA_FIELD_INVALID']),  # formulas never reach DETAIL
    ('TA', {'first_name': '＠cmd'}, ['NAPA_FIELD_INVALID']),
])
def test_category_period_and_identity_rules(category, change, expected):
    outcome, findings = evaluate({**frozen('2001-02-03'), **change}, {'category': category, 'source_agency': 'CHED'},
                                 date(2026, 11, 3))
    assert codes(findings) == expected and outcome == ('FAIL' if expected else 'PASS')
    assert all(f['remark'] for f in findings)


def test_cross_batch_claims():
    outcome, findings = evaluate(frozen('2001-02-03'), TK, date(2026, 11, 3), other_selection=True)
    assert outcome == 'REVIEW' and codes(findings) == ['SELECTED_IN_OTHER_BATCH']
    outcome, findings = evaluate(frozen('2001-02-03'), TK, date(2026, 11, 3), prior_claim=True, other_selection=True)
    assert outcome == 'FAIL' and codes(findings) == ['DUPLICATE_CLAIM', 'SELECTED_IN_OTHER_BATCH']


@pytest.mark.parametrize('dob', ['', '22-02-20010', '2026-11-04'])
def test_invalid_or_future_birthdates_fail(dob):
    outcome, findings = evaluate(frozen(dob), {'category': 'TK', 'source_agency': 'CHED'}, date(2026, 11, 3))
    assert outcome == 'FAIL' and 'INVALID_BIRTH_DATE' in [f['rule_code'] for f in findings]
