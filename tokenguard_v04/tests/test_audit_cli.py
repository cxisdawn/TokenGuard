"""Bounded reports retain aggregate scope and expose unknown/incomplete evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tokenguard import audit


CLI = Path(__file__).resolve().parents[1] / 'tg.py'


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path))
    return tmp_path


def stats(*args):
    return subprocess.run([sys.executable, '-X', 'utf8', str(CLI), 'stats', *args],
                          capture_output=True, text=True, encoding='utf-8', timeout=15)


def add(index, source='manual'):
    audit.append({'event': 'compression', 'compression_id': f'c{index}',
                  'session_id': 's', 'source': source, 'original_chars': 1000,
                  'optimized_chars': 100})


def test_default_bounds_records_but_not_totals(isolated):
    for index in range(25):
        add(index)
    proc = stats('--session', 's', '--json')
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result['compressions'] == 25 and result['saved_chars'] == 22500
    assert result['records_total'] == 25 and result['records_omitted'] == 5
    assert len(result['records']) == 20
    assert result['records'][0]['compression_id'] == 'c5'


def test_source_filter_and_aggregate_only_output(isolated):
    add(1)
    add(2, 'claude_hook')
    result = json.loads(stats('--source', 'claude_hook', '--limit-records', '0', '--json').stdout)
    assert result['compressions'] == 1 and result['scope'] == 'filtered'
    assert result['records'] == [] and result['records_omitted'] == 1


def test_human_report_missing_estimates_and_unconfirmed_delivery(isolated):
    add(1)
    proc = stats()
    assert proc.returncode == 0
    assert 'estimate: unavailable' in proc.stdout
    assert 'Prepared candidates: 1' in proc.stdout
    assert 'stdout succeeded: 0; failed: 0; unknown: 1' in proc.stdout
    assert 'host acceptance unverified' in proc.stdout


def test_human_report_discloses_corruption(isolated):
    add(1)
    with (isolated / 'audit-v04.jsonl').open('ab') as stream:
        stream.write(b'\xff\n')
    proc = stats()
    assert proc.returncode == 0
    assert 'INCOMPLETE' in proc.stdout and 'malformed=1' in proc.stdout
    assert 'Traceback' not in proc.stderr


@pytest.mark.parametrize('limit', ['-1', '101'])
def test_cli_record_limit_is_bounded(isolated, limit):
    assert stats('--limit-records', limit, '--json').returncode == 2
