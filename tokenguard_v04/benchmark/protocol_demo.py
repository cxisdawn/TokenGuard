"""Real local pytest + Hook + receipt checks; deterministic edits, ZERO model calls."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from tokenguard import audit
from tokenguard.experiment import audit_scope, prepare, write_json
from tokenguard.experiment_validation import run_process
from tokenguard.experiment_snapshot import changes
from tokenguard.quality_protocol import evidence, pair_assessment, receipt
from tokenguard.quality_seed import activate_seed, build_seeds, seed_prompt
from tokenguard.visible_tests import fingerprint


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def local_test(root: Path, worker: Path, receipts: Path, session: str, experiment: str, origin: str) -> dict:
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'TOKENGUARD_TEST_RECEIPTS_DIR': str(receipts),
           'TOKENGUARD_TEST_SESSION_ID': session, 'TOKENGUARD_EXPERIMENT_ID': experiment,
           'TOKENGUARD_TEST_ORIGIN': origin}
    result = run_process([sys.executable, str(worker), str(root)], root, env, 60)
    require(result['exit_code'] in (0, 1) and not result['timed_out'] and not result['outputs_truncated'],
            'Actual local test process failed')
    return result


def demo(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    # The helper is a controller tool outside editable source. This demo uses
    # local pytest, explicitly not the optional official Docker task adapter.
    with tempfile.TemporaryDirectory(prefix='tg-protocol-') as temporary:
        temp = Path(temporary)
        project = temp / 'project'
        (project / 'pylint').mkdir(parents=True)
        (project / 'pylint/__init__.py').write_bytes(b'')
        (project / 'pylint/arithmetic.py').write_bytes((BASE / 'benchmark/fixture/arithmetic.py').read_bytes())
        tests = (BASE / 'benchmark/fixture/test_arithmetic.py').read_text(encoding='utf-8')
        (project / 'test_arithmetic.py').write_text(tests.replace('from arithmetic', 'from pylint.arithmetic'), encoding='utf-8')
        task = {'schema_version': 1, 'task_id': 'protocol-demo', 'provider': 'mock', 'model': 'mock',
                'prompt': 'Offline deterministic fixture; no agent or model is started.',
                'files': ['pylint/__init__.py', 'pylint/arithmetic.py', 'test_arithmetic.py'],
                'editable_files': ['pylint/arithmetic.py'], 'test_files': ['test_arithmetic.py'],
                'max_turns': 1, 'timeout_seconds': 60, 'expected_tests': 504}
        spec = temp / 'task.json'
        write_json(spec, task)
        folder = prepare(project, spec, output, 1, baseline='no_hook')
        record = json.loads((folder / 'experiment.json').read_text(encoding='utf-8'))
        worker = folder / 'local-test-worker.py'
        worker.write_text(
            'import sys\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {str(BASE)!r})\n'
            'from tokenguard.visible_tests import run_recorded\n'
            "root = Path(sys.argv[1])\n"
            "raise SystemExit(run_recorded(root, 'local-fixture-not-official-image', ['pylint/arithmetic.py'], [], "
            "[sys.executable, '-m', 'pytest', '-v', '--color=no', '-p', 'no:cacheprovider', 'test_arithmetic.py']))\n",
            encoding='utf-8')
        initial = local_test(folder / 'snapshot', worker, folder / 'controller-receipts',
                             'controller-preflight', record['experiment_id'], 'controller')
        require(initial['exit_code'] == 1 and '3 failed, 501 passed' in initial['stdout'], 'Unexpected initial fixture')
        seed = build_seeds(folder, record, folder, initial, next((folder / 'controller-receipts').glob('*.json')))
        initial_sha = fingerprint(folder / 'snapshot', task['editable_files'])
        rows = []
        for trial in record['trials']:
            root = folder / 'trials' / trial['trial_id']
            work = root / 'worktree'
            delivery = activate_seed(folder, seed['deliveries'][trial['trial_id']], root)
            prompt = (folder / delivery['prompt_path']).read_text(encoding='utf-8')
            if trial['arm'] == 'baseline':
                require(prompt == seed_prompt(task['prompt'], {'stdout': initial['stdout'],
                        'stderr': initial['stderr'], 'exit_code': 1}), 'Baseline observation changed')
            else:
                require('3 failed, 501 passed' in prompt and 'FAILED' in prompt and 'artifact=' in prompt,
                        'Compressed initial failures or recovery guide lost')
            # Mock decision, deliberately disclosed. No assertion about coding ability.
            source = work / 'pylint/arithmetic.py'
            source.write_bytes(source.read_bytes().replace(b'a // b', b'a / b'))
            final = local_test(work, worker, root / 'test-receipts', trial['session_id'], record['experiment_id'], 'agent')
            require(final['exit_code'] == 0 and '504 passed' in final['stdout'], 'Fixture did not pass')
            path = next((root / 'test-receipts').glob('*.json'))
            proof = receipt(path, trial['session_id'], record['experiment_id'])
            # Mock stream shape; test execution and Hook are real local processes.
            events = [{'type': 'assistant', 'session_id': trial['session_id'], 'message': {'content': [
                {'type': 'tool_use', 'id': 'post-test', 'name': 'Bash', 'input': {'command': 'python pytest.py'}}]}},
                {'type': 'user', 'session_id': trial['session_id'], 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'post-test', 'content': final['stdout']}]}}]
            observed = evidence(events, [], root / 'test-receipts', session=trial['session_id'],
                                experiment=record['experiment_id'], initial_source=initial_sha,
                                final_source=fingerprint(work, task['editable_files']), required_command='python pytest.py',
                                image_id='local-fixture-not-official-image', expected_tests=504)
            require(observed['final_test_observed'] and not observed['initial_test_observed'], 'Test evidence mismatch')
            source_check = changes(work, record['files_sha256'], task['editable_files'])
            require(source_check['only_allowed_changes'], 'Protected fixture files changed')
            with audit_scope(root / 'tg-data'):
                native = audit.summary(trial['session_id'], record['experiment_id'], source='claude_hook')
                manual = audit.summary(trial['session_id'], record['experiment_id'], source='manual')
            require(native['compressions'] == 0, 'Seed incorrectly counted as a native Hook compression')
            if trial['arm'] == 'optimized':
                # Test the exact recovery command metadata under the runtime data directory.
                cid = manual['records'][0]['compression_id']
                recovered = run_process([sys.executable, str(BASE / 'tg.py'), 'grep', delivery['original_stdout_sha256'],
                                         'FAILED', '--compression-id', cid], work,
                                        {**os.environ, 'PYTHONUTF8': '1', 'TOKENGUARD_DATA_DIR': str(root / 'tg-data')}, 30)
                require(recovered['exit_code'] == 0 and 'FAILED' in recovered['stdout'], 'Seed recovery failed')
                with audit_scope(root / 'tg-data'):
                    manual = audit.summary(trial['session_id'], record['experiment_id'], source='manual')
                require(manual['recovery_count'] == 1, 'Seed recovery attribution lost')
            rows.append({**trial, 'task_success': True, 'grade': {'evaluation_valid': True, 'resolved': True},
                         'source_changes': source_check, 'seed_delivery': delivery,
                         'compression': {'compressions': native['compressions']}, 'protocol_evidence': observed,
                         'post_tests': 504, 'seed_compressions': manual['compressions'],
                         'seed_recoveries': manual['recovery_count'], 'post_stdout_sha256': proof['stdout_sha256']})
    base, opt = ({r['arm']: r for r in rows}[a] for a in ('baseline', 'optimized'))
    pair = pair_assessment(base, opt, 'controlled_initial_log')
    require(pair['comparison_evidence_satisfied'], 'Controlled fixture protocol failed')
    native_pair = pair_assessment(base, opt, 'agent_driven')
    require(not native_pair['comparison_evidence_satisfied'], 'Controlled mode mislabelled as native integration')
    result = {'schema': 'tokenguard.protocol-demo/v1', 'model_api_calls': 0,
              'decision_driver': 'deterministic mock edits and mock transcript; actual local pytest and Hook subprocesses',
              'official_swebench_or_live_claude': False, 'initial_tests': 504, 'initial_failures': 3,
              'pair': pair, 'native_pair_comparison_valid': native_pair['comparison_evidence_satisfied'],
              'runs': rows, 'actual_provider_usage': 'unavailable', 'actual_billed_cost': 'unavailable'}
    write_json(folder / 'protocol-demo.json', result)
    fields = ['trial_id', 'post_tests', 'seed_compressions', 'seed_recoveries']
    with (folder / 'protocol-demo.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: row[k] for k in fields} for row in rows)
    (folder / 'protocol-demo.md').write_text(
        '# Offline protocol verification\n\nZERO model calls. Deterministic mock edits/transcript, actual local tests/Hook.\n\n'
        'Initial: 504 tests, 3 failed; after fixed edit: both arms 504 passed.\n\n'
        'Optimized seed: one manual compression, one selective recovery, zero native Hook compressions.\n\n'
        'Controlled comparison evidence passed; native agent-driven evidence correctly rejected.\n\n'
        'This verifies measurement plumbing, not live Claude integration or model coding quality.\n', encoding='utf-8')
    return {'report': str(folder / 'protocol-demo.json'), 'model_api_calls': 0,
            'actual_test_runs': 3, 'post_tests_per_arm': 504, 'manual_seed_compressions': 1,
            'seed_recoveries': 1, 'native_hook_compressions': 0, 'controlled_protocol_verified': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=BASE / 'reports/protocol')
    args = parser.parse_args()
    print(json.dumps(demo(args.output.resolve()), indent=2))


if __name__ == '__main__':
    main()
