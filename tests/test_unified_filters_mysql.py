"""Real MySQL JSON/filter behavior in disposable synthetic schemas only."""
import json
import mysql.connector
from test_bcmd_mysql import mysql_candidate
from routers.beneficiaries import UNIFIED_MATCHED_COLUMNS, unified_query
from routers.staff import UNIFIED_OVERVIEW_COUNTS


def test_global_agency_course_search_and_literal_wildcards(mysql_candidate):
    db = mysql.connector.connect(**mysql_candidate)
    cur = db.cursor(dictionary=True)
    try:
        cur.execute('CREATE TABLE unified_fixture (agency_name varchar(5),tuloy_id varchar(50),member_id int,'
                    'enrollment_details json,created_at timestamp NULL,updated_at timestamp NULL)')
        cur.execute(f'CREATE VIEW vw_unified_matched_enrollment AS SELECT {UNIFIED_MATCHED_COLUMNS} FROM unified_fixture')
        for agency, member, kind, program in [('CHED', 1, 'degree', 'BS Synthetic'),
                ('TESDA', 2, 'diploma', 'Synthetic Diploma'), ('TESDA', 2, 'short_course', 'A_100%'),
                ('TESDA', 3, 'short_course', 'Aq100x')]:
            course = {'program_name': program}
            details = {kind: [course] if kind == 'short_course' else course}
            cur.execute('INSERT INTO unified_fixture VALUES (%s,%s,%s,%s,%s,NULL)',
                        (agency, f'SYN-{member}', member, json.dumps(details), '2026-10-30 00:00:00'))
        db.commit()
        def rows(agency=None, kind=None, search='', offset=0, limit=50):
            sql, params = unified_query(agency, kind, search)
            cur.execute(sql, (*params, limit, offset))
            return cur.fetchall()
        cur.execute(UNIFIED_OVERVIEW_COUNTS)
        assert cur.fetchone() == {'total_records': 4, 'ched_records': 1, 'tesda_records': 3, 'distinct_tuloy_ids': 3, 'ched_beneficiaries': 1, 'tesda_beneficiaries': 2}
        cur.execute("UPDATE unified_fixture SET created_at='2026-10-31 00:00:00' WHERE member_id=3")
        sql, params = unified_query('TESDA', recent=True)
        cur.execute(sql, (*params, 1, 0))
        assert cur.fetchone()['member_id'] == 3
        assert len(rows()) == 4
        assert len(rows('TESDA')) == 3
        assert len(rows('TESDA', 'short_course')) == 2
        assert len(rows('CHED', 'short_course')) == 0
        assert len(rows(search='A_100%')) == 1
        assert len(rows(search="' OR 1=1 --")) == 0
        assert rows('TESDA', 'diploma')[0]['member_id'] == 2
        assert len(rows('TESDA', offset=1, limit=1)) == 1
    finally:
        cur.close()
        db.close()
