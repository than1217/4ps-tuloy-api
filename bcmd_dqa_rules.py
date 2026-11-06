"""DQA ruleset essi-mvp-v4 on frozen BCMD members (MVP, draft until BCMD approves DQA_RULES).

Sources: "Proposed ESSI 4Ps Data Quality Parameters" (draft), the 2026-11-03 review
meeting (BCMD-08, BCMD-11, NAPA-03) and the TULOY MySQL batch runbook section 4.
A member with no FAIL, REVIEW or EXCLUDED finding is PASS and goes to the NAPA files. Passing
DQA never authorizes payment; exports stay behind the recorded approval gates.
v3 adds EXCLUDED: a duplicate-selection review kept the same member and period in another
batch (docs/bcmd-duplicate-selection-review.md). EXCLUDED records are in no NAPA file.
v4 adds payment history (bcmd_payment_history): a recorded payment that blocks this category and
period is FAIL when its status is confirmed paid and REVIEW (PAYMENT_PENDING) otherwise; an
unmappable one is REVIEW (PAYMENT_UNMAPPED). TESDA short courses are never TA and stay held
pending policy. A run that cannot read payment history fails and retries; it never passes a member.
"""
import hashlib
import json
import re
from datetime import date

import bcmd_payment_history as payment_history
from bcmd_candidates import AGE_MAX, AGE_MIN, age_on, valid_academic_year
from napa_detail import NapaValidationError, serialize_detail

VERSION = 'essi-mvp-v4'
PREVIOUS_VERSIONS = ('essi-draft-frozen-review-v1', 'essi-mvp-v2', 'essi-mvp-v3')
# Strongest first: unrelated FAIL or REVIEW findings keep their effect on an excluded member.
OUTCOME_ORDER = ('FAIL', 'REVIEW', 'EXCLUDED', 'PASS')

