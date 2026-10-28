"""unittest progress recognition must not hide docs, live output or report tails."""
import pytest

from tokenguard.core import compress


def progress(ending: str = '\n') -> str:
    return ''.join(f'test_{n} (test_sample.Case.test_{n}) ... ok{ending}' for n in range(120))


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('boundary', [
    '----------------------------------------------------------------------', 'Ran 120 tests in 0.001s',
    'OK', 'OK (skipped=1)', 'FAILED (unexpected successes=1)',
])
def test_report_tail_keeps_pass_shaped_diagnostics(ending, boundary):
    tail = ending.join((boundary, 'test_diagnostic (custom.report.test_diagnostic) ... ok',
                        'Ran 120 tests in 0.001s', 'OK', ''))
    raw = progress(ending) + tail
    optimized, strategy = compress(raw, command='python -m unittest -v')
    assert strategy == 'unittest_pass_summary'
    assert optimized.endswith(tail)


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('ambiguous', [
    'test_live (test_sample.Case.test_live) ... payload without status',
    'test_docs (test_sample.Case.test_docs)',
    'ok', 'expected failure', 'unexpected success', "skipped 'diagnostic reason'",
])
def test_interrupted_progress_and_detached_status_pass_through(ending, ambiguous):
    raw = progress(ending) + ending.join((ambiguous,
        'test_payload (test_sample.Case.test_payload) ... ok', 'ok',
        'Ran 121 tests in 0.001s', 'OK', ''))
    optimized, strategy = compress(raw, command='python -m unittest -v')
    assert optimized == raw
    assert strategy == 'ambiguous_unittest_output'


@pytest.mark.parametrize('diagnostic', [
    'WARNING: (configuration unavailable) ... ok',
    'CRITICAL (package.State) ... ok',
    'test_data (not a Python test identity) ... ok',
    'test_data (package.State) ... ... ok',
    'test_data (package.State\u2028diagnostic) ... ok',
])
def test_unknown_pass_shaped_records_stay_visible(diagnostic):
    raw = progress() + diagnostic + '\nRan 120 tests in 0.001s\nOK\n'
    optimized, _ = compress(raw, command='python -m unittest -v')
    assert diagnostic + '\n' in optimized


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_known_statuses_and_original_whitespace_remain(ending):
    tail = ending.join(("test_skip (test_sample.Case.test_skip) ... skipped 'reason'",
        'test_expected (test_sample.Case.test_expected) ... expected failure',
        'test_unexpected (test_sample.Case.test_unexpected) ... unexpected success',
        'Ran 123 tests in 0.001s', 'FAILED (skipped=1, expected failures=1, unexpected successes=1)', ''))
    raw = progress(ending) + tail
    optimized, strategy = compress(raw, command='python -m unittest -v')
    assert strategy == 'unittest_pass_summary'
    assert optimized.endswith(tail)
    assert '120 unittest ok lines hidden' in optimized


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_python_id_formats_and_ansi_progress(legacy, ending):
    raw = ''.join(f'\x1b[32mtest_{n} (test_sample.Case' + ('' if legacy else f'.test_{n}')
                  + f') ... ok\x1b[0m{ending}' for n in range(120))
    tail = ending.join(('----------------------------------------------------------------------', 'Ran 120 tests in 0.001s', 'OK', ''))
    optimized, strategy = compress(raw + tail, command='python -m unittest -v')
    assert strategy == 'unittest_pass_summary'
    assert '120 unittest ok lines hidden' in optimized
    assert optimized.endswith(tail)
