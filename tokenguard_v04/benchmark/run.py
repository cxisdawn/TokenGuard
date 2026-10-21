"""Reproducible offline paired experiments. This runner never invokes a model API."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
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
    original_tokens = sum(estimate(x)['estimated_tokens'] for x in inputs)
    visible_tokens = sum(estimate(x)['estimated_tokens'] for x in outputs)
    return {**config, 'mode':'offline_mock', 'success':success, 'failure_details_preserved':failures_preserved,
            'failure_recovery_verified':failure_recovery_verified,
            'indexed_recoveries':report['indexed_recoveries'], 'index_build_bytes':report['index_build_bytes'],
            'recovery_scanned_chars':report['scanned_chars'], 'recovery_latency_ms':report['recovery_latency_ms'],
            'initial_snapshot_sha256':initial, 'final_code_sha256':digest(project),
            'tool_calls':tools, 'original_tool_chars':sum(map(len,inputs)), 'visible_tool_chars':sum(map(len,outputs)),
            'estimated_original_tool_tokens':original_tokens, 'estimated_visible_tool_tokens':visible_tokens,
            'estimated_tool_token_reduction_ratio':1-visible_tokens/original_tokens if original_tokens else 0,
            'estimate_method':estimate('')['method'], 'compressions':report['compressions'],
            'recovery_count':report['recovery_count'], 'recovered_chars':report['returned_chars'],
            'total_task_tokens':'unavailable', 'actual_api_input_tokens':'unavailable',
            'actual_api_output_tokens':'unavailable', 'cache_read_tokens':'unavailable', 'cache_write_tokens':'unavailable',
            'cache_hit_ratio':'unavailable', 'actual_billed_cost':'unavailable', 'api_latency_ms':'unavailable'}


def run(output: Path, rounds: int = 3) -> Path:
    if not 1 <= rounds <= 100:
        raise ValueError('rounds must be 1..100')
    experiment = 'mock-'+uuid.uuid4().hex
    folder = output.resolve()/experiment
    folder.mkdir(parents=True,exist_ok=False)
    rows = []
    for repeat in range(1,rounds+1):
        for case in CASES:
            pair = []
            for variant in ('baseline','optimized'):
                project = folder/f'{case}-{repeat}-{variant}'
                shutil.copytree(ROOT/'fixture',project,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
                config = {'case':case,'round':repeat,'variant':variant,'project':str(project),
                          'session_id':uuid.uuid4().hex,'experiment_id':experiment,'model':'deepseek-flash',
                          'prompt_sha256':hashlib.sha256(PROMPT.encode()).hexdigest(),
                          'base_config_sha256':hashlib.sha256(b'offline-mock-policy-v2-indexed-recovery').hexdigest()}
                request = project/'request.json'
                request.write_text(json.dumps(config),encoding='utf-8')
                started = time.perf_counter()
                # A separate process with a fresh UUID per arm, no --resume or shared conversation.
                result = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',str(request)],capture_output=True,text=True,encoding='utf-8',timeout=150)
                if result.returncode:
                    raise RuntimeError(f'Mock worker failed: {case}/{variant}; exit {result.returncode}')
                row = json.loads(result.stdout)
                row['elapsed_ms'] = round((time.perf_counter()-started)*1000,3)
                pair.append(row)
            if pair[0]['initial_snapshot_sha256'] != pair[1]['initial_snapshot_sha256']:
                raise ValueError('A/B snapshots differ')
            for row in pair:
                row['paired_final_code_equal'] = pair[0]['final_code_sha256'] == pair[1]['final_code_sha256']
                row['success'] = row['success'] and row['paired_final_code_equal'] and row['failure_details_preserved']
                rows.append(row)
    aggregate = []
    for case in CASES:
        baseline = [r for r in rows if r['case']==case and r['variant']=='baseline']
        optimized = [r for r in rows if r['case']==case and r['variant']=='optimized']
        base_chars = statistics.mean(r['visible_tool_chars'] for r in baseline)
        opt_chars = statistics.mean(r['visible_tool_chars'] for r in optimized)
        aggregate.append({'case':case, 'pairs':rounds,
                          'baseline_successes':sum(r['success'] for r in baseline),
                          'optimized_successes':sum(r['success'] for r in optimized),
                          'mean_character_reduction_after_recovery':1-opt_chars/base_chars,
                          'baseline_mean_ms':statistics.mean(r['elapsed_ms'] for r in baseline),
                          'optimized_mean_ms':statistics.mean(r['elapsed_ms'] for r in optimized),
                          'optimized_time_stddev_ms':statistics.pstdev(r['elapsed_ms'] for r in optimized)})
    (folder/'report.json').write_text(json.dumps({'experiment_id':experiment,'mode':'offline_mock','prompt':PROMPT,'aggregate':aggregate,'rows':rows},indent=2),encoding='utf-8')
    with (folder/'report.csv').open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    markdown = '# TokenGuard offline mock A/B\n\nThese are deterministic fixture/mock trials, not model or billing experiments. Each arm uses a new process/session and identical initial Python files.\n\n'
    markdown += '|Case|Round|Arm|Success|Tool chars|Estimated tool tokens|Time ms|Recoveries|Index build bytes|Recovery scanned chars|\n|---|---:|---|---|---:|---:|---:|---:|---:|---:|\n'
    for row in rows:
        markdown += f"|{row['case']}|{row['round']}|{row['variant']}|{row['success']}|{row['visible_tool_chars']}|{row['estimated_visible_tool_tokens']}|{row['elapsed_ms']}|{row['recovery_count']}|{row['index_build_bytes']}|{row['recovery_scanned_chars']}|\n"
    markdown += '\nPaired character reductions below include selective recovery output; they are not task token or cost reductions.\n\n'
    for item in aggregate:
        markdown += f"- {item['case']}: {item['mean_character_reduction_after_recovery']:.2%} mean character reduction; {item['optimized_successes']}/{rounds} mock successes.\n"
    markdown += '\nTotal task/API tokens, cache usage, cache hit changes and actual billed costs: unavailable. Tool-only heuristic estimates cannot establish task-level savings. Timing includes Python/pytest startup and disk I/O; no API latency. The Python fix is scripted; AI repair quality is unmeasured.\n'
    (folder/'report.md').write_text(markdown,encoding='utf-8')
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
        if not all(row['success'] for row in rows):
            raise SystemExit(1)

if __name__ == '__main__':
    main()
