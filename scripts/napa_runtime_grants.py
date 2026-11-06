"""Print guarded least-privilege grants for DBA review. Never connects or applies.

The DBA must create a separate account and configure its password out of band.
This does not revoke existing rights: preflight rejects any excessive rights.
"""
import argparse
import re

from bcmd_readiness import READ_TABLES, WRITE_TABLES, UPDATE_TABLES


def render(user, account_host, database, hostname):
    if not re.fullmatch(r'[A-Za-z0-9_]{1,32}', user):
        raise ValueError('Invalid account name')
    if not re.fullmatch(r'[A-Za-z0-9_.:%/-]{1,255}', account_host):
        raise ValueError('Invalid account host')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', database):
        raise ValueError('Invalid database name')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,255}', hostname):
        raise ValueError('Invalid server hostname')
    account = f"'{user}'@'{account_host}'"
    grants = [f"GRANT SELECT{', INSERT' if t in WRITE_TABLES or t == 'bcmd_batch_export' else ''}{', UPDATE' if t in UPDATE_TABLES else ''} ON `{database}`.`{t}` TO {account}"
              for t in READ_TABLES]
    lines = ['-- REVIEW ONLY. DBA must provision the account first; no passwords in this file.',
             '-- DCL commits implicitly. Do not treat this as a rollback-able transaction.',
             f"SET @napa_grant_target=(DATABASE()='{database}' AND @@hostname='{hostname}');"]
    for grant in grants:
        escaped = grant.replace("'", "''")
        lines += [f"SET @napa_grant_sql=IF(@napa_grant_target,'{escaped}','SELECT ''STOP_WRONG_TARGET'' AS result');",
                  'PREPARE napa_grant_stmt FROM @napa_grant_sql;',
                  'EXECUTE napa_grant_stmt;', 'DEALLOCATE PREPARE napa_grant_stmt;']
    lines += ["SELECT IF(@napa_grant_target,'GRANTS_SENT_RUN_PREFLIGHT','STOP_WRONG_TARGET') AS result;"]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--account-host', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--hostname', required=True)
    args = parser.parse_args()
    try:
        print(render(args.user, args.account_host, args.database, args.hostname), end='')
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
