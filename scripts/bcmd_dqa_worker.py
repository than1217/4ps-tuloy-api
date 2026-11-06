"""Run the durable DQA worker: python -m scripts.bcmd_dqa_worker [--once].

Uses the normal BCMD connection and readiness checks. Never performs migrations,
grants, approval writes or external agency calls. Stop with Ctrl+C.
"""
import argparse
import time

from config import load_settings
from database import get_bcmd_connection
from bcmd_readiness import verify
from bcmd_dqa import work_once, log_worker_error
from observability import configure_logging


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.bcmd_enabled:
        raise SystemExit('BCMD is disabled')
    from routers.bcmd import validate_local_candidate
    validate_local_candidate(settings)
    while True:
        connections = get_bcmd_connection(settings)
        try:
            db = next(connections)
            verify(db, settings)
            worked = work_once(db)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log_worker_error('bcmd_dqa_worker_unavailable', exc)
            if args.once:
                raise SystemExit(1) from None
            worked = False
        finally:
            connections.close()
        if args.once:
            return
        if not worked:
            time.sleep(1)


if __name__ == '__main__':
    main()