# (rule code, severity, what it checks, ESSI parameter reference)
CHECKS = (
    ('MISSING_MEMBER_ID', 'FAIL', 'Member ID is present', 'ESSI 02'),
    ('MISSING_HHID', 'FAIL', 'Member is linked to a household ID', 'ESSI 02'),
    ('MISSING_NAME', 'FAIL', 'Last and first name are present', 'ESSI 01'),
    ('DUPLICATE_MEMBER', 'FAIL', 'Member appears once in this batch', 'ESSI 02'),
    ('INVALID_BIRTH_DATE', 'FAIL', 'Birth date is valid and not in the future', 'ESSI 03'),
    ('AGE_OUTSIDE_RANGE', 'FAIL', f'Age is {AGE_MIN}-{AGE_MAX} inclusive on the run date', 'MC age rule'),
    ('INVALID_INSTITUTIONAL_PERIOD', 'FAIL', 'Academic year (YYYY-YYYY) and semester are valid (TK/TA)', 'ESSI 08'),
    ('NAPA_FIELD_INVALID', 'FAIL', 'Every NAPA detailed field is valid', 'NAPA template'),
    ('SOURCE_MAPPING_FAILED', 'FAIL', 'Frozen source mapping checks passed at selection', 'NAPA template'),
    ('DUPLICATE_CLAIM', 'FAIL', 'Same member and period is not already in another batch with a NAPA export', 'ESSI 12-13'),
    ('SELECTED_IN_OTHER_BATCH', 'REVIEW', 'Same member and period is not selected in another batch without a NAPA export', 'ESSI 12'),
    ('SELECTION_EXCLUDED', 'EXCLUDED', 'A duplicate-selection review did not keep this member and period in this batch', 'ESSI 12'),
    ('ALREADY_PAID_TK', 'FAIL', 'No confirmed-paid TK payment is recorded for this member (TK is paid once)', 'ESSI 13'),
    ('ALREADY_PAID_TT', 'FAIL', 'No confirmed-paid TT payment is recorded for this member (TT is paid once)', 'ESSI 13'),
    ('TA_BLOCKED_BY_TT_PAYMENT', 'FAIL', 'No confirmed-paid TT payment is recorded for this member (it ends TA)', 'ESSI 13'),
    ('ALREADY_PAID_SAME_PERIOD', 'FAIL', 'No confirmed-paid TA payment is recorded for this member, academic year and semester', 'ESSI 13'),
    ('SHORT_COURSE_NOT_ELIGIBLE_TA', 'FAIL', 'The enrollment is not a TESDA short course (never TA)', 'Program rule'),
    ('PAYMENT_PENDING', 'REVIEW', 'No recorded, not yet confirmed payment blocks this member under the program rules', 'ESSI 13'),
    ('PAYMENT_UNMAPPED', 'REVIEW', 'No recorded payment for this member lacks a category or period', 'ESSI 13'),
    ('SHORT_COURSE_POLICY_PENDING', 'REVIEW', 'The enrollment is not a TESDA short course (policy pending)', 'Q-P1'),
)
CHECKED_AT_SELECTION = (
    ('CLIENT_STATUS', 'Active 4Ps client status (CS 1, 3, 14, 15, 19, 28) and grade level, via vw_verify_member_bene', 'ESSI 04'),
    ('PARTNER_VALIDATION', 'CHED degree or TESDA diploma matched record with the category enrollment or graduation status', 'ESSI 07, 11, 14'),
)
NOT_EVALUATED = (
    ('SHS_STATUS', 'Senior high school completion status is not in TULOY data', 'ESSI 05'),
    ('PATHWAY', 'Intended pathway is not in TULOY data', 'ESSI 06'),
    ('TVET_PROGRAM', 'TVET program accreditation is not in TULOY data', 'ESSI 09'),
    ('TULOY_GABAY', 'Tuloy Gabay participation is not in TULOY data', 'ESSI 10'),
    ('EXTERNAL_DISBURSEMENT_LEDGER', 'Disbursements recorded outside payment_transaction are not available', 'ESSI 13'),
    ('PAYMENT_ACCOUNT', 'Payment account is checked in Payroll, not in DQA', 'ESSI 15'),
)
CONFIG = {
    'version': VERSION, 'approval': 'DRAFT_UNTIL_DQA_RULES_GATE',
    'age': {'minimum': AGE_MIN, 'maximum': AGE_MAX, 'inclusive': True, 'as_of': 'run_creation_Asia/Manila'},
    'family_id_nullable': True,
    'duplicate_claim': {'key': ['member_ref', 'academic_year', 'semester'],
                        'other_batch_with_napa_export': 'FAIL', 'other_batch_without_napa_export': 'REVIEW',
                        'review_kept_other_batch': 'EXCLUDED',
                        'review_kept_this_batch_covering_all_selections': 'NO_FINDING'},
    'payment_history': payment_history.config(),
    'short_course': {'TA': 'FAIL', 'TK': 'REVIEW', 'TT': 'REVIEW', 'academic_period': 'never derived from dates'},
    'outcome_order': list(OUTCOME_ORDER),
    'checks': [list(check) for check in CHECKS],
    'checked_at_selection': [list(item) for item in CHECKED_AT_SELECTION],
    'not_evaluated': [list(item) for item in NOT_EVALUATED],
    'sources': ['Proposed ESSI_4PsData_Quality-Parameters (draft)', 'review_meeting:2026-11-03', 'worker_runbook:4'],
}
CANONICAL = json.dumps(CONFIG, sort_keys=True, separators=(',', ':'))
HASH = hashlib.sha256(CANONICAL.encode()).hexdigest()
REMARKS = {
    'MISSING_MEMBER_ID': 'Member ID is missing. Correct the member record in the source.',
    'MISSING_HHID': 'No household ID is linked to the member. Correct the household roster.',
    'MISSING_NAME': 'Last name or first name is missing. Correct the member record.',
    'DUPLICATE_MEMBER': 'The member appears more than once in this batch.',
    'INVALID_BIRTH_DATE': 'Birth date is missing, invalid or in the future. Correct the member record.',
    'AGE_OUTSIDE_RANGE': f'Age is outside {AGE_MIN}-{AGE_MAX} on the run date.',
    'INVALID_INSTITUTIONAL_PERIOD': 'Academic year or semester is missing or invalid. Correct the enrollment record.',
    'NAPA_FIELD_INVALID': 'A required NAPA field is missing or invalid. Correct the source record.',
    'SOURCE_MAPPING_FAILED': 'A source mapping check failed when the batch was saved. Correct the source and use a new batch.',
    'DUPLICATE_CLAIM': 'The same member and period is already in another batch with a NAPA export.',
    'SELECTED_IN_OTHER_BATCH': 'The same member and period is also selected in another batch. Verify which batch should claim it.',
    'SELECTION_EXCLUDED': 'A duplicate-selection review kept the same member and period in another batch.',
    'ALREADY_PAID_TK': 'A confirmed TK payment is already recorded for this member. TK is paid once.',
    'ALREADY_PAID_TT': 'A confirmed TT payment is already recorded for this member. TT is paid once.',
    'TA_BLOCKED_BY_TT_PAYMENT': 'A confirmed TT payment is recorded for this member, so TA cannot be paid.',
    'ALREADY_PAID_SAME_PERIOD': 'A confirmed TA payment is already recorded for this member, academic year and semester.',
    'SHORT_COURSE_NOT_ELIGIBLE_TA': 'A TESDA short course is not eligible for TA.',
    'PAYMENT_PENDING': 'Payment recorded, not yet confirmed paid. Another payment would be a duplicate under the program rules.',
    'PAYMENT_UNMAPPED': 'A recorded payment for this member could not be matched to a category and period. Verify it before paying.',
    'SHORT_COURSE_POLICY_PENDING': 'TESDA short-course eligibility is not decided yet. Hold this record.',
}
SEVERITY = {code: severity for code, severity, _, _ in CHECKS}


