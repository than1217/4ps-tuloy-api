"""Read-only BCMD release preflight. No schema, grant, batch or flag changes."""
from dataclasses import replace
import json

from mysql.connector import Error

from bcmd_readiness import verify
from config import ConfigError, load_settings
from database import close, get_bcmd_connection
from errors import ApiError
from routers.bcmd import validate_local_candidate


def main():
    connection = None
    try:
        settings = replace(load_settings(), bcmd_enabled=True)
        validate_local_candidate(settings)
        connection = get_bcmd_connection(settings)
        db = next(connection)
        verify(db, settings)
        print(json.dumps({'result': 'PASS', 'identity': 'MATCH', 'ledger': 'MATCH',
                          'schema': 'MATCH', 'runtime_privileges': (
                              'EXISTING_ACCOUNT_REQUIRED_RIGHTS' if settings.bcmd_use_existing_account
                              else 'LEAST_PRIVILEGE'),
                          'export_enabled': False}))
        return 0
    except (ConfigError, ApiError, Error):
        print(json.dumps({'result': 'STOP', 'reason': 'CONFIG_OR_READINESS_CHECK_FAILED'}))
        return 1
    finally:
        if connection is not None:
            connection.close()
        close()


if __name__ == '__main__':
    raise SystemExit(main())
