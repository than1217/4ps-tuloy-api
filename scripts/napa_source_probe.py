"""Read-only NAPA source evidence: schema metadata, reference data and counts.

Uses the configured runtime account, never the migration account. No beneficiary
values are returned. Target assertions prevent accidentally profiling a local DB.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mysql.connector

from config import load_settings
from tunnel import Tunnel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    settings = load_settings()
    if (settings.ods.database, settings.ods.host) != (args.expect_db, args.expect_host):
        raise SystemExit('STOP: configured target differs from expected target')
    tunnel = None
    connection = None
    evidence = {
        'captured_utc': datetime.now(timezone.utc).isoformat(),
        'target': {'host': settings.ods.host, 'database': settings.ods.database},
        'scope': 'metadata, non-personal reference values, aggregate counts only',
        'sample': 'first 200 matched IDs; not a population-wide quality assessment',
        'checks': {},
    }
    queries = {
        'server': 'SELECT DATABASE() AS db, @@hostname AS hostname, @@version AS version',
        'lookup_candidates': """SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,COLUMN_COMMENT
            FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
            AND (TABLE_NAME LIKE 'lib%'
              OR TABLE_NAME REGEXP 'extension|suffix|ethnic|indigen'
              OR COLUMN_NAME REGEXP 'ext.*name|name.*ext|ip_group|ip_label')
            ORDER BY TABLE_NAME,ORDINAL_POSITION""",
        'psgc_candidates': """SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,COLUMN_COMMENT
            FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
              AND (TABLE_NAME IN ('tbl_psgc_brgy','lib_psgc_barangay','lib_psgc_denorm')
                OR TABLE_NAME REGEXP '^tbl_psgc_')
            ORDER BY TABLE_NAME,ORDINAL_POSITION""",
        'library_views': """SELECT TABLE_NAME,VIEW_DEFINITION FROM information_schema.VIEWS
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'lib%'""",
        'engines': """SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN
            ('enroll_matched_CHED_tbl','tbl_person','tbl_hh_roster','tbl_household',
             'tbl_family_assoc_ops','tbl_family_selected','sex_tbl','lib_psgc_denorm')""",
        'sex_reference': 'SELECT id,code,description FROM sex_tbl ORDER BY id LIMIT 20',
        'ods_sex_reference_candidate': """SELECT sex_id,name,code,archived
            FROM consultant_ods.lib_sex_v3 ORDER BY sex_id LIMIT 20""",
        'ods_extension_reference_candidate': """SELECT ext_name_id,name,archived
            FROM consultant_ods.lib_ext_name_v3 ORDER BY ext_name_id LIMIT 200""",
        'ods_ip_reference_candidate': """SELECT ip_group_id,name,code,status,archived
            FROM consultant_ods.lib_ip_group_v3 ORDER BY ip_group_id LIMIT 200""",
        'ods_lookup_sample': """SELECT COUNT(*) AS sampled_rows,
            SUM(s.sex_id IS NULL) AS sex_unmapped,
            SUM(p.ext_name_id IS NOT NULL AND e.ext_name_id IS NULL) AS extension_unmapped,
            SUM(p.ip_group_id IS NOT NULL AND i.ip_group_id IS NULL) AS ip_group_unmapped
            FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            JOIN tbl_person p ON p.person_id=m.member_id
            LEFT JOIN consultant_ods.lib_sex_v3 s ON s.sex_id=p.sex_id
            LEFT JOIN consultant_ods.lib_ext_name_v3 e ON e.ext_name_id=p.ext_name_id
            LEFT JOIN consultant_ods.lib_ip_group_v3 i ON i.ip_group_id=p.ip_group_id""",
        'agency_reference': 'SELECT agency_id,agency_alias FROM lib_partner_agency ORDER BY agency_id LIMIT 50',
        'matched_sample': """SELECT COUNT(*) AS sampled_rows,
            SUM(p.person_id IS NULL) AS missing_person,
            SUM(p.sex_id IS NULL OR sx.id IS NULL) AS unmapped_sex,
            SUM(p.ext_name_id IS NOT NULL) AS extension_id_present,
            SUM(p.ip_group_id IS NOT NULL) AS ip_group_present,
            SUM(m.agency_id IS NULL) AS agency_missing,
            SUM(m.agency_id IS NOT NULL AND a.agency_id IS NULL) AS agency_unmapped,
            SUM(p.first_name IS NULL OR TRIM(p.first_name)='') AS first_name_missing,
            SUM(p.last_name IS NULL OR TRIM(p.last_name)='') AS last_name_missing,
            SUM(p.date_of_birth IS NULL) AS birthdate_missing
            FROM (SELECT id,member_id,agency_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            LEFT JOIN tbl_person p ON p.person_id=m.member_id
            LEFT JOIN sex_tbl sx ON sx.id=p.sex_id
            LEFT JOIN lib_partner_agency a ON a.agency_id=m.agency_id""",
        'roster_sample': """SELECT COUNT(*) AS sampled_rows,
            SUM(rosters=0) AS no_roster,SUM(rosters=1) AS one_roster,SUM(rosters>1) AS multiple_rosters
            FROM (SELECT (SELECT COUNT(*) FROM tbl_hh_roster r WHERE r.person_id=m.member_id) AS rosters
              FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m) x""",
        'external_lookup_metadata': """SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
            AND TABLE_NAME IN ('lib_sex','lib_sex_v2','lib_sex_v3','lib_ext_name','lib_ext_name_v3',
                               'lib_ip_group','lib_ip_group_v3')
            ORDER BY TABLE_SCHEMA,TABLE_NAME,ORDINAL_POSITION LIMIT 500""",
        'source_sex_id_counts': """SELECT p.sex_id,COUNT(*) AS records
            FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            JOIN tbl_person p ON p.person_id=m.member_id GROUP BY p.sex_id""",
        'family_sample_plan': """EXPLAIN SELECT COUNT(*) AS sampled_rows,
            SUM(families=0) AS no_family,SUM(families=1) AS one_family,
            SUM(families>1) AS multiple_families
            FROM (SELECT (SELECT COUNT(*) FROM tbl_family_assoc_ops f
                WHERE f.person_id=m.member_id AND f.archived=0) AS families
              FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m) x""",
        'family_indexes': """SELECT INDEX_NAME,COLUMN_NAME,SEQ_IN_INDEX,NON_UNIQUE
            FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()
            AND TABLE_NAME='tbl_family_assoc_ops' ORDER BY INDEX_NAME,SEQ_IN_INDEX""",
        'selected_family_plan': """EXPLAIN SELECT m.id,fs.family_id,f.fam_assoc_id
            FROM (SELECT id,member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            LEFT JOIN tbl_hh_roster r ON r.person_id=m.member_id
            LEFT JOIN tbl_family_selected fs ON fs.household_id=r.household_id
            LEFT JOIN tbl_family_assoc_ops f ON f.family_id=fs.family_id
              AND f.person_id=m.member_id AND f.hh_roster_id=r.hh_roster_id AND f.archived=0""",
        'selected_family_sample': """SELECT COUNT(*) AS sampled_rows,
            SUM(selected_families=0) AS no_selected_family,
            SUM(selected_families>1) AS multiple_selected_families,
            SUM(confirmed_families=0) AS no_matching_active_association,
            SUM(confirmed_families=1) AS one_matching_active_family,
            SUM(confirmed_families>1) AS multiple_matching_active_families
            FROM (SELECT m.id,COUNT(DISTINCT fs.family_id) AS selected_families,
                COUNT(DISTINCT CASE WHEN f.fam_assoc_id IS NOT NULL THEN fs.family_id END) AS confirmed_families
              FROM (SELECT id,member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
              LEFT JOIN tbl_hh_roster r ON r.person_id=m.member_id
              LEFT JOIN tbl_family_selected fs ON fs.household_id=r.household_id
              LEFT JOIN tbl_family_assoc_ops f ON f.family_id=fs.family_id
                AND f.person_id=m.member_id AND f.hh_roster_id=r.hh_roster_id AND f.archived=0
              GROUP BY m.id) x""",
        'unmapped_ip_id_counts': """SELECT p.ip_group_id,COUNT(*) AS records
            FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            JOIN tbl_person p ON p.person_id=m.member_id
            LEFT JOIN consultant_ods.lib_ip_group_v3 i ON i.ip_group_id=p.ip_group_id
            WHERE p.ip_group_id IS NOT NULL AND i.ip_group_id IS NULL
            GROUP BY p.ip_group_id""",
        'ip_source_definitions': """SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,
                COLUMN_DEFAULT,COLUMN_COMMENT FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA IN (DATABASE(),'consultant_ods','dswd_4ps_datamart_raw')
              AND COLUMN_NAME IN ('ip_label','ip_group_id')
            ORDER BY TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME""",
        'ip_view_metadata': """SELECT TABLE_SCHEMA,TABLE_NAME,
                VIEW_DEFINITION LIKE '%case %' AS has_case_expression
            FROM information_schema.VIEWS
            WHERE TABLE_SCHEMA IN (DATABASE(),'consultant_ods','dswd_4ps_datamart_raw')
              AND (VIEW_DEFINITION LIKE '%ip_label%' OR VIEW_DEFINITION LIKE '%ip_group_id%')""",
        'ip_household_sample': """SELECT h.ip_label,
            CASE WHEN p.ip_group_id IS NULL THEN 'NULL'
                 WHEN p.ip_group_id=0 THEN 'ZERO' ELSE 'NONZERO' END AS person_ip_group_state,
            COUNT(*) AS records
            FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            JOIN tbl_person p ON p.person_id=m.member_id
            LEFT JOIN tbl_hh_roster r ON r.person_id=m.member_id
            LEFT JOIN tbl_household h ON h.household_id=r.household_id
            GROUP BY h.ip_label,person_ip_group_state""",
        'geography_sample': """SELECT COUNT(*) AS joined_rows,
            SUM(h.household_id IS NULL) AS missing_household,
            SUM(h.listahanan_id IS NULL OR TRIM(h.listahanan_id)='') AS missing_listahanan_id,
            SUM(g.barangay_id IS NULL) AS missing_geography,
            SUM(g.region_psa_code IS NULL OR g.region_psa_code NOT REGEXP '^[0-9]{10}$') AS invalid_region_code,
            SUM(g.province_psa_code IS NULL OR g.province_psa_code NOT REGEXP '^[0-9]{10}$') AS invalid_province_code,
            SUM(g.city_psa_name IS NULL OR g.city_psa_name NOT REGEXP '^[0-9]{10}$') AS invalid_city_code,
            SUM(b.psa_code IS NULL OR b.psa_code NOT REGEXP '^[0-9]{10}$') AS invalid_barangay_code,
            SUM(NOT (b.psa_code <=> g.brgy_psa_code)) AS barangay_code_disagreement
            FROM (SELECT member_id FROM enroll_matched_CHED_tbl ORDER BY id LIMIT 200) m
            LEFT JOIN tbl_hh_roster r ON r.person_id=m.member_id
            LEFT JOIN tbl_household h ON h.household_id=r.household_id
            LEFT JOIN lib_psgc_barangay b ON b.barangay_id=h.barangay_id
            LEFT JOIN lib_psgc_denorm g ON g.barangay_id=b.barangay_id""",
    }
    try:
        host, port = settings.ods.host, settings.ods.port
        if settings.ssh_host:
            tunnel = Tunnel(settings, host, port)
            tunnel.start()
            host, port = '127.0.0.1', tunnel.ensure()
        kwargs = dict(host=host, port=port, user=settings.ods.user,
                      password=settings.ods.password, database=settings.ods.database,
                      connection_timeout=8, read_timeout=15, autocommit=False)
        if settings.db_ssl_ca:
            kwargs.update(ssl_ca=settings.db_ssl_ca, ssl_verify_cert=True,
                          ssl_verify_identity=settings.db_ssl_verify_identity and not settings.ssh_host)
        connection = mysql.connector.connect(**kwargs)
        cursor = connection.cursor(dictionary=True)
        cursor.execute('SET SESSION max_execution_time=5000')
        cursor.execute('START TRANSACTION READ ONLY')
        for name, sql in queries.items():
            try:
                cursor.execute(sql)
                evidence['checks'][name] = {'sql': sql, 'rows': cursor.fetchall(), 'status': 'ok'}
            except mysql.connector.Error as exc:
                evidence['checks'][name] = {'sql': sql, 'status': 'failed', 'errno': exc.errno}
        connection.rollback()
    except Exception as exc:
        evidence['connection_error'] = {'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None)}
    finally:
        if connection:
            connection.close()
        if tunnel:
            tunnel.stop()
    args.output.write_text(json.dumps(evidence, indent=2, default=str), encoding='utf8')
    print(json.dumps({name: value['status'] for name, value in evidence['checks'].items()}))
    print(f'Evidence: {args.output}')
    return int('connection_error' in evidence or any(v['status'] != 'ok' for v in evidence['checks'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
