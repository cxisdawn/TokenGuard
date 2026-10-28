"""A scan budget bounds work; it is not evidence of unexamined content."""
import io
from pathlib import Path
import random
import re
import subprocess
import sys
from unittest.mock import patch

import pytest

from tokenguard import storage
from tokenguard.audit import events


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('TOKENGUARD_INDEX_ENABLED', '1')


@pytest.mark.parametrize('ending', ['', '\n', '\r', '\r\n'])
@pytest.mark.parametrize('mode', ['show', 'grep_hit', 'grep_miss'])
def test_exact_scan_budget_at_eof_is_complete(ending, mode):
    raw = 'NEEDLE ordinary' + ending
    identifier = storage.save_original(raw)
    with patch('tokenguard.storage.MAX_SCAN', len(raw)):
        if mode == 'show':
            result = storage.read_lines(identifier, 1, 100, max_chars=200)
            expected = raw
        else:
            pattern = 'needle' if mode == 'grep_hit' else 'absent'
            result = storage.grep_lines(identifier, pattern, context=0, max_chars=200)
            expected = '1: ' + raw if mode == 'grep_hit' else '(no matches)\n'
    assert result == expected
    row = events()[0][-1]
    assert row['truncated'] is False
    assert row['next_start_line'] is None and row['next_start_char'] is None
    assert row['scanned_chars'] == len(raw)
    assert row['returned_chars'] == len(expected)


@pytest.mark.parametrize('ending', ['', '\n', '\r', '\r\n'])
@pytest.mark.parametrize('context', [0, 1, 5])
def test_earlier_hit_and_tail_at_exact_scan_eof_match_independent_oracle(ending, context):
    raw = 'NEEDLE\r\nordinary\nlast' + ending
    lines = io.StringIO(raw, newline='').readlines()
    expected = ''.join(f'{n}: {line}' for n, line in enumerate(lines[:context + 1], 1))
    with patch('tokenguard.storage.MAX_SCAN', len(raw)):
        result = storage.grep_lines(storage.save_original(raw), 'needle', context=context)
    assert result == expected
    assert events()[0][-1]['truncated'] is False


@pytest.mark.parametrize('ending', ['', '\n', '\r', '\r\n'])
def test_exact_scan_eof_can_return_complete_small_no_match_status(ending):
    raw = 'ordinary' + ending
    expected = '(no matches)\n'
    with patch('tokenguard.storage.MAX_SCAN', len(raw)):
        result = storage.grep_lines(storage.save_original(raw), 'absent',
                                    max_chars=len(expected))
    assert result == expected
    assert events()[0][-1]['truncated'] is False


