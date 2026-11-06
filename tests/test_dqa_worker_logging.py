"""Processing failures remain diagnosable without leaking source/driver text."""
import json
import logging

import bcmd_dqa as dqa
from observability import JsonFormatter


def test_processing_error_logs_safe_context_and_preserves_retry(monkeypatch, caplog):
    run = {'public_id': 'synthetic-run', 'attempts': 2, 'processed': 0,
           'status': 'RUNNING', 'request_id': 'synthetic-request'}
    monkeypatch.setattr(dqa, 'claim', lambda db: run)
    retried = []
    monkeypatch.setattr(dqa, 'failed', lambda db, value: retried.append(value))

    def broken(*args):
        error = RuntimeError('private beneficiary name password=do-not-log')
        error.errno = 1146
        raise error

    monkeypatch.setattr(dqa, 'process_chunk', broken)
    with caplog.at_level(logging.ERROR, logger='tuloy.dqa'):
        assert dqa.work_once(object()) is True
    assert retried == [run]
    record = next(r for r in caplog.records if r.message == 'dqa_processing_failed')
    output = JsonFormatter().format(record)
    data = json.loads(output)
    assert data['run_id'] == 'synthetic-run'
    assert data['attempt'] == 2 and data['errno'] == 1146
    assert data['dqa_request_id'] == 'synthetic-request'
    assert data['exc_type'] == 'RuntimeError' and data['exc_frames']
    assert 'private beneficiary' not in output and 'do-not-log' not in output


def test_connection_error_logs_without_run(caplog):
    with caplog.at_level(logging.ERROR, logger='tuloy.dqa'):
        dqa.log_worker_error('bcmd_dqa_worker_unavailable', ValueError('secret detail'))
    data = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert data['event'] == 'bcmd_dqa_worker_unavailable'
    assert data['exc_type'] == 'ValueError'
    assert 'run_id' not in data
    assert 'secret detail' not in str(data)
