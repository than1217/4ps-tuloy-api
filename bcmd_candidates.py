"""TK/TA/TT discovery on the unified CHED/TESDA source. Shared predicates for preview, counts and save.

Source (the same precedence as vw_unified_matched_enrollment and /GetUnifiedMatchedBene, see
docs/UNIFIED_MATCHED_API.md): CHED degree rows, plus TESDA diploma rows only when that tuloy_id has
no CHED match. The view has no stable row identity, so discovery reads its two source tables, the
way COR review does; each record is identified by agency, source row ID and course type.
TESDA short courses have no academic year, semester or year level (Q-P1, Q-P3): they are never
selectable and are counted separately as SHORT_COURSE_POLICY_PENDING.
Every category requires current 4Ps eligibility through vw_verify_member_bene, an age of 18-30
inclusive on the Philippine selection date (BCMD-08) and no recorded payment that blocks it
(bcmd_payment_history). Each category adds its fixed rules (BCMD-01).
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import bcmd_payment_history as payment_history

MANILA = ZoneInfo('Asia/Manila')
AGE_MIN, AGE_MAX = 18, 30
PREVIEW_TIMEOUT_MS, SELECTION_TIMEOUT_MS = 5000, 30000
ACADEMIC_YEAR = re.compile(r'(\d{4})-(\d{4})')
SOURCE_REF = re.compile(r'(CHED|TESDA):([1-9][0-9]{0,9}):(degree|diploma)')

# Fixed (non-negotiable) rules per category. Optional filters are listed per category.
CATEGORIES = {
    'TK': {'label': 'Tuloy Kolehiyo', 'prefix': 'TLY-KLHYO', 'status': 'ENROLLED',
           'year_level': '1', 'semester': 1, 'optional': ('academic_year',)},
    'TA': {'label': 'Tuloy Aral', 'prefix': 'TLY-ARL', 'status': 'ENROLLED',
           'year_level': None, 'semester': None, 'optional': ('academic_year', 'semester', 'year_level')},
    'TT': {'label': 'Tuloy Trabaho', 'prefix': 'TLY-TRBH', 'status': 'GRADUATED',
           'year_level': None, 'semester': None, 'optional': ('academic_year',)},
}
# Kept for the legacy TK v1 policy record; equivalent to the TK fixed rules above.
TK_ELIGIBILITY_SQL = """UPPER(TRIM(m.enrollment_status))='ENROLLED'
    AND TRIM(m.degree_year_level)='1' AND TRIM(m.degree_semester)='1'"""

NO_CHED_MATCH = 'NOT EXISTS (SELECT 1 FROM enroll_matched_CHED_tbl c WHERE c.tuloy_id=m.tuloy_id)'


def _json(column, field):
    # A JSON null becomes SQL NULL; identifiers are fixed constants, never request text.
    return f"NULLIF(JSON_UNQUOTE(JSON_EXTRACT(m.{column},'$.{field}')),'null')"


# Normalized fields per source. Every expression is a fixed SQL fragment over alias m.
SOURCES = {
    'CHED': {'order': 0, 'course_type': 'degree',
             'from': "enroll_matched_CHED_tbl m JOIN lib_partner_agency a ON a.agency_id=m.agency_id AND a.agency_alias='CHED'",
             'where': [], 'academic_year': 'm.degree_academic_year', 'semester': 'm.degree_semester',
             'year_level': 'TRIM(m.degree_year_level)', 'status': 'UPPER(TRIM(m.enrollment_status))',
             'program_name': 'm.degree_program_name'},
    'TESDA': {'order': 1, 'course_type': 'diploma', 'from': 'enroll_matched_TESDA_tbl m',
              'where': ["JSON_TYPE(m.diploma)='OBJECT'", NO_CHED_MATCH],
              'academic_year': _json('diploma', 'academic_year'),
              'semester': f"CASE WHEN {_json('diploma', 'semester')} REGEXP '^[0-9]{{1,2}}$' "
                          f"THEN CAST({_json('diploma', 'semester')} AS UNSIGNED) END",
              'year_level': f"TRIM({_json('diploma', 'year_level')})",
              'status': f"UPPER(TRIM({_json('diploma', 'enrollment_status')}))",
              'program_name': _json('diploma', 'program_name')},
}
SHORT_COURSE_FROM = 'enroll_matched_TESDA_tbl m'
SHORT_COURSE_WHERE = ["JSON_TYPE(m.short_course)='OBJECT'", NO_CHED_MATCH]


def manila_today():
    return datetime.now(MANILA).date()


def years_before(day, years):
    """Same calendar arithmetic as MySQL DATE_SUB(day, INTERVAL n YEAR): 29 Feb -> 28 Feb."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def age_bounds(today):
    """Born after the first date and on or before the second date means age 18-30 inclusive."""
    return years_before(today, AGE_MAX + 1), years_before(today, AGE_MIN)


