"""One pinned SWE-bench task: local orchestration, official image/script/grader.

The existing snapshot/session/Hook machinery is reused. Docker and the upstream
grading checkout are optional evaluation tools, never runtime dependencies.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import urllib.parse
import urllib.request
import uuid

from . import audit
from .claude_runtime import resolve_claude
from .experiment import (BASE, audit_scope, command, load_experiment, locked,
                         prepare, write_json)
from .experiment_snapshot import changes, digest, relative_file
from .experiment_validation import run_process

INSTANCE = 'pylint-dev__pylint-6528'
COMMIT = '273a8b25620467c1e5686aa8d2a1dbb8c02c78d0'
IMAGE = 'swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-6528:latest'
VISIBLE_TESTS = ['tests/lint/unittest_lint.py', 'tests/test_self.py']


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process(argv: list[str], cwd: Path, timeout: int = 120, *, stdin: str | None = None) -> dict:
    return run_process(argv, cwd, {**os.environ, 'PYTHONUTF8': '1'}, timeout, stdin)


def checked(argv: list[str], cwd: Path, timeout: int = 120) -> str:
    result = process(argv, cwd, timeout)
    require(result['exit_code'] == 0 and not result['timed_out'] and not result['outputs_truncated'],
            'Local prerequisite process failed')
    return result['stdout'].strip()


def instance(path: Path) -> dict:
    row = json.loads(path.read_text(encoding='utf-8'))
    require(row['instance_id'] == INSTANCE and row['base_commit'] == COMMIT and row['image'] == IMAGE,
            'This adapter only accepts the documented pinned task')
    require(row['difficulty'] == '15 min - 1 hour' and len(row['FAIL_TO_PASS']) == 4 and
            len(row['PASS_TO_PASS']) == 171, 'Unexpected task criteria')
    return row


def fetch_instance(output: Path) -> Path:
    """Explicit public dataset download; no credentials or model calls."""
    require(not output.exists(), 'Instance output already exists; no silent overwrite')
    for offset in range(0, 500, 100):
        query = urllib.parse.urlencode({'dataset': 'SWE-bench/SWE-bench_Verified', 'config': 'default',
                                        'split': 'test', 'offset': offset, 'length': 100})
        with urllib.request.urlopen('https://datasets-server.huggingface.co/rows?' + query, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        require(len(raw) <= 8 * 1024 * 1024, 'Dataset page exceeds limit')
        for item in json.loads(raw)['rows']:
            row = item['row']
            if row.get('instance_id') == INSTANCE:
                output.parent.mkdir(parents=True, exist_ok=True)
                write_json(output, row)
                instance(output)
                return output
    raise ValueError('Pinned task not found in public dataset')


def visible_wrapper(image_id: str) -> str:
    """Same bytes in every arm; package-only mount keeps evaluator tests pristine."""
    return '''"""Run the original visible tests inside the pinned benchmark image."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

root = Path(__file__).resolve().parent
name = 'tgq-visible-' + uuid.uuid4().hex
args = ['docker', 'run', '--rm', '--name', name, '--network', 'none',
        '--label', 'tokenguard.quality=' + os.environ.get('TOKENGUARD_EXPERIMENT_ID', 'local'),
        '--label', 'tokenguard.role=visible-tests',
        '--mount', 'type=bind,source=' + str(root / 'pylint') + ',target=/testbed/pylint,readonly',
        '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYLINT_HOME=/tmp/tg-pylint',
        '--workdir', '/testbed', IMAGE_ID,
        '/opt/miniconda3/envs/testbed/bin/python', '-m', 'pytest', '-v', '--color=no',
        '-p', 'no:cacheprovider', *sys.argv[1:]]
if len(sys.argv) == 1:
    args.extend(['tests/lint/unittest_lint.py', 'tests/test_self.py'])
try:
    result = subprocess.run(args, timeout=150)
    code = result.returncode
finally:
    subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15)
