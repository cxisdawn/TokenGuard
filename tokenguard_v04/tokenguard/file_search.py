"""Conservative filename-list recognizer, never an evaluator of shell commands."""
from __future__ import annotations

import io
import re

TOKEN = re.compile(r'''"[^"\r\n]*"|'[^'\r\n]*'|[^\s'"]+''')
SHELL = re.compile(r'[;&|<>\r\n$`(){}]')
DIAGNOSTIC = re.compile(r'^(?:warn(?:ing)?|notice|note|info|debug|hint)\b', re.I)
PATH = re.compile(r'[\w./\\@+ -]+\.[\w-]+')
SEARCH_HINT = re.compile(r'\brg(?:\.exe)?\b[^\r\n]*--files\b|\bfind(?:\.exe)?\b[^\r\n]*-type[ \t]+f\b', re.I)
RG_FLAGS = frozenset(('--hidden', '--no-ignore', '--no-ignore-vcs', '--no-ignore-parent',
    '--no-ignore-global', '--no-ignore-dot', '--follow', '--no-follow', '--no-config',
    '--no-messages', '-u', '-uu', '-uuu'))
RG_VALUES = frozenset(('-g', '--glob', '--iglob', '-t', '--type', '-T', '--type-not',
                       '--max-depth', '--sort', '--sortr', '--color'))


def _tokens(command: str) -> list[str] | None:
    command = command.strip(' \t')
    # PowerShell's call operator is allowed only before one literal executable.
    if command.startswith('&') and len(command) > 1 and command[1] in ' \t':
        command = command[1:].lstrip(' \t')
    if (SHELL.search(command) or any((ord(char) < 32 and char != '\t') or ord(char) == 127
                                   or (char.isspace() and char not in ' \t') for char in command)):
        return None
    result, position = [], 0
    for match in TOKEN.finditer(command):
        gap = command[position:match.start()]
        # Reject concatenated quoting and unparsed fragments. This is a small
        # literal grammar shared by common Bash/PowerShell spellings, not shlex.
        if gap.strip() or (result and not gap):
            return None
        value = match[0]
        if value[0] in "\"'":
            value = value[1:-1]
        if not value:
            return None
        result.append(value)
        position = match.end()
    return result if result and not command[position:].strip() else None


def _rg(arguments: list[str]) -> bool:
    files = 0
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == '--':
            # Everything after -- is a literal search root.
            break
        if argument == '--files':
            files += 1
        elif argument in RG_FLAGS:
            pass
        elif argument.startswith('-'):
            option, equal, value = argument.partition('=')
            if option not in RG_VALUES:
                return False
            if not equal:
                index += 1
                if index >= len(arguments):
                    return False
                value = arguments[index]
            if not value:
                return False
            if option in ('--sort', '--sortr') and value != 'path':
                return False
            if option == '--color' and value != 'never':
                return False
            if option == '--max-depth' and not (value.isascii() and value.isdecimal()):
                return False
        index += 1
    return files == 1


def _find(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments) and not arguments[index].startswith('-'):
        index += 1
    if not index:
        return False
    typed = printed = False
    while index < len(arguments):
        option = arguments[index]
        if option == '-print' and not printed:
            printed = True
        elif option in ('-type', '-name', '-iname', '-path', '-ipath'):
            index += 1
            if index >= len(arguments):
                return False
            if option == '-type':
                if arguments[index] != 'f' or typed:
                    return False
                typed = True
        else:
            return False
        index += 1
    return typed


def filename_command(command: str) -> bool:
    """Accept one literal filename-only invocation; unknown syntax stays visible."""
    tokens = _tokens(command)
    if not tokens:
        return False
    executable = re.split(r'[/\\]', tokens[0])[-1].casefold()
    if executable in ('rg', 'rg.exe'):
        return _rg(tokens[1:])
    # find.exe is Windows' content-search tool, not POSIX find.
    return executable == 'find' and _find(tokens[1:])


def search_hint(command: str) -> bool:
    """Route ambiguous listings to passthrough, not generic duplicate folding."""
    return bool(SEARCH_HINT.search(command))


def summarize(text: str, command: str, protected: re.Pattern[str]) -> str | None:
    if not filename_command(command):
        return None
    records = list(io.StringIO(text, newline=''))
    for record in records:
        value = record.rstrip('\r\n')
        if value != value.strip() or DIAGNOSTIC.match(value) or protected.search(value):
            return None
        # Colons are common diagnostics/content locations. Only an absolute
        # drive-letter prefix is accepted, including on a Linux verifier.
        if re.match(r'^[A-Za-z]:[/\\]', value):
            value = value[2:]
        if ':' in value or not PATH.fullmatch(value):
            return None
    if len(records) <= 20:
        return text
    return ''.join(records[:20]) + f'[TokenGuard: {len(records)-20} file paths hidden; use artifact grep/show]\n'
