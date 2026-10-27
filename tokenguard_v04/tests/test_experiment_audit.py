"""Audit faults cannot turn a partial task experiment into a complete comparison."""
import csv
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from tokenguard import audit, experiment
from tokenguard.experiment_report import report


def prepared(root: Path) -> Path:
    project = root / 'project'
    project.mkdir()
    (project / 'arithmetic.py').write_text('def divide(a, b):\n    return a // b\n', encoding='utf-8')
    (project / 'test_arithmetic.py').write_text(
        'from arithmetic import divide\ndef test_division():\n    assert divide(3, 2) == 1.5\n', encoding='utf-8')
    spec = {'schema_version': 1, 'task_id': 'audit-fixture', 'provider': 'mock', 'model': 'mock',
            'prompt': 'Local fixed mock edit; no model requests.',
            'files': ['arithmetic.py', 'test_arithmetic.py'], 'editable_files': ['arithmetic.py'],
            'test_files': ['test_arithmetic.py'], 'expected_tests': 1, 'timeout_seconds': 30,
            'mock_edits': [{'path': 'arithmetic.py', 'old': 'a // b', 'new': 'a / b'}]}
    path = root / 'task.json'
    path.write_text(json.dumps(spec), encoding='utf-8')
    return experiment.prepare(project, path, root / 'output')


@pytest.mark.parametrize('failure', ['truncated', 'exception'])
def test_audit_fault_keeps_code_validation_and_unrun_denominator(failure):
    real_summary = audit.summary

    def broken_summary(*args, **kwargs):
        if failure == 'exception':
            raise OSError('private detail must not be reported')
        data = Path(os.environ['TOKENGUARD_DATA_DIR'])
        data.mkdir(exist_ok=True)
        (data / 'audit-v04.jsonl').write_bytes(b'{"event":')
        return real_summary(*args, **kwargs)

    with tempfile.TemporaryDirectory(prefix='tg-task-audit-') as temporary:
        folder = prepared(Path(temporary).resolve())
        with patch('tokenguard.experiment.audit.summary', side_effect=broken_summary):
            execution = experiment.execute(folder, driver='mock', execute_run=True)
        assert execution['completed_trials'] == 1
        assert execution['planned_trials'] == 2
        result = report(folder)
        assert result['prepared_trials'] == 2
        assert result['completed_trials'] == 1
        assert result['successful_trials'] == 0
        baseline, optimized = result['runs']
        assert baseline['tests_passed'] is True
        assert baseline['final_validation']['tests'] == 1
        assert baseline['compression']['data_available'] is False
        assert baseline['stage_errors'] == [{'stage': 'audit', 'type':
                                            'AuditIntegrityError' if failure == 'truncated' else 'OSError'}]
        assert optimized['status'] == 'not_run'
        assert not (folder / 'trials/001-optimized/driver').exists()
        assert (folder / 'execution-stopped.json').exists()
        assert 'private detail' not in (folder / 'report.json').read_text(encoding='utf-8')
        with (folder / 'report.csv').open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert rows[0]['compression_data_available'] == 'False'
        markdown = (folder / 'report.md').read_text(encoding='utf-8')
        assert 'Audit complete' in markdown
        assert 'partial observations' in markdown


def test_delivery_unknown_alone_does_not_block_fixed_mock_task():
    real_summary = audit.summary

    def unknown_delivery(*args, **kwargs):
        return {**real_summary(*args, **kwargs), 'delivery_data_available': False}

    with tempfile.TemporaryDirectory(prefix='tg-task-audit-') as temporary:
        folder = prepared(Path(temporary).resolve())
        with patch('tokenguard.experiment.audit.summary', side_effect=unknown_delivery):
            execution = experiment.execute(folder, driver='mock', execute_run=True)
        assert execution['completed_trials'] == 2
        assert report(folder)['successful_trials'] == 2