raise SystemExit(code)
'''.replace('IMAGE_ID', repr(image_id))


def prepare_quality(instance_file: Path, harness: Path, output: Path, rounds: int = 3,
                    model: str = 'deepseek-flash', compat_wheel: Path | None = None,
                    max_turns: int = 20) -> Path:
    require(type(max_turns) is int and 1 <= max_turns <= 100, 'max_turns must be 1..100')
    row = instance(instance_file)
    harness = harness.resolve()
    harness_commit = checked(['git', 'rev-parse', 'HEAD'], harness)
    require(not checked(['git', 'status', '--porcelain'], harness), 'Upstream grader checkout is modified')
    details = json.loads(checked(['docker', 'image', 'inspect', IMAGE], output.parent))[0]
    image_id = details['Id']
    require(bool(re.fullmatch(r'sha256:[0-9a-f]{64}', image_id)), 'Invalid pinned image ID')
    head = checked(['docker', 'run', '--rm', '--network', 'none', image_id,
                    'git', '-C', '/testbed', 'rev-parse', 'HEAD'], output.parent)
    upstream_image_id = image_id
    if compat_wheel is not None:
        compat_wheel = compat_wheel.resolve()
        require(compat_wheel.name == 'tomlkit-0.10.1-py3-none-any.whl' and
                compat_wheel.is_file() and not compat_wheel.is_symlink() and compat_wheel.stat().st_size < 1024 * 1024,
                'Expected the documented small tomlkit compatibility wheel')
    if head != COMMIT or compat_wheel is not None:
        # Published environment images may retain the environment-setup checkout.
        # Align an owned container to the dataset base, then freeze it for ALL arms.
        name = 'tgq-checkout-' + uuid.uuid4().hex
        checked(['docker', 'create', '--name', name, '--network', 'none', image_id,
                 'bash', '-c', 'sleep infinity'], output.parent)
        try:
            checked(['docker', 'start', name], output.parent)
            checked(['docker', 'exec', name, 'git', '-C', '/testbed', 'reset', '--hard', COMMIT], output.parent)
            if compat_wheel is not None:
                checked(['docker', 'cp', str(compat_wheel), name + ':/tmp/' + compat_wheel.name], output.parent)
                checked(['docker', 'exec', name, '/opt/miniconda3/envs/testbed/bin/python', '-m', 'pip',
                         'install', '--no-index', '--no-deps', '/tmp/' + compat_wheel.name], output.parent)
            image_id = checked(['docker', 'commit', name], output.parent)
        finally:
            process(['docker', 'rm', '-f', name], output.parent, 30)
    require(checked(['docker', 'run', '--rm', '--network', 'none', image_id,
                     'git', '-C', '/testbed', 'rev-parse', 'HEAD'], output.parent) == COMMIT,
            'Frozen source commit does not match task')
    with tempfile.TemporaryDirectory(prefix='tgq-source-') as temporary:
        root = Path(temporary)
        archive = root / 'source.tar'
        root_files = checked(['docker', 'run', '--rm', '--network', 'none', image_id,
                              'git', '-C', '/testbed', 'ls-tree', '--name-only', COMMIT], output.parent).splitlines()
        metadata = [name for name in ('README.rst', 'LICENSE', 'pyproject.toml', 'setup.cfg', 'setup.py')
                    if name in root_files]
        with archive.open('wb') as stream:
            run = subprocess.run(['docker', 'run', '--rm', '--network', 'none', image_id,
                                  'git', '-C', '/testbed', 'archive', COMMIT,
                                  'pylint', *VISIBLE_TESTS, *metadata],
                                 stdout=stream, stderr=subprocess.PIPE, timeout=120)
        require(run.returncode == 0 and archive.stat().st_size < 16 * 1024 * 1024, 'Source export failed')
        project = root / 'project'
        project.mkdir()
        selected = []
        with tarfile.open(archive) as tar:
            for member in tar:
                if member.isdir():
                    continue
                require(member.isfile() and member.size <= 1024 * 1024, 'Unsupported source member')
                name = relative_file(member.name)
                content = tar.extractfile(member).read()
                target = project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                selected.append(name)
        (project / 'pytest.py').write_text(visible_wrapper(image_id), encoding='utf-8')
        selected.append('pytest.py')
        python = str(Path(sys.executable).resolve()).replace('\\', '/')
        prompt = (row['problem_statement'] + '\n\n'
                  'Work in the supplied public Pylint source snapshot. Fix the issue, preserving existing behavior. '
                  'Only modify existing files under pylint/. Do not modify tests, configuration, or pytest.py. '
                  'Do not inspect credentials, user configuration, parent directories, reference patches, online solutions, '
                  'or git history. No network research. The local source snapshot has no git history. '
                  'Dependencies and original tests are in a pinned local Docker image. '
                  f'Before editing and after the fix, run this exact Bash command without pipes, truncation, '
                  f'or redirection: "{python}" pytest.py\n'
                  'This helper runs the original tests with pytest -v in Docker; the complete output is intentional. '
                  'You may run additional diagnostics using the same helper or read the local source. '
                  'The evaluator will independently apply the hidden regression tests to a pristine checkout. '
                  'Do not change tests to pass. If a tool output offers TokenGuard grep/show recovery, '
                  'recover only the needed lines. Do not read audit or diagnostics files. '
                  'In your final response briefly describe the fix and test results, then quote any TokenGuard '
                  'artifact and compression_id you actually saw in a tool response; say none if absent. '
                  'Do not use tools just to find those IDs.\n')
        spec = {'schema_version': 1, 'task_id': INSTANCE, 'provider': 'deepseek', 'model': model,
                'prompt': prompt, 'files': selected, 'editable_files': [p for p in selected if p.startswith('pylint/')],
                'test_files': VISIBLE_TESTS, 'timeout_seconds': 600, 'max_turns': max_turns,
                'expect_initial_failure': False, 'expected_tests': None}
        spec_path = root / 'task.json'
        write_json(spec_path, spec)
        folder = prepare(project, spec_path, output, rounds, baseline='no_hook')
    # Neither the official hidden test patch nor the gold solution enters a worktree.
    write_json(folder / 'official-instance.json', row)
    plan = {'schema': 'tokenguard.swebench-quality/v1', 'instance_id': INSTANCE,
            'image_id': image_id, 'upstream_image_id': upstream_image_id,
            'upstream_source_head': head, 'image_repo_digests': details.get('RepoDigests', []),
            'compatibility_dependency': ({'name': 'tomlkit', 'version': '0.10.1', 'wheel_sha256': sha(compat_wheel),
                                         'reason': 'upstream image 0.13.2 breaks two existing TOML config tests'}
                                        if compat_wheel is not None else None),
            'harness': str(harness), 'harness_commit': harness_commit,
            'instance_sha256': sha(folder / 'official-instance.json'),
            'adapter_sha256': sha(Path(__file__)), 'runner_sha256': sha(BASE / 'benchmark/swebench_runner.py'),
            'scope': 'official environment image aligned to base commit with optional documented dependency pin; official eval script/grader; custom local orchestration and selected visible source',
            'agent_source_scope': 'all pylint/ files plus two original tests and selected root metadata; not full repository',
            'official_suite': '4 FAIL_TO_PASS and 171 PASS_TO_PASS cases; hidden test patch not agent-visible'}
    write_json(folder / 'quality-plan.json', plan)
    return folder


def load(folder: Path, *, verify_runtime: bool = True) -> tuple[dict, dict, dict]:
    record = load_experiment(folder)
    plan = json.loads((folder / 'quality-plan.json').read_text(encoding='utf-8'))
    row = instance(folder / 'official-instance.json')
    require(plan['instance_sha256'] == sha(folder / 'official-instance.json'), 'Task evidence changed')
    if verify_runtime:
        require(plan['adapter_sha256'] == sha(Path(__file__)) and
                plan['runner_sha256'] == sha(BASE / 'benchmark/swebench_runner.py'), 'Quality adapter changed; prepare again')
    harness = Path(plan['harness'])
    require(checked(['git', 'rev-parse', 'HEAD'], harness) == plan['harness_commit'] and
            not checked(['git', 'status', '--porcelain'], harness), 'Official grader drifted')
    return record, plan, row


def grade_log(instance_file: Path, harness: Path, log: Path) -> dict:
    """Called in a UTF-8 interpreter; imports the unmodified official grader."""
    sys.path.insert(0, str(harness))
    # Upstream harness/__init__.py eagerly imports Docker/datasets orchestration.
    # Load its package namespace without that initializer: grading/constants/parser
    # source files are imported unchanged; no fake grading functions or results.
    package = types.ModuleType('swebench.harness')
    package.__path__ = [str(harness / 'swebench/harness')]
    package.__package__ = 'swebench.harness'
    sys.modules['swebench.harness'] = package
    from swebench.types import TestSpec
    from swebench.harness.grading import get_eval_report, get_logs_eval
    row = instance(instance_file)
    spec = TestSpec(instance_id=INSTANCE, image=row['image'], eval_script_list=[], repo=row['repo'],
                    version=row['version'], FAIL_TO_PASS=row['FAIL_TO_PASS'], PASS_TO_PASS=row['PASS_TO_PASS'],
                    log_parser=row['log_parser'], eval_type=row['eval_type'])
    report = get_eval_report(spec, {'instance_id': INSTANCE, 'model_patch': 'controller-applied patch'}, str(log), True)[INSTANCE]
    statuses, found = get_logs_eval(spec, str(log))
    report['parsed_tests'] = len(statuses)
    report['expected_cases_observed'] = all(k in statuses for k in row['FAIL_TO_PASS'] + row['PASS_TO_PASS'])
    report['status_counts'] = {status: sum(v == status for v in statuses.values())
                               for status in sorted(set(statuses.values()))}
    report['parseable'] = found
    return report


def evaluate(folder: Path, plan: dict, row: dict, patch: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    (output / 'candidate.patch').write_bytes(patch.encode('utf-8'))
    (output / 'eval.sh').write_bytes(row['eval_script'].encode('utf-8'))
    name = 'tgq-eval-' + uuid.uuid4().hex
    checked(['docker', 'create', '--name', name, '--network', 'none',
             '--label', 'tokenguard.role=independent-evaluation', plan['image_id'],
             'bash', '-c', 'sleep infinity'], folder)
    try:
        checked(['docker', 'cp', str(output / 'eval.sh'), name + ':/tmp/tg-eval.sh'], folder)
        if patch:
            checked(['docker', 'cp', str(output / 'candidate.patch'), name + ':/tmp/tg-candidate.patch'], folder)
        checked(['docker', 'start', name], folder)
        shell = 'cd /testbed && '
        if patch:
            shell += 'git apply /tmp/tg-candidate.patch && '
        shell += 'bash /tmp/tg-eval.sh 2>&1'
        run = process(['docker', 'exec', name, 'bash', '-c', shell], folder, 300)
        (output / 'test-output.txt').write_bytes(run['stdout'].encode('utf-8'))
        (output / 'process.stderr.txt').write_bytes(run['stderr'].encode('utf-8'))
        grade = process([sys.executable, '-X', 'utf8', str(BASE / 'benchmark/swebench_runner.py'),
                         'grade', '--instance', str(folder / 'official-instance.json'), '--harness', plan['harness'],
                         '--log', str(output / 'test-output.txt')], folder)
        (output / 'grader.stderr.txt').write_bytes(grade['stderr'].encode('utf-8'))
        require(grade['exit_code'] == 0 and not grade['timed_out'] and not grade['outputs_truncated'],
                'Official grader process failed')
        result = json.loads(grade['stdout'])
        result.update(process_exit_code=run['exit_code'], timed_out=run['timed_out'],
                      outputs_truncated=run['outputs_truncated'], elapsed_ms=run['elapsed_ms'])
        cleanup = (run['exit_code'] == 1 and run['stdout'].rstrip().endswith(
            "error: pathspec 'tests/regrtest_data/directory/ignored_subdirectory/failing.py' did not match any file(s) known to git"))
        result['upstream_cleanup_error'] = 'new_test_file_not_in_base_commit' if cleanup else None
        result['evaluation_valid'] = ((run['exit_code'] == 0 or cleanup) and not run['timed_out'] and
                                      not run['outputs_truncated'] and result['parseable'] and
                                      result['expected_cases_observed'])
        result['resolved'] = result['resolved'] and result['evaluation_valid'] and bool(patch)
        write_json(output / 'grade.json', result)
        return result
    finally:
        process(['docker', 'rm', '-f', name], folder, 30)


def preflight(folder: Path) -> dict:
    record, plan, row = load(folder)
    require(not (folder / 'started.json').exists(), 'Sessions already consumed')
    output = folder / ('quality-preflight-' + uuid.uuid4().hex[:8])
    output.mkdir()
    original = evaluate(folder, plan, row, '', output / 'original')
    gold = evaluate(folder, plan, row, row['patch'], output / 'gold')
    f2p = original.get('tests_status', {}).get('FAIL_TO_PASS', {})
    p2p = original.get('tests_status', {}).get('PASS_TO_PASS', {})
    visible = process([sys.executable, str(folder / 'snapshot/pytest.py')], folder / 'snapshot', 200)
    (output / 'visible.stdout.txt').write_bytes(visible['stdout'].encode('utf-8'))
    (output / 'visible.stderr.txt').write_bytes(visible['stderr'].encode('utf-8'))
    from .hook import handle
    with audit_scope(output / 'tg-data'):
        previous = {key: os.environ.get(key) for key in ('TOKENGUARD_SOURCE', 'TOKENGUARD_ENABLED')}
        try:
            os.environ.update(TOKENGUARD_SOURCE='manual', TOKENGUARD_ENABLED='1')
            replacement = handle({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'session_id': 'quality-preflight',
                                  'tool_input': {'command': 'python pytest.py'},
                                  'tool_response': {'stdout': visible['stdout'], 'stderr': visible['stderr']}})
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    result = {'schema': 'tokenguard.swebench-preflight/v1', 'model_api_calls': 0,
              'original': original, 'gold': gold, 'visible_test_exit': visible['exit_code'],
              'visible_stdout_chars': len(visible['stdout']), 'manual_hook_compressed': replacement is not None,
              'manual_hook_not_live_integration': True,
              'passed': original['evaluation_valid'] and len(f2p.get('failure', [])) == 4 and
                        not f2p.get('success') and len(p2p.get('success', [])) == 171 and
                        gold['resolved'] and visible['exit_code'] == 0 and not visible['timed_out'] and
                        not visible['outputs_truncated'] and replacement is not None}
    write_json(output / 'preflight.json', result)
    write_json(folder / 'quality-preflight-latest.json', {'path': str(output / 'preflight.json'), 'passed': result['passed']})
    return result


def candidate_patch(folder: Path, record: dict, worktree: Path) -> tuple[str, dict]:
    check = changes(worktree, record['files_sha256'], record['task']['editable_files'])
    if not check['only_allowed_changes']:
        return '', check
    pieces = []
    for name in check['changed_files']:
        old = (folder / 'snapshot' / name).read_text(encoding='utf-8')
        new = (worktree / name).read_text(encoding='utf-8')
        for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                         fromfile='a/' + name, tofile='b/' + name):
            pieces.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    return ''.join(pieces), check


def parse_stream(raw: str, session: str) -> tuple[dict, dict]:
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    require(all(isinstance(e, dict) for e in events), 'Invalid stream event')
    results = [e for e in events if e.get('type') == 'result']
    require(len(results) == 1 and results[0].get('session_id') == session, 'Final result/session mismatch')
    tools = {}
    for event in events:
        require(event.get('session_id', session) == session, 'Mixed-session stream')
        if event.get('type') != 'assistant':
            continue
        message = event.get('message')
        require(isinstance(message, dict) and isinstance(message.get('content'), list), 'Invalid assistant message')
        for block in message['content']:
            require(isinstance(block, dict), 'Invalid content block')
            if block.get('type') == 'tool_use':
                tools[block['id']] = block['name']
    return results[0], {'tool_calls': len(tools), 'bash_calls': sum(name == 'Bash' for name in tools.values())}


def execute_quality(folder: Path, *, execute: bool = False, allow_paid: bool = False,
                    max_paid_sessions: int = 2, claude: str = 'claude') -> dict:
    record, plan, row = load(folder)
    count = len(record['trials'])
    if not execute:
        return {'dry_run': True, 'sessions': count, 'model': record['task']['model'],
                'max_turns_flag': record['task']['max_turns'], 'timeout_seconds': 600, 'baseline_mode': 'no_hook',
                'benchmark': INSTANCE, 'paid_requests': 'multiple per session; billed cost unavailable'}
    require(allow_paid and type(max_paid_sessions) is int and count <= max_paid_sessions,
            'Paid execution needs explicit gates')
    require(not (folder / 'started.json').exists(), 'Sessions already consumed; prepare a new experiment')
    marker = json.loads((folder / 'quality-preflight-latest.json').read_text(encoding='utf-8'))
    require(marker['passed'], 'Official original/gold preflight has not passed')
    hashes = {str(p.relative_to(BASE)): sha(p) for p in sorted((BASE / 'tokenguard').glob('*.py'))}
    require(digest(hashes) == record['config']['tools_sha256'] and sys.version == record['config']['python'],
            'Runtime drift; prepare again')
    for trial in record['trials']:
        check = changes(folder / 'trials' / trial['trial_id'] / 'worktree', record['files_sha256'], [])
        require(check['only_allowed_changes'], 'Trial worktree drift')
    executable = resolve_claude(claude)['executable']
    version = checked([executable, '--version'], folder, 15)
    with locked(folder):
        require(not (folder / 'started.json').exists(), 'Sessions already consumed')
        write_json(folder / 'started.json', {'driver': 'claude', 'timestamp': audit.timestamp(), 'sessions': count})
        for trial in record['trials']:
            root = folder / 'trials' / trial['trial_id']
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'TOKENGUARD_ENABLED': '1' if trial['arm'] == 'optimized' else '0',
                   'TOKENGUARD_DATA_DIR': str(root / 'tg-data'), 'TOKENGUARD_DIAGNOSTICS': '1',
                   'TOKENGUARD_EXPERIMENT_ID': record['experiment_id'], 'TOKENGUARD_SOURCE': 'claude_hook'}
            argv = command(folder, record, trial, 'claude', executable)
            argv[argv.index('json')] = 'stream-json'
            argv.append('--verbose')
            print('Starting ' + trial['trial_id'] + ' (new session)', flush=True)
            run = run_process(argv, root / 'worktree', env, 600, record['task']['prompt'])
            (root / 'agent.stream.jsonl').write_bytes(run['stdout'].encode('utf-8'))
            (root / 'agent.stderr.txt').write_bytes(run['stderr'].encode('utf-8'))
            response, tools, error = {}, {'tool_calls': None, 'bash_calls': None}, None
            try:
                response, tools = parse_stream(run['stdout'], trial['session_id'])
            except (ValueError, KeyError, TypeError) as exc:
                error = type(exc).__name__
            patch, check = candidate_patch(folder, record, root / 'worktree')
            grade = evaluate(folder, plan, row, patch, root / 'evaluation')
            with audit_scope(root / 'tg-data'):
                stats = audit.summary(trial['session_id'], record['experiment_id'])
                diagnostics = audit.diagnostics(trial['session_id'], record['experiment_id'], limit=100)
            citations = [r for r in stats['records'] if r['artifact_id'] in str(response.get('result', '')) and
                         r['compression_id'] in str(response.get('result', ''))]
            host_usage = response.get('usage', {})
            host_usage = {k: host_usage[k] for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                         'cache_creation_input_tokens') if type(host_usage.get(k)) is int}
            success = (run['exit_code'] == 0 and not run['timed_out'] and not run['outputs_truncated'] and
                       error is None and response.get('is_error') is False and grade['resolved'] and
                       check['only_allowed_changes'])
            result = {**trial, 'driver': 'claude', 'agent_version': version, 'task_success': success,
                      'grade': grade, 'source_changes': check, 'patch_nonempty': bool(patch),
                      'agent_elapsed_ms': run['elapsed_ms'], 'agent_exit_code': run['exit_code'],
                      'timed_out': run['timed_out'], 'outputs_truncated': run['outputs_truncated'],
                      'execution_error': error, **tools,
                      'bash_hook_invocations': diagnostics['matching_invocations'],
                      'hook_statuses': [d['status'] for d in diagnostics['invocations']],
                      'compression': {k: v for k, v in stats.items() if k != 'records'},
                      'model_cited_matching_metadata_count': len(citations),
                      'cli_aggregate_usage_unverified': host_usage, 'cli_num_turns': response.get('num_turns'),
                      'permission_denial_count': len(response.get('permission_denials', [])),
                      'cli_model_names_unverified': sorted(response.get('modelUsage', {})),
                      'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable'}
            write_json(root / 'quality-result.json', result)
            report_quality(folder)
            print(f"Completed {trial['trial_id']}: success={success}; compressions={stats['compressions']}", flush=True)
    return report_quality(folder)


def report_quality(folder: Path) -> dict:
    # Historical results remain readable after an adapter upgrade. Execution and
    # preflight still require the originally prepared runtime to match exactly.
    record, plan, _ = load(folder, verify_runtime=False)
    rows = []
    for trial in record['trials']:
        path = folder / 'trials' / trial['trial_id'] / 'quality-result.json'
        rows.append(json.loads(path.read_text(encoding='utf-8')) if path.exists() else
                    {**trial, 'task_success': False, 'not_run': True})
    arms = {}
    for arm in ('baseline', 'optimized'):
        own = [r for r in rows if r['arm'] == arm]
        arms[arm] = {'planned': len(own), 'completed': sum(not r.get('not_run') for r in own),
                     'successful': sum(r['task_success'] for r in own),
                     'compressions': sum(r.get('compression', {}).get('compressions', 0) for r in own),
                     'recoveries': sum(r.get('compression', {}).get('recovery_count', 0) for r in own),
                     'metadata_citations': sum(r.get('model_cited_matching_metadata_count', 0) for r in own)}
    paired = []
    for pair in sorted({r['pair_id'] for r in rows}):
        group = {r['arm']: r for r in rows if r['pair_id'] == pair}
        b, o = group['baseline'], group['optimized']
        exposure = o.get('compression', {}).get('compressions', 0) > 0
        paired.append({'pair_id': pair, 'baseline_success': b['task_success'], 'optimized_success': o['task_success'],
                       'compression_exposed': exposure,
                       'observed_quality_drop': not o['task_success'] if exposure and b['task_success'] and
                       not b.get('not_run') and not o.get('not_run') else 'not_evaluable'})
    report = {'schema': 'tokenguard.swebench-quality-report/v1', 'instance_id': INSTANCE,
              'difficulty': '15 min - 1 hour', 'requested_model': record['task']['model'],
              'scope': plan['scope'], 'arms': arms, 'pairs': paired, 'runs': rows,
              'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable',
              'general_no_quality_loss_claim': 'not_established; one task and small repeated sample',
              'limitations': ['custom agent source selection/orchestration; not an official full-suite score',
                              'same-user directories are not an OS security sandbox',
                              'hidden user/managed Claude context and backend routing not independently certified',
                              'max-turns flag is not an independently metered API request/cost cap',
                              'metadata citation supports visibility only for the cited tool output',
                              'CLI aggregate usage is not independently verified provider usage or billing']}
    write_json(folder / 'quality-report.json', report)
    fields = ['trial_id', 'arm', 'task_success', 'f2p_passed', 'p2p_passed', 'agent_elapsed_ms',
              'tool_calls', 'compressions', 'recovery_count', 'metadata_citations']
    lines = ['# SWE-bench quality pilot', '', INSTANCE + '; ' + plan['scope'], '',
             '| Trial | Success | F2P / 4 | P2P / 171 | Compressions | Recoveries | Metadata citations |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    with (folder / 'quality-report.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            statuses = row.get('grade', {}).get('tests_status', {})
            flat = {**row, 'f2p_passed': len(statuses.get('FAIL_TO_PASS', {}).get('success', [])),
                    'p2p_passed': len(statuses.get('PASS_TO_PASS', {}).get('success', [])),
                    **row.get('compression', {}), 'metadata_citations': row.get('model_cited_matching_metadata_count', 0)}
            writer.writerow({k: flat.get(k, 'unavailable') for k in fields})
            lines.append(f"| {row['trial_id']} | {row['task_success']} | {flat['f2p_passed']} | {flat['p2p_passed']} | "
                         f"{flat.get('compressions', 'unavailable')} | {flat.get('recovery_count', 'unavailable')} | {flat['metadata_citations']} |")
    lines += ['', 'Unrun cases remain in the report and are not silently excluded.',
              'No-compression trials do not measure the effect of compression on coding ability.',
              'Provider usage/billed cost unavailable. CLI aggregates are unverified and reported separately.',
              'One repeated task does not establish general non-inferiority.', '']
    (folder / 'quality-report.md').write_text('\n'.join(lines), encoding='utf-8')
    return report