def test_actual_cli_default_eight_mib_scan_boundary_is_complete():
    # Exercise the production bound in another process, without patching it.
    assert storage.MAX_SCAN == 8 * 1024 * 1024
    identifier = storage.save_original('x' * storage.MAX_SCAN)
    cli = Path(__file__).resolve().parents[1] / 'tg.py'
    result = subprocess.run([sys.executable, '-X', 'utf8', str(cli), 'grep',
                             identifier, 'absent', '--context', '0', '--max-chars', '13'],
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'(no matches)\n'
    row = events()[0][-1]
    assert row['truncated'] is False
    assert row['scanned_chars'] == storage.MAX_SCAN
    assert row['returned_chars'] == len(result.stdout)


@pytest.mark.parametrize('ending', ['\n', '\r', '\r\n'])
@pytest.mark.parametrize('mode', ['show', 'grep'])
def test_exact_scan_budget_with_unexamined_tail_preserves_reachable_cursor(ending, mode):
    first = 'ordinary 😀' + ending
    tail = 'NEEDLE_FAILURE\nlast\n'
    identifier = storage.save_original(first + tail)
    with patch('tokenguard.storage.MAX_SCAN', len(first)):
        result = (storage.read_lines(identifier, 1, 100) if mode == 'show' else
                  storage.grep_lines(identifier, 'NEEDLE_FAILURE', context=0))
    row = events()[0][-1]
    assert row['truncated'] is True
    assert row['scanned_chars'] == len(first) + 1
    assert (row['next_start_line'], row['next_start_char']) == (2, 0)
    assert 'next_start_line=2; next_start_char=0' in result
    if mode == 'show':
        assert result.split('\n[TokenGuard:', 1)[0] == first
        recovered = storage.read_lines(identifier, 2, 100)
        assert recovered == tail
    else:
        recovered = storage.grep_lines(identifier, 'NEEDLE_FAILURE', start=2, context=0)
        assert recovered == '2: NEEDLE_FAILURE\n'
    assert events()[0][-1]['truncated'] is False


@pytest.mark.parametrize('ending', ['', '\n', '\r', '\r\n'])
@pytest.mark.parametrize('mode', ['show', 'grep'])
def test_scan_probe_is_included_in_verified_byte_range(ending, mode):
    first = 'ordinary 😀' + ending
    tail = 'z' if ending else ''
    identifier = storage.save_original(first + tail)
    with patch('tokenguard.storage.MAX_SCAN', len(first)), \
            patch('tokenguard.storage.verify_range', wraps=storage.verify_range) as verify:
        if mode == 'show':
            storage.read_lines(identifier, 1, 100)
        else:
            storage.grep_lines(identifier, 'absent', context=0)
    _, _, start_byte, end_byte = verify.call_args.args
    assert start_byte == 0
    assert end_byte == len((first + tail).encode('utf-8'))


@pytest.mark.parametrize('seed', range(16))
def test_seeded_grep_pages_reach_all_independently_identified_matching_lines(seed):
    generator = random.Random(65537 + seed)
    phrase, pattern = generator.choice([('NEEDLE_FAILURE', 'needle_failure'),
                                         ('Straße', 'STRASSE'), ('İstanbul', 'i\u0307stanbul')])
    records = []
    for _ in range(18):
        text = generator.choice(['x', '原', '😀']) * generator.randrange(1, 700)
        if generator.randrange(3) == 0:
            text += phrase
        records.append(text + generator.choice(['\n', '\r', '\r\n']))
    raw = ''.join(records)
    # Independent physical-line oracle; do not use the recovery matcher/cursor
    # implementation to decide which original lines must remain reachable.
    lines = io.StringIO(raw, newline='').readlines()
    expected = {n for n, text in enumerate(lines, 1) if pattern.casefold() in text.casefold()}
    identifier = storage.save_original(raw)
    cursor = (1, 0)
    observed = set()
    context = generator.randrange(3)
    budget = generator.randrange(260, 501)
    for _ in range(100):
        result = storage.grep_lines(identifier, pattern, context=context, limit=2,
                                    start=cursor[0], char_offset=cursor[1], max_chars=budget)
        assert len(result) <= budget
        body = result.split('\n[TokenGuard: retrieval limit reached;', 1)[0]
        for text in io.StringIO(body, newline=''):
            match = re.match(r'^(\d+): (?:\[char \d+\] )?(.*)', text)
            if match and pattern.casefold() in match[2].casefold():
                observed.add(int(match[1]))
        row = events()[0][-1]
        if not row['truncated']:
            assert observed == expected
            break
        following = (row['next_start_line'], row['next_start_char'])
        assert following > cursor
        cursor = following
    else:
        pytest.fail('Grep pages did not finish within the bounded fixture')


@pytest.mark.parametrize('ending', ['\n', '\r', '\r\n'])
@pytest.mark.parametrize('phrase,pattern', [('NEEDLE_FAILURE', 'needle_failure'),
                                           ('Straße', 'STRASSE'),
                                           ('İstanbul', 'i\u0307stanbul')])
def test_matching_line_that_fits_alone_can_window_when_tail_requires_marker(ending, phrase, pattern):
    first = 'x' * 165 + phrase + ending
    raw = first + 'y' * 400 + '\n' + phrase + '\n'
    identifier = storage.save_original(raw)
    assert len('1: ' + first) < 200
    result = storage.grep_lines(identifier, pattern, context=1, max_chars=200)
    assert phrase in result
    assert '[char ' in result
    assert len(result) <= 200
    row = events()[0][-1]
    assert row['truncated'] is True
    assert row['windowed_matches'] == 1
    assert (row['next_start_line'], row['next_start_char']) > (1, 0)


def test_cli_marker_window_preserves_casefold_match_and_original_bytes():
    raw = 'x' * 165 + 'Straße\r\n' + 'y' * 400 + '\nStraße\n'
    identifier = storage.save_original(raw)
    cli = Path(__file__).resolve().parents[1] / 'tg.py'
    result = subprocess.run([sys.executable, '-X', 'utf8', str(cli), 'grep', identifier,
                             'STRASSE', '--context', '1', '--max-chars', '200'],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    text = result.stdout.decode('utf-8')
    assert 'Straße' in text and '[char ' in text
    assert 'retrieval limit reached' in text
    assert len(text) <= 200
    assert storage.artifact_path(identifier).read_bytes() == raw.encode('utf-8')
    assert events()[0][-1]['returned_chars'] == len(text)
