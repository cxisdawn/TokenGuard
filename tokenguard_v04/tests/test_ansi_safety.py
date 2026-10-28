"""Rendered severity words protect every occurrence; raw records stay byte exact."""
import pytest

from tokenguard.core import compress


@pytest.mark.parametrize('command', ['python diagnostics.py', 'cmake --build out'])
@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('diagnostic', [
    'ER\x1b[31mROR\x1b[0m: missing configuration',
    'FA\x1b[1mILED\x1b[0m: target did not finish',
    'Excep\x1b[33mtion\x1b[0m: invalid configuration',
    'As\x1b[1msert\x1b[0m: result differs',
    'fa\x1b[31mtal\x1b[0m: cannot compile',
    'de\x1b[1mnied\x1b[0m: cannot open target',
    'cri\x1b[31mtical\x1b[0m: service unavailable',
    '  a\x1b[31mt\x1b[0m Package.method(Module.java:12)',
    '  Fi\x1b[31mle\x1b[0m "module.py", line 12',
])
def test_colored_critical_duplicates_remain_verbatim(command, ending, diagnostic):
    raw = (diagnostic + ending) * 180
    assert compress(raw, command=command)[0] == raw


@pytest.mark.parametrize('command', ['python diagnostics.py', 'cmake --build out'])
@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_colored_diagnostics_survive_other_compression(command, ending):
    diagnostic = 'ER\x1b[31mROR\x1b[0m: cannot compile 项目' + ending
    raw = ('\x1b[32mcompile step complete\x1b[0m' + ending) * 400 + diagnostic * 3
    delivered, strategy = compress(raw, command=command)
    assert strategy == 'adjacent_duplicates'
    assert len(delivered) < len(raw) * 0.85
    assert delivered.endswith(diagnostic * 3)
    assert delivered.count(diagnostic) == 3
    assert delivered.startswith('\x1b[32mcompile step complete\x1b[0m' + ending)


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_split_color_warning_retains_warning_strategy(ending):
    record = 'WAR\x1b[33mNING\x1b[0m: synthetic advisory' + ending
    delivered, strategy = compress(record * 200, command='python diagnostics.py')
    assert strategy == 'repeated_warnings'
    assert delivered.startswith(record)
    assert '199 identical adjacent non-critical lines hidden' in delivered
