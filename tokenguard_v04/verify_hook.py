"""No API calls: manual PostToolUse process/response/artifact verification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    root = Path(__file__).resolve().parent
    raw = ''.join(f'test_mod.py::test_case[{i}] PASSED [ 90%]\n' for i in range(500))
    raw += 'test_mod.py::test_bug FAILED [100%]\nE assert 3 == 5\n===== 1 failed, 500 passed in 0.4s =====\n'
    event = {'hook_event_name': 'PostToolUse', 'session_id': 'manual-hook-verification-v04',
             'tool_name': 'Bash', 'tool_input': {'command': 'python -m pytest -v'},
             'tool_response': {'stdout': raw, 'stderr': 'stderr retained', 'interrupted': False,
                               'isImage': False, 'extra': {'retained': True}}}
    with tempfile.TemporaryDirectory(prefix='tg-manual-') as folder:
        env = {**os.environ, 'TOKENGUARD_DATA_DIR': folder, 'TOKENGUARD_SOURCE': 'manual',
               'TOKENGUARD_ENABLED': '1', 'TOKENGUARD_DIAGNOSTICS': '1'}
        run = subprocess.run([sys.executable, str(root/'tokenguard/hook.py')], input=json.dumps(event),
                             capture_output=True, text=True, encoding='utf-8', env=env, check=True)
        out = json.loads(run.stdout)['hookSpecificOutput']['updatedToolOutput']
        assert {k:v for k,v in out.items() if k != 'stdout'} == {k:v for k,v in event['tool_response'].items() if k != 'stdout'}
        assert 'FAILED' in out['stdout'] and 'E assert 3 == 5' in out['stdout']
        rows = [json.loads(line) for line in (Path(folder)/'audit-v04.jsonl').read_text(encoding='utf-8').splitlines()]
        candidates = [row for row in rows if row['event'] == 'compression']
        deliveries = [row for row in rows if row['event'] == 'replacement_delivery']
        assert len(candidates) == len(deliveries) == 1
        audit, delivery = candidates[0], deliveries[0]
        assert audit['replacement_status'] == 'prepared_candidate_not_host_confirmed'
        assert delivery['compression_id'] == audit['compression_id']
        assert delivery['status'] == 'stdout_write_succeeded'
        artifact = Path(folder)/'artifacts'/(audit['artifact_id']+'.txt')
        assert artifact.read_bytes() == raw.encode('utf-8')
        recovered = subprocess.run([sys.executable, str(root/'tg.py'), 'grep', audit['artifact_id'],
                                   'test_bug', '--compression-id', audit['compression_id']],
                                  env=env, capture_output=True, text=True, encoding='utf-8', check=True)
        assert 'FAILED' in recovered.stdout
        report = {'verification': 'manual subprocess; NOT live Claude integration',
                  'original_chars': len(raw), 'optimized_chars': len(out['stdout']),
                  'failure_preserved': True, 'artifact_byte_identical': True, 'recovery_verified': True,
                  'stdout_delivery_verified': True, 'host_acceptance': 'unverified'}
        print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