def public_checks():
    return {'checks': [{'rule_code': c, 'severity': s, 'label': l, 'reference': r} for c, s, l, r in CHECKS],
            'checked_at_selection': [{'key': k, 'label': l, 'reference': r} for k, l, r in CHECKED_AT_SELECTION],
            'not_evaluated': [{'key': k, 'label': l, 'reference': r} for k, l, r in NOT_EVALUATED]}


def claim_key(member):
    return (member.get('member_ref'), member.get('academic_year') or '', int(member.get('semester') or 0))


def evaluate(member, batch, evaluation_date, *, duplicate_member=False, legacy_issues=(),
             prior_claim=False, other_selection=False, excluded_selection=False, payment_findings=()):
    findings = []

    def add(code, field=None, detail=None):
        findings.append({'rule_code': code, 'severity': SEVERITY[code],
                         'remark': REMARKS[code] + (f' ({detail})' if detail else ''), 'field': field})

    if not member.get('member_ref'):
        add('MISSING_MEMBER_ID', 'member_ref')
    if not member.get('hhid'):
        add('MISSING_HHID', 'hhid')
    if not (member.get('last_name') or '').strip() or not (member.get('first_name') or '').strip():
        add('MISSING_NAME', 'last_name')
    if duplicate_member:
        add('DUPLICATE_MEMBER', 'member_ref')
    try:
        raw = str(member.get('date_of_birth_raw') or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError('format')
        dob = date.fromisoformat(raw)
        if dob > evaluation_date:
            raise ValueError('future date')
        age = age_on(dob, evaluation_date)
        if not AGE_MIN <= age <= AGE_MAX:
            # No computed age in the remark: the FAILED file discloses no birth date (napa-disclosure.md).
            add('AGE_OUTSIDE_RANGE', 'date_of_birth_raw')
    except ValueError:
        add('INVALID_BIRTH_DATE', 'date_of_birth_raw')
    if batch['category'] in ('TK', 'TA'):
        semester = member.get('semester')
        if not valid_academic_year(member.get('academic_year')) or semester not in (1, 2, 3):
            add('INVALID_INSTITUTIONAL_PERIOD', 'academic_year')
    elif member.get('academic_year') and not valid_academic_year(member.get('academic_year')):
        add('INVALID_INSTITUTIONAL_PERIOD', 'academic_year')
    for field, code in legacy_issues:
        add('SOURCE_MAPPING_FAILED', field, code)
    aliases = {'member_id': 'member_ref', 'member_last_name': 'last_name', 'member_first_name': 'first_name',
               'member_middle_name': 'middle_name', 'member_ext_name': 'ext_name', 'date_of_birth': 'date_of_birth_raw'}
    row = {**member, **{key: member.get(value) for key, value in aliases.items()},
           'line_no': 1, 'source_agency': member.get('source_agency') or batch['source_agency']}
    try:
        serialize_detail([row], batch['category'])
    except NapaValidationError as exc:
        add('NAPA_FIELD_INVALID', exc.field, exc.code)
    if prior_claim:
        add('DUPLICATE_CLAIM', 'member_ref')
    if other_selection:
        add('SELECTED_IN_OTHER_BATCH', 'member_ref')
    if excluded_selection:
        add('SELECTION_EXCLUDED', 'member_ref')
    if member.get('course_type') == 'short_course':
        add('SHORT_COURSE_NOT_ELIGIBLE_TA' if batch['category'] == 'TA' else 'SHORT_COURSE_POLICY_PENDING', 'course_type')
    for found in payment_findings:
        # The payment row reference, like conflict_id; the NAPA FAILED file keeps only rule code and remark.
        add(found['code'], 'member_ref')
        findings[-1]['payment_ref'] = found['payment_ref']
    severities = {f['severity'] for f in findings}
    outcome = next(level for level in OUTCOME_ORDER if level in severities or level == 'PASS')
    return outcome, findings
