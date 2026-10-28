"""Pure Windows spelling checks run on every CI platform without UNC access."""
import os
from pathlib import Path

import pytest

from tokenguard.visible_tests import _windows_io_name, filesystem_path


@pytest.mark.parametrize('value,expected', [
    ('C:\\work\\artifact.txt', '\\\\?\\C:\\work\\artifact.txt'),
    ('d:/work/\u4e2d\u6587.txt', '\\\\?\\d:\\work\\\u4e2d\u6587.txt'),
    ('C:\\', '\\\\?\\C:\\'),
    ('\\\\server\\share\\artifact.txt', '\\\\?\\UNC\\server\\share\\artifact.txt'),
    ('//server/share/folder/artifact.txt', '\\\\?\\UNC\\server\\share\\folder\\artifact.txt'),
    ('\\\\?\\C:\\work\\artifact.txt', '\\\\?\\C:\\work\\artifact.txt'),
    ('\\\\?\\UNC\\server\\share\\artifact.txt', '\\\\?\\UNC\\server\\share\\artifact.txt'),
    ('\\\\?\\unc\\server\\share\\artifact.txt', '\\\\?\\UNC\\server\\share\\artifact.txt'),
])
def test_windows_drive_and_unc_spelling(value, expected):
    assert _windows_io_name(value) == expected
    assert _windows_io_name(expected) == expected


@pytest.mark.parametrize('value', [
    'relative\\file.txt', 'C:relative\\file.txt', '\\rooted\\file.txt',
    'C:\\work\\..\\outside', '\\\\?\\C:\\work\\..\\outside',
    '\\\\server\\share\\..\\outside', '\\\\server\\..\\outside',
    '\\\\.\\pipe\\name', '\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\file',
    '\\\\?\\Volume{1234}\\file', '\\\\?\\UNC\\server',
])
def test_windows_unsafe_or_unsupported_namespace_is_rejected(value):
    with pytest.raises(ValueError):
        _windows_io_name(value)


def test_short_native_path_keeps_its_public_spelling(tmp_path):
    assert filesystem_path(tmp_path / 'data') == tmp_path / 'data'


@pytest.mark.skipif(os.name != 'nt', reason='Windows UTF-16 threshold and relative-path API')
@pytest.mark.parametrize('emoji', [False, True])
def test_native_io_path_uses_absolute_utf16_length(tmp_path, monkeypatch, emoji):
    monkeypatch.chdir(tmp_path)
    relative = Path('\U0001f600' * 90 if emoji else 'x' * 190) / 'artifact.txt'
    absolute = str(relative.absolute())
    assert len(absolute.encode('utf-16-le')) // 2 >= 248
    expected = Path('\\\\?\\' + absolute)
    assert filesystem_path(relative) == expected
    assert filesystem_path(expected) == expected


@pytest.mark.skipif(os.name != 'nt', reason='Windows path traversal API')
def test_io_path_rejects_parent_before_absolute_normalization(tmp_path):
    with pytest.raises(ValueError, match='traversal'):
        filesystem_path(tmp_path / '..' / 'outside')