def age_on(dob, today):
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def valid_academic_year(value):
    match = ACADEMIC_YEAR.fullmatch(value or '')
    return bool(match) and int(match[2]) == int(match[1]) + 1


def parse_ref(ref):
    """(agency, source row ID, course type) from 'CHED:<id>:degree' or 'TESDA:<id>:diploma'."""
    match = SOURCE_REF.fullmatch(ref or '')
    if not match or SOURCES[match[1]]['course_type'] != match[3]:
        raise ValueError('Invalid source reference')
    return match[1], int(match[2]), match[3]


def rules(category, today=None):
    spec = CATEGORIES[category]
    today = today or manila_today()
    items = [{'key': 'source', 'label': 'CHED degree, or TESDA diploma without a CHED match'},
             {'key': 'enrollment_status', 'label': f"Enrollment status {spec['status']}"}]
    if spec['year_level']:
        items.append({'key': 'year_level', 'label': f"Year level {spec['year_level']}"})
    if spec['semester']:
        items.append({'key': 'semester', 'label': f"Semester {spec['semester']}"})
    items += [{'key': 'beneficiary', 'label': 'Active 4Ps member (client status and grade level)'},
              {'key': 'age', 'label': f'Age {AGE_MIN}-{AGE_MAX} as of {today.isoformat()}'},
              {'key': 'payment_history', 'label': 'No recorded payment that blocks this category and period'}]
    return items


def _beneficiary(today):
    oldest, youngest = age_bounds(today)
    return (['EXISTS (SELECT 1 FROM vw_verify_member_bene v WHERE v.member_id=m.member_id)',
             'EXISTS (SELECT 1 FROM tbl_person ap WHERE ap.person_id=m.member_id '
             'AND ap.date_of_birth > %s AND ap.date_of_birth <= %s)'], [oldest, youngest])


def category_clauses(category, today, agency='CHED', *, payments='eligible'):
    """WHERE clauses for one source: fixed category rules, 4Ps/age eligibility and payment history.

    payments='eligible' keeps records no recorded payment blocks; 'blocked' keeps only the blocked
    ones (the separate exclusion count); None leaves payment history out.
    """
    spec, src = CATEGORIES[category], SOURCES[agency]
    clauses = [*src['where'], src['status'] + '=%s']
    params = [spec['status']]
    if spec['year_level']:
        clauses.append(src['year_level'] + '=%s')
        params.append(spec['year_level'])
    if spec['semester']:
        clauses.append(src['semester'] + '=%s')
        params.append(spec['semester'])
    extra, extra_params = _beneficiary(today)
    clauses += extra
    params += extra_params
    if payments:
        sql, history_params = payment_history.blocking_sql(category, 'm.member_id', src['academic_year'], src['semester'])
        clauses.append(sql if payments == 'blocked' else f'NOT {sql}')
        params += history_params
    return clauses, params


def _filters(filters, category, src):
    allowed = CATEGORIES[category]['optional']
    clauses, params = [], []
    for key, column in [('academic_year', src['academic_year']), ('program_name', src['program_name'])]:
        if filters.get(key):
            clauses.append(column + '=%s')
            params.append(filters[key])
    if 'semester' in allowed and filters.get('semester'):
        clauses.append(src['semester'] + '=%s')
        params.append(int(filters['semester']))
    if 'year_level' in allowed and filters.get('year_level'):
        clauses.append(src['year_level'] + '=%s')
        params.append(str(int(filters['year_level'])))
    if filters.get('region_name'):
        clauses.append('EXISTS (SELECT 1 FROM tbl_hh_roster r JOIN tbl_household h ON h.household_id=r.household_id JOIN lib_psgc_denorm g ON g.barangay_id=h.barangay_id WHERE r.person_id=m.member_id AND g.region_name=%s)')
        params.append(filters['region_name'])
    if filters.get('search'):
        pattern = '%' + filters['search'].replace('=', '==').replace('%', '=%').replace('_', '=_') + '%'
        clauses.append(f"(m.tuloy_id LIKE %s ESCAPE '=' OR CAST(m.member_id AS CHAR) LIKE %s ESCAPE '=' OR {src['program_name']} LIKE %s ESCAPE '=')")
        params.extend([pattern] * 3)
    return clauses, params


