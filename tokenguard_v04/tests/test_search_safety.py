"""Filename summaries require a simple listing command and unambiguous records."""
import pytest

from tokenguard.core import command_type, compress


def listing(ending='\n'):
    return ''.join(f'src/module_{index:04d}.py{ending}' for index in range(300))


@pytest.mark.parametrize('notice', ['WARNING: missing configuration.py', 'NOTICE: configure plugin.py',
    'note: use local settings.py', 'INFO: pending build.py', 'src/file.py:10: configuration.py',
    'warning missing configuration.py', 'NOTICE configure plugin.py'])
def test_path_shaped_diagnostic_is_not_hidden(notice):
    raw = listing() + notice + '\n'
    assert compress(raw, command='rg --files')[0] == raw


@pytest.mark.parametrize('command', [
    "rg --files; echo 'NOTICE: configure plugin.py'", 'rg --files | sort',
    'cd project && rg --files', 'rg --files 2>&1', 'rg --files > listing.py',
    'echo "rg --files"', "python -c 'rg --files'", 'rg --files $(helper)',
    'rg --files `helper`', 'rg --files\necho notice.py', 'rg --files --null',
    'rg --files --json', 'rg --files --debug', 'rg --files-with-matches needle',
    'find src -type f -exec helper {} +', 'find src -type f -printf "%p: notice.py\\n"',
])
def test_ambiguous_or_output_transforming_command_is_passthrough(command):
    raw = listing()
    assert compress(raw, command=command)[0] == raw


@pytest.mark.parametrize('command', [
    'rg --files', 'rg --files --sort path --no-config', 'rg --hidden --files',
    'rg.exe --files', '"C:\\Tools\\rg.exe" --files', "& 'C:\\My Tools\\rg.exe' --files",
    "rg --files -g '*.py' src", 'rg --files --glob=*.py --max-depth 3',
    'rg --files --color never -- src', 'find src -type f', "find 'source files' -name '*.py' -type f -print",
])
@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_supported_filename_commands_preserve_first_twenty_records(command, ending):
    raw = listing(ending)
    delivered, strategy = compress(raw, command=command)
    assert command_type(command) == 'search'
    assert strategy == 'file_search'
    assert delivered.startswith(''.join(raw.splitlines(keepends=True)[:20]))
    assert '280 file paths hidden' in delivered


@pytest.mark.parametrize('path', ['.gitignore', 'LICENSE', 'src/[generated].py', 'src/emoji_😀.py',
                                 ' trailing.py', 'trailing.py ', 'src/a.py\x00', 'src/a.py\u2028note.py'])
def test_unsupported_path_format_is_preserved(path):
    raw = listing() + path + '\n'
    assert compress(raw, command='rg --files')[0] == raw


@pytest.mark.parametrize('path', ['src/my file.py', '项目/模块.py', 'C:\\project\\source.py',
                                 '//server/share/source.py', '\\\\server\\share\\source.py'])
def test_supported_spaces_unicode_and_absolute_paths(path):
    raw = listing() + path + '\n'
    assert compress(raw, command='rg --files')[1] == 'file_search'


@pytest.mark.parametrize('suffix', ['src\x00', 'src\x1b[0m', 'src\x7f', 'src\u2028other',
    "'unterminated", '"unterminated', "'src'other", '--glob', '--max-depth nope',
    '--color always', '--sort modified', '--files', '--unknown', '--files=1'])
def test_unknown_command_syntax_is_not_assumed_to_be_a_listing(suffix):
    raw = listing()
    assert compress(raw, command='rg --files ' + suffix)[0] == raw


@pytest.mark.parametrize('command', ['rg --hidden --files --json', 'rg.exe --files --debug',
    '"C:\\Tools\\rg.exe" --files | sort', 'find.exe src -type f -exec helper {} +',
    'rg --no-config --files; echo notice.py'])
def test_ambiguous_search_does_not_fall_through_to_generic_duplicate_compression(command):
    raw = 'NOTICE: configuration missing.py\n' * 300
    assert command_type(command) == 'search'
    assert compress(raw, command=command)[0] == raw
