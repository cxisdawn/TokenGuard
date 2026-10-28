"""Exact-fit results are complete; budget errors never become clipped success."""
import io
from pathlib import Path
import random
import subprocess
import sys
from unittest.mock import patch

import pytest

from tokenguard.audit import events
from tokenguard.storage import grep_lines, read_lines, save_original, RecoveryLimitError


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKENGUARD_DATA_DIR', str(tmp_path/'data'))
    monkeypatch.setenv('TOKENGUARD_INDEX_ENABLED', '1')


@pytest.mark.parametrize('raw', ['x', '😀', 'x'*99+'\n', '原文😀\r\n', 'one\rsecond\nlast'])
def test_show_exact_fit_returns_complete_content_without_cursor(raw):
    identifier=save_original(raw)
    result=read_lines(identifier, 1, 100, max_chars=len(raw))
    assert result == raw
    row=events()[0][-1]
    assert row['truncated'] is False
    assert row['next_start_line'] is None and row['next_start_char'] is None
    assert row['returned_chars'] == len(result)


@pytest.mark.parametrize('ending', ['', '\n', '\r', '\r\n'])
def test_grep_exact_fit_returns_complete_matching_line(ending):
    raw='NEEDLE'+ending
    expected='1: '+raw
    result=grep_lines(save_original(raw), 'needle', context=0, limit=1, max_chars=len(expected))
    assert result == expected
    assert events()[0][-1]['truncated'] is False


@pytest.mark.parametrize('mode', ['show', 'grep'])
def test_exact_fit_requested_end_does_not_require_unrequested_tail(mode):
    first='NEEDLE '+'x'*90+'\r\n'
    identifier=save_original(first+'not requested\n')
    if mode=='show':
        result=read_lines(identifier,1,1,max_chars=len(first))
        expected=first
    else:
        expected='1: '+first
        result=grep_lines(identifier,'needle',context=0,end=1,max_chars=len(expected))
    assert result == expected
    row=events()[0][-1]
    assert row['truncated'] is False
    assert row['scanned_chars'] == len(first)


@pytest.mark.parametrize('mode', ['show', 'grep'])
def test_same_budget_keeps_continuation_when_requested_tail_remains(mode):
    first='NEEDLE '+'x'*90+'\n'
    identifier=save_original(first+'NEEDLE later\n')
    if mode=='show':
        result=read_lines(identifier,1,2,max_chars=len(first))
    else:
        result=grep_lines(identifier,'needle',context=0,max_chars=len('1: '+first))
    row=events()[0][-1]
    assert row['truncated'] is True
    assert (row['next_start_line'],row['next_start_char']) > (1,0)
    assert 'retrieval limit reached' in result


@pytest.mark.parametrize('mode', ['show', 'grep'])
@pytest.mark.parametrize('budget', [1, 4, 11])
def test_incomplete_empty_status_is_rejected_without_success_audit(mode,budget):
    identifier=save_original('ordinary\n')
    with pytest.raises(RecoveryLimitError,match='max_chars'):
        if mode=='show':read_lines(identifier,2,1,max_chars=budget)
        else:grep_lines(identifier,'absent',max_chars=budget)
    assert events()[0] == []


@pytest.mark.parametrize('mode', ['show', 'grep'])
def test_full_empty_status_fits_exactly(mode):
    identifier=save_original('ordinary\n')
    expected='(no matches)\n'
    result=(read_lines(identifier,2,1,max_chars=len(expected)) if mode=='show' else
            grep_lines(identifier,'absent',max_chars=len(expected)))
    assert result == expected
    assert events()[0][-1]['truncated'] is False


def test_seeded_show_windows_match_independent_text_line_oracle():
    generator=random.Random(8191)
    for _ in range(40):
        raw=''.join(generator.choice(['a','原','😀','\u2028'])*generator.randrange(1,12)
                    +generator.choice(['\r','\n','\r\n']) for _ in range(6))+'last'
        # StringIO models only CR/LF text boundaries, including retained CRLF.
        lines=io.StringIO(raw,newline='').readlines()
        start=generator.randint(1,len(lines))
        count=generator.randint(1,len(lines)-start+1)
        expected=''.join(lines[start-1:start-1+count])
        result=read_lines(save_original(raw),start,count,max_chars=len(expected))
        assert result == expected
        assert events()[0][-1]['truncated'] is False


@pytest.mark.parametrize('mode', ['show','grep'])
def test_cli_tiny_no_match_budget_has_no_stdout_or_success_record(mode):
    identifier=save_original('ordinary\n')
    cli=Path(__file__).resolve().parents[1]/'tg.py'
    args=[sys.executable,'-X','utf8',str(cli),mode,identifier]
    args+=['--start','2'] if mode=='show' else ['absent']
    result=subprocess.run([*args,'--max-chars','1'],capture_output=True,timeout=30)
    assert result.returncode == 2
    assert result.stdout == b''
    assert b'max_chars' in result.stderr
    assert events()[0] == []


def test_cli_exact_one_unicode_character_is_delivered_and_audited():
    identifier=save_original('😀')
    cli=Path(__file__).resolve().parents[1]/'tg.py'
    result=subprocess.run([sys.executable,'-X','utf8',str(cli),'show',identifier,
                           '--count','1','--max-chars','1'],capture_output=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '😀'.encode('utf-8')
    assert len(events()[0]) == 1
    assert events()[0][0]['returned_chars'] == 1


@pytest.mark.parametrize('mode',['show','grep'])
@pytest.mark.parametrize('ending',['','\n','\r','\r\n'])
def test_exact_fit_at_scan_budget_still_distinguishes_eof(mode,ending):
    raw='NEEDLE '+'x'*90+ending
    identifier=save_original(raw)
    expected=raw if mode=='show' else '1: '+raw
    with patch('tokenguard.storage.MAX_SCAN',len(raw)):
        result=(read_lines(identifier,1,1,max_chars=len(expected)) if mode=='show' else
                grep_lines(identifier,'needle',context=0,max_chars=len(expected)))
    assert result == expected
    row=events()[0][-1]
    assert row['truncated'] is False
    assert row['scanned_chars'] == len(raw)


def test_exact_fit_grep_can_include_context_and_multiple_matches():
    raw='before\r\nNEEDLE one\rbetween\nNEEDLE two\nafter'
    lines=io.StringIO(raw,newline='').readlines()
    expected=''.join(f'{n}: '+line for n,line in enumerate(lines,1))
    result=grep_lines(save_original(raw),'needle',context=1,max_chars=len(expected))
    assert result == expected
    assert events()[0][-1]['truncated'] is False


def test_seeded_grep_all_matching_lines_fit_without_extra_recovery():
    generator=random.Random(65537)
    for _ in range(30):
        raw=''.join('NEEDLE '+generator.choice(['x','原','😀'])*generator.randrange(1,20)
                    +generator.choice(['\n','\r','\r\n']) for _ in range(generator.randrange(1,15)))
        lines=io.StringIO(raw,newline='').readlines()
        expected=''.join(f'{n}: '+line for n,line in enumerate(lines,1))
        result=grep_lines(save_original(raw),'needle',context=0,max_chars=len(expected))
        assert result == expected
        assert events()[0][-1]['truncated'] is False


def test_full_budget_lookahead_does_not_claim_unsearched_tail_complete():
    raw='NEEDLE '+'x'*90+'\n'
    result=grep_lines(save_original(raw+'not yet searched\n'),'needle',context=0,
                      max_chars=len('1: '+raw))
    row=events()[0][-1]
    assert row['truncated'] is True
    assert row['scanned_chars'] == len(raw)+1
    assert 'retrieval limit reached' in result
