"""Reproducible offline paired experiments. This runner never invokes a model API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from tokenguard.hook import handle
from tokenguard.audit import summary
from tokenguard.tokens import estimate
from tokenguard.storage import grep_lines, read_lines, MAX_SCAN
from tokenguard.experiment_validation import run_process
from tokenguard.mock_reporting import unrun, checked_worker, export
from tokenguard.usage import parse

PROMPT = 'Run the task checks; preserve failure diagnostics. For Python bug task, fix divide without changing tests, then run pytest. In pytest log fixtures also inspect test_123 by name. In the large artifact task, verify final failure details by line range.'
CASES = ('pytest_pass', 'pytest_fail', 'build_errors', 'python_bug', 'large_artifact_recovery')


def digest(folder: Path) -> str:
    data = b''
    for path in sorted(folder.glob('*.py')):
        data += path.name.encode()+b'\0'+path.read_bytes()+b'\0'
    return hashlib.sha256(data).hexdigest()


def fixture_log(case: str) -> tuple[str, str, list[str]]:
    if case.startswith('pytest'):
        text = ''.join(f'test_log.py::test_{i} PASSED [ 90%]\n' for i in range(1000))
        details = ('test_log.py::test_bug FAILED [100%]\nE assert 3 == 5\n===== 1 failed, 1000 passed in 0.5s =====\n' if case.endswith('fail') else '===== 1000 passed in 0.5s =====\n')
        return text+details, 'python -m pytest -v', details.splitlines()
    details = 'src/module.c:99: error: missing_symbol\nBUILD FAILED: exit 1\n'
    return 'src/old.c:12: warning: deprecated API\n'*1500+details, 'cmake --build build', details.splitlines()


def worker(config: dict) -> dict:
    project = Path(config['project'])
    os.environ.update({'TOKENGUARD_DATA_DIR': str(project/'tg-data'), 'TOKENGUARD_SOURCE':'mock',
                       'TOKENGUARD_EXPERIMENT_ID':config['experiment_id'], 'TOKENGUARD_ENABLED':'1',
                       'TOKENGUARD_DIAGNOSTICS':'1'})
    session = config['session_id']
    inputs: list[str] = []
    outputs: list[str] = []
    tools = 0
    failures_preserved = True
    failure_recovery_verified = None
    def tool(raw: str, command: str) -> str:
        nonlocal tools
        tools += 1
        visible = raw
        if config['variant'] == 'optimized':
            result = handle({'hook_event_name':'PostToolUse','session_id':session,'tool_name':'Bash',
                             'tool_input': {'command':command}, 'tool_response': {'stdout':raw,'stderr':'','isImage':False,'interrupted':False}})
            if result:
                visible = result['hookSpecificOutput']['updatedToolOutput']['stdout']
        inputs.append(raw)
        outputs.append(visible)
        (project/f'tool-{tools}-original.txt').write_text(raw,encoding='utf-8')
        (project/f'tool-{tools}-visible.txt').write_text(visible,encoding='utf-8')
        return visible
    initial = digest(project)
    if config['case'] == 'python_bug':
        before = subprocess.run([sys.executable,'-m','pytest','-v','--color=no'], cwd=project,capture_output=True,text=True,encoding='utf-8',timeout=60)
        visible = tool(before.stdout+before.stderr, 'python -m pytest -v')
        failures_preserved = before.returncode == 1 and 'FAILED' in visible and 'AssertionError' in visible
        # Fixed deterministic mock policy, shared across both variants. No claim of AI quality.
        target = project/'arithmetic.py'
        target.write_text(target.read_text(encoding='utf-8-sig').replace('a // b','a / b'),encoding='utf-8')
        after = subprocess.run([sys.executable,'-m','pytest','-q','--color=no'],cwd=project,capture_output=True,text=True,encoding='utf-8',timeout=60)
        tool(after.stdout+after.stderr,'python -m pytest -q')
        success = after.returncode == 0 and '504 passed' in after.stdout
        tests_unchanged = (project/'test_arithmetic.py').read_bytes() == (ROOT/'fixture/test_arithmetic.py').read_bytes()
        success = success and tests_unchanged and 'return a / b' in target.read_text(encoding='utf-8')
        (project/'verification.json').write_text(json.dumps({'before_exit':before.returncode,'after_exit':after.returncode,'tests_unchanged':tests_unchanged}),encoding='utf-8')
    elif config['case'] == 'large_artifact_recovery':
        repeated = 'compile heartbeat ok ' + 'x'*64 + '\n'
        repetitions = MAX_SCAN//len(repeated)+2000
        details = 'src/module.c:99: error: missing_symbol\nBUILD FAILED: exit 1\n'
        text = repeated*repetitions+details
        visible = tool(text, 'cmake --build build')
        failures_preserved = details in visible
        if config['variant'] == 'optimized':
            record = summary(session, config['experiment_id'])['records'][-1]
            recovered = read_lines(record['artifact_id'], repetitions+1, 2,
                                   compression_id=record['compression_id'])
            visible = tool(recovered, 'python tg.py show ARTIFACT --start FINAL --count 2')
            failure_recovery_verified = visible == details
        else:
            # The identical task can inspect the original visible log without recovery.
            failure_recovery_verified = visible.endswith(details)
        success = failures_preserved and failure_recovery_verified
    else:
        text, command, required = fixture_log(config['case'])
        visible = tool(text, command)
        failures_preserved = all(s in visible for s in required)
        success = failures_preserved
        if config['case'].startswith('pytest'):
            target = 'test_log.py::test_123 PASSED'
            if target not in visible:
                record = summary(session, config['experiment_id'])['records'][-1]
                recovered = grep_lines(record['artifact_id'], target, context=0, limit=1,
                                       compression_id=record['compression_id'])
                visible = tool(recovered, 'python tg.py grep ARTIFACT test_123')
            success = success and target in visible
    report = summary(session, config['experiment_id'])
    audit_complete = report.get('data_available') is True
    def measured(key):
        return report.get(key) if audit_complete else None
    original_tokens = sum(estimate(x)['estimated_tokens'] for x in inputs)
    visible_tokens = sum(estimate(x)['estimated_tokens'] for x in outputs)
    return {**config, 'mode':'offline_mock', 'success':success, 'failure_details_preserved':failures_preserved,
            'audit_complete':audit_complete, 'observed_audit':{k:v for k,v in report.items() if k != 'records'},
            'failure_recovery_verified':failure_recovery_verified,
            'indexed_recoveries':measured('indexed_recoveries'), 'index_build_bytes':measured('index_build_bytes'),
            'integrity_build_bytes':measured('integrity_build_bytes'), 'integrity_checked_bytes':measured('integrity_checked_bytes'),
            'recovery_scanned_chars':measured('scanned_chars'), 'recovery_latency_ms':measured('recovery_latency_ms'),
            'initial_snapshot_sha256':initial, 'final_code_sha256':digest(project),
            'tool_calls':tools, 'original_tool_chars':sum(map(len,inputs)), 'visible_tool_chars':sum(map(len,outputs)),
            'estimated_original_tool_tokens':original_tokens, 'estimated_visible_tool_tokens':visible_tokens,
            'estimated_tool_token_reduction_ratio':1-visible_tokens/original_tokens if original_tokens else 0,
            'estimate_method':estimate('')['method'], 'compressions':measured('compressions'),
            'recovery_count':measured('recovery_count'), 'recovered_chars':measured('returned_chars'),
            'total_task_tokens':'unavailable', 'actual_api_input_tokens':'unavailable',
            'actual_api_output_tokens':'unavailable', 'cache_read_tokens':'unavailable', 'cache_write_tokens':'unavailable',
            'cache_hit_ratio':'unavailable', 'actual_billed_cost':'unavailable', 'api_latency_ms':'unavailable'}


def run(output: Path, rounds: int = 3) -> Path:
    if not 1 <= rounds <= 100:
        raise ValueError('rounds must be 1..100')
    experiment = 'mock-'+uuid.uuid4().hex
    folder = output.resolve()/experiment
    folder.mkdir(parents=True,exist_ok=False)
    configs = []
    for repeat in range(1, rounds+1):
        for case in CASES:
            for variant in (('baseline', 'optimized') if repeat % 2 else ('optimized', 'baseline')):
                configs.append({'case':case, 'round':repeat, 'variant':variant,
                    'project':str(folder/f'{case}-{repeat}-{variant}'),
                    'session_id':uuid.uuid4().hex, 'experiment_id':experiment, 'model':'deepseek-flash',
                    'prompt_sha256':hashlib.sha256(PROMPT.encode()).hexdigest(),
                    'base_config_sha256':hashlib.sha256(b'offline-mock-policy-v3-complete-evidence-abba').hexdigest()})
    rows = [unrun(config) for config in configs]
    expected_snapshot = digest(ROOT/'fixture')
    (folder/'plan.json').write_text(json.dumps({'trials':configs, 'initial_snapshot_sha256':expected_snapshot,
                                               'model_api_calls':0}, indent=2)+'\n', encoding='utf-8')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    for index, config in enumerate(configs):
        started = time.perf_counter()
        project = Path(config['project'])
        try:
            shutil.copytree(ROOT/'fixture', project, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
            initial = digest(project)
            if initial != expected_snapshot:
                raise ValueError('Fixture changed after planning')
            request = project/'request.json'
            request.write_text(json.dumps(config), encoding='utf-8')
            # A new bounded local process per arm; no model driver is involved.
            process = run_process([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                   '--worker', str(request)], project, env, 150)
            (project/'worker-process.json').write_text(json.dumps(process, allow_nan=False, indent=2), encoding='utf-8')
            if (type(process.get('exit_code')) is not int or process['exit_code'] != 0 or
                    process.get('timed_out') is not False or process.get('outputs_truncated') is not False):
                raise RuntimeError('Worker process did not complete with intact output')
            row = checked_worker(parse(process['stdout']), config, initial, digest(project))
            row['elapsed_ms'] = round((time.perf_counter()-started)*1000, 3)
            rows[index] = row
        except Exception as exc:
            rows[index].update(status='infrastructure_failed', failure_reason=type(exc).__name__,
                               elapsed_ms=round((time.perf_counter()-started)*1000, 3))
        # Always persist the full plan, even if the first process failed. Exception
        # details/streams stay in the private run folder, never the public report.
        export(folder, experiment, PROMPT, rows, CASES, rounds)
        if rows[index]['status'] != 'completed':
            break
    print(folder)
    return folder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT.parent/'reports/benchmarks')
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.loads(args.worker.read_text(encoding='utf-8')))))
    else:
        folder = run(args.output,args.rounds)
        rows = json.loads((folder/'report.json').read_text(encoding='utf-8'))['rows']
        if not all(row['success'] is True and row['comparison_eligible'] is True for row in rows):
            raise SystemExit(1)

if __name__ == '__main__':
    main()
