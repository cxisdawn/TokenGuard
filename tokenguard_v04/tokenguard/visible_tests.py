"""Standalone visible-test worker, copied into owned benchmark snapshots.

Receipts are local execution evidence, not a security boundary against the same
OS user. This file intentionally imports no TokenGuard package or model SDK.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time
import uuid

IMAGE_ID = None
SOURCE_FILES = ()
DEFAULT_TESTS = ['tests/lint/unittest_lint.py', 'tests/test_self.py']
LIMIT = 16 * 1024 * 1024


def canonical_path(path: Path) -> Path:
    """Reject actual links/reparse points, while accepting Windows 8.3 aliases."""
    path = path.absolute()
    if '..' in path.parts:
        raise ValueError('Parent path traversal is forbidden')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('Path links are forbidden')
        try:
            attributes = getattr(component.lstat(), 'st_file_attributes', 0)
        except FileNotFoundError:
            continue
        if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT, including junctions
            raise ValueError('Path links or reparse points are forbidden')
    resolved = path.resolve()
    # Windows may retain the extended-length prefix during a sharing race.
    # Normalize its spelling so equal paths still compare equal; all reparse
    # checks above happen before resolution and are never skipped.
    if os.name == 'nt':
        value = str(resolved)
        if value.startswith('\\\\?\\UNC\\'):
            return Path('\\\\' + value[8:])
        if value.startswith('\\\\?\\'):
            return Path(value[4:])
    return resolved


def fingerprint(root: Path, files: list[str] | tuple[str, ...]) -> str:
    """Hash only declared editable files; reject links before opening a file."""
    root = canonical_path(root)
    hashes = {}
    total = 0
    if not files:
        raise ValueError('Missing source manifest')
    for name in files:
        parts = PurePosixPath(name).parts
        if not parts or parts[0] != 'pylint' or '..' in parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe source name')
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError('Source links are forbidden')
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Missing or oversized source')
        with path.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        total += len(raw)
        if len(raw) > 1024 * 1024 or total > LIMIT:
            raise ValueError('Source exceeds budget')
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def run_visible(root: Path, image_id: str, files: list[str], arguments: list[str]) -> int:
    run_id = uuid.uuid4().hex
    name = 'tgq-visible-' + run_id
    args = ['docker', 'run', '--rm', '--name', name, '--network', 'none',
            '--label', 'tokenguard.quality=' + os.environ.get('TOKENGUARD_EXPERIMENT_ID', 'local'),
            '--label', 'tokenguard.role=visible-tests',
            '--mount', 'type=bind,source=' + str(root / 'pylint') + ',target=/testbed/pylint,readonly',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYLINT_HOME=/tmp/tg-pylint',
            '--workdir', '/testbed', image_id,
            '/opt/miniconda3/envs/testbed/bin/python', '-m', 'pytest', '-v', '--color=no',
            '-p', 'no:cacheprovider', *(arguments or DEFAULT_TESTS)]
    return run_recorded(root, image_id, files, arguments, args, cleanup_name=name, run_id=run_id)


def run_recorded(root: Path, image_id: str, files: list[str], arguments: list[str], argv: list[str],
                 *, cleanup_name: str | None = None, run_id: str | None = None) -> int:
    """Execute and record an actual local test process; also used by the offline demo."""
    before = fingerprint(root, files)
    run_id = run_id or uuid.uuid4().hex
    started = time.perf_counter()
    timed_out = False
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(argv, cwd=root, stdout=out, stderr=err, timeout=150)
            code = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, code = True, 124
        finally:
            if cleanup_name:
                try:
                    subprocess.run(['docker', 'rm', '-f', cleanup_name], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        truncated = out.tell() > LIMIT or err.tell() > LIMIT
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(LIMIT), err.read(LIMIT)
    after = fingerprint(root, files)
    destination = os.environ.get('TOKENGUARD_TEST_RECEIPTS_DIR')
    if destination:
        stdout += ('\n[TokenGuard test_run=' + run_id + ']\n').encode()
        receipt = {'schema': 'tokenguard.visible-test/v1', 'run_id': run_id,
                   'session_id': os.environ.get('TOKENGUARD_TEST_SESSION_ID'),
                   'experiment_id': os.environ.get('TOKENGUARD_EXPERIMENT_ID'),
                   'origin': os.environ.get('TOKENGUARD_TEST_ORIGIN', 'agent'),
                   'image_id': image_id, 'arguments': arguments, 'full_suite_requested': not arguments,
                   'source_before': before, 'source_after': after,
                   'exit_code': code, 'timed_out': timed_out, 'outputs_truncated': truncated,
                   'elapsed_ms': round((time.perf_counter() - started) * 1000),
                   'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
                   'stderr_sha256': hashlib.sha256(stderr).hexdigest(),
                   'stdout_bytes': len(stdout), 'stderr_bytes': len(stderr)}
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        # Unique exclusive writes; receipt committed only after both complete logs.
        for suffix, data in (('.stdout.txt', stdout), ('.stderr.txt', stderr),
                             ('.json', json.dumps(receipt, indent=2).encode())):
            with (folder / (run_id + suffix)).open('xb') as stream:
                stream.write(data)
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return code


if __name__ == '__main__':
    if not isinstance(IMAGE_ID, str) or not IMAGE_ID.startswith('sha256:'):
        raise SystemExit('This worker must be generated by the quality adapter')
    raise SystemExit(run_visible(Path(__file__).resolve().parent, IMAGE_ID, list(SOURCE_FILES), sys.argv[1:]))