def _branch(agency, columns, filters, category, today, payments, ids=None):
    src = SOURCES[agency]
    clauses, params = category_clauses(category, today, agency, payments=payments)
    more, more_params = _filters(filters, category, src)
    if ids is not None:
        more.append(f"m.id IN ({','.join(['%s'] * len(ids))})" if ids else '1=0')
        more_params += sorted(ids)
    return f"SELECT {columns(agency, src)} FROM {src['from']} WHERE " + ' AND '.join(clauses + more), params + more_params


def _row_columns(agency, src):
    return (f"{src['order']} AS source_order,'{agency}' AS source_agency,'{src['course_type']}' AS course_type,"
            f"CONCAT('{agency}:',m.id,':{src['course_type']}') AS source_ref,m.id AS source_record_id,m.tuloy_id,"
            f"{src['academic_year']} AS academic_year,{src['semester']} AS semester,{src['year_level']} AS year_level,"
            f"{src['status']} AS enrollment_status,{src['program_name']} AS program_name")


def _id_columns(agency, src):
    return f"{src['order']} AS source_order,'{agency}' AS source_agency,m.id AS source_record_id"


def candidate_query(filters=None, *, category='TK', today=None, count=False, ids_only=False, payments='eligible'):
    """One query over both sources, the same filters for preview, exact count and save-time selection.

    count=True returns one row with total_count. payments='blocked' with count=True counts the
    records the payment history alone excludes. Rows come in stable (agency, source ID) order.
    """
    if category not in CATEGORIES:
        raise ValueError('Unknown category')
    filters = filters or {}
    today = today or manila_today()
    branches = [_branch(agency, _id_columns if (count or ids_only) else _row_columns, filters, category, today, payments)
                for agency in SOURCES]
    params = [p for _, branch_params in branches for p in branch_params]
    if count:
        # Exact total; a timeout is reported as unavailable, never as a capped number.
        return (f'SELECT /*+ MAX_EXECUTION_TIME({PREVIEW_TIMEOUT_MS}) */ '
                + '+'.join(f'(SELECT COUNT(*) FROM ({sql}) b{i})' for i, (sql, _) in enumerate(branches))
                + ' AS total_count'), params
    # The save-time selection may read many more rows than a preview page, so it gets a longer bound.
    timeout = SELECTION_TIMEOUT_MS if ids_only else PREVIEW_TIMEOUT_MS
    union = ' UNION ALL '.join(f'({sql})' for sql, _ in branches)
    return (f'SELECT /*+ MAX_EXECUTION_TIME({timeout}) */ * FROM ({union}) u '
            'ORDER BY u.source_order,u.source_record_id LIMIT %s OFFSET %s'), params


def short_course_query(filters=None, *, today=None):
    """Count of TESDA short courses that would otherwise be in scope (beneficiary, age, region, search)."""
    filters = filters or {}
    clauses, params = _beneficiary(today or manila_today())
    clauses = [*SHORT_COURSE_WHERE, *clauses]
    src = {**SOURCES['TESDA'], 'academic_year': 'NULL', 'semester': 'NULL', 'year_level': 'NULL',
           'program_name': _json('short_course', 'program_name')}
    more, more_params = _filters({k: v for k, v in filters.items() if k in ('region_name', 'search')}, 'TK', src)
    return (f'SELECT /*+ MAX_EXECUTION_TIME({PREVIEW_TIMEOUT_MS}) */ COUNT(*) AS total_count FROM {SHORT_COURSE_FROM} '
            'WHERE ' + ' AND '.join(clauses + more)), params + more_params


def selection_branches(category, today, refs):
    """Per-source SQL selecting exactly these records, still only when currently eligible.

    refs: {agency: {source row IDs}}. Used inside the save transaction.
    """
    out = []
    for agency, src in SOURCES.items():
        ids = refs.get(agency, set())
        if not ids:
            continue
        clauses, params = category_clauses(category, today, agency)
        clauses.append(f"m.id IN ({','.join(['%s'] * len(ids))})")
        params += sorted(ids)
        out.append((agency, src, clauses, params))
    return out
