"""Context records must not lose matches when a page splits their keywords."""
import io
from pathlib import Path
import random
import re
import subprocess
import sys

import pytest

from tokenguard.audit import events
from tokenguard.storage import artifact_path, grep_lines, save_original


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('TOKENGUARD_INDEX_ENABLED', '1')


def matched_lines(result, pattern):
    body = result.split('\n[TokenGuard: retrieval limit reached;', 1)[0]
    observed = set()
    for text in io.StringIO(body, newline=''):
        match = re.match(r'^(\d+): (?:\[char \d+\] )?(.*)', text)
        if match and pattern.casefold() in match[2].casefold():
            observed.add(int(match[1]))
    return observed


@pytest.mark.parametrize('ending', ['\n', '\r', '\r\n'])
@pytest.mark.parametrize('context', [1, 2, 5])
@pytest.mark.parametrize('phrase,pattern', [('IMPORTANT_FAILURE', 'important_failure'),
                                           ('Straße', 'STRASSE'),
                                           ('İstanbul', 'i\u0307stanbul')])
def test_context_keyword_split_by_marker_remains_reachable(ending, context, phrase, pattern):
    raw = phrase + ending + 'x' * (22 - len(phrase)) + phrase + 'x' * 100 + ending
    identifier = save_original(raw)
    first = grep_lines(identifier, pattern, context=context, limit=1, max_chars=110)
    row = events()[0][-1]
    assert row['truncated'] is True
    assert len(first) <= 110
    observed = matched_lines(first, pattern)
    cursor = (row['next_start_line'], row['next_start_char'])
    assert cursor > (1, 0)
    for _ in range(10):
        following = grep_lines(identifier, pattern, context=context, limit=1,
                               max_chars=110, start=cursor[0], char_offset=cursor[1])
        assert len(following) <= 110
        observed.update(matched_lines(following, pattern))
        row = events()[0][-1]
        if not row['truncated']:
            break
        next_cursor = (row['next_start_line'], row['next_start_char'])
        assert next_cursor > cursor
        cursor = next_cursor
    else:
        pytest.fail('Context recovery did not finish')
    assert observed == {1, 2}
    assert artifact_path(identifier).read_bytes() == raw.encode('utf-8')


def test_cli_default_budget_preserves_second_context_match():
    pattern = 'IMPORTANT_FAILURE'
    raw = pattern + '\n' + 'x' * 11890 + pattern + 'y' * 100 + '\n'
    identifier = save_original(raw)
    cli = Path(__file__).resolve().parents[1] / 'tg.py'
    first = subprocess.run([sys.executable, '-X', 'utf8', str(cli), 'grep', identifier,
                            pattern, '--context', '1', '--limit', '1'],
                           capture_output=True, timeout=30)
    assert first.returncode == 0, first.stderr
    text = first.stdout.decode('utf-8')
    row = events()[0][-1]
    following = ''
    if row['truncated']:
        second = subprocess.run([sys.executable, '-X', 'utf8', str(cli), 'grep', identifier,
                                 pattern, '--context', '1', '--limit', '1',
                                 '--start', str(row['next_start_line']),
                                 '--start-char', str(row['next_start_char'])],
                                capture_output=True, timeout=30)
        assert second.returncode == 0, second.stderr
        following = second.stdout.decode('utf-8')
    assert matched_lines(text, pattern) | matched_lines(following, pattern) == {1, 2}
    assert max(len(text), len(following)) <= 12000
    assert artifact_path(identifier).read_bytes() == raw.encode('utf-8')


@pytest.mark.parametrize('context', [1, 2, 5])
def test_context_matches_do_not_extend_requested_tail_after_match_limit(context):
    raw = 'NEEDLE\r\n' * 12
    identifier = save_original(raw)
    result = grep_lines(identifier, 'needle', context=context, limit=1)
    body = result.split('\n[TokenGuard: retrieval limit reached;', 1)[0]
    assert body == ''.join(f'{n}: NEEDLE\r\n' for n in range(1, context + 2))
    row = events()[0][-1]
    assert row['truncated'] is True
    assert (row['next_start_line'], row['next_start_char']) == (context + 2, 0)


@pytest.mark.parametrize('seed', range(24))
def test_dense_matching_context_pages_agree_with_independent_line_oracle(seed):
    generator = random.Random(104729 + seed)
    phrase, pattern = generator.choice([('IMPORTANT_FAILURE', 'important_failure'),
                                         ('Straße', 'STRASSE'), ('İstanbul', 'i\u0307stanbul')])
    records = []
    for index in range(20):
        text = generator.choice(['x', '原', '😀']) * generator.randrange(5, 601)
        if index in (0, 19) or generator.randrange(5):
            text += phrase
        records.append(text + generator.choice(['\n', '\r', '\r\n']))
    raw = ''.join(records)
    expected = {n for n, text in enumerate(io.StringIO(raw, newline=''), 1)
                if pattern.casefold() in text.casefold()}
    identifier = save_original(raw)
    budget = generator.randrange(240, 401)
    context = generator.choice([1, 2, 5])
    limit = generator.choice([1, 2, 3])
    cursor = (1, 0)
    observed = set()
    for _ in range(120):
        result = grep_lines(identifier, pattern, context=context, limit=limit,
                            max_chars=budget, start=cursor[0], char_offset=cursor[1])
        assert len(result) <= budget
        observed.update(matched_lines(result, pattern))
        row = events()[0][-1]
        assert row['returned_chars'] == len(result)
        if not row['truncated']:
            assert observed == expected
            break
        following = (row['next_start_line'], row['next_start_char'])
        assert following > cursor
        cursor = following
    else:
        pytest.fail('Dense context pagination did not terminate')
    assert artifact_path(identifier).read_bytes() == raw.encode('utf-8')


@pytest.mark.parametrize('start', [9, 99, 999])
@pytest.mark.parametrize('phrase,pattern', [('NEEDLE', 'needle'), ('Straße', 'STRASSE'),
                                           ('İstanbul', 'i\u0307stanbul')])
def test_marker_digit_growth_cannot_displace_first_complete_match(start, phrase, pattern):
    first = '😀' * 249 + phrase + '\n'
    raw = ('skip\n' * (start - 1) + first + 'x' * 388 + phrase + '\r\n'
           + 'z' * 128 + '\r' + 'last\n')
    # At this budget the first keyword fits with its own cursor, but a cursor
    # in the next line has another line-number digit. Derive fixture sizes from
    # public marker syntax, without calling the implementation's budget helper.
    later_marker = (f'\n[TokenGuard: retrieval limit reached; next_start_line={start + 1}; '
                    'next_start_char=388]\n')
    required = len(f'{start}: ') + len(first) - 1
    budget = required + len(later_marker) - 1
    result = grep_lines(save_original(raw), pattern, start=start, limit=3,
                        context=2, max_chars=budget)
    assert phrase in result
    assert matched_lines(result, pattern) >= {start}
    assert len(result) <= budget
    row = events()[0][-1]
    assert row['truncated'] is True
    assert row['windowed_matches'] >= 1
    assert (row['next_start_line'], row['next_start_char']) > (start, 0)
