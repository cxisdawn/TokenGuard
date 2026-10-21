"""Separate-process fixture agent. No model, API usage or billing is simulated."""
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tokenguard.experiment_snapshot import read_spec, source_file
from tokenguard.experiment_validation import run_process
from tokenguard.usage import load


def main() -> None:
    spec=read_spec(load(Path(sys.argv[1])))
    session=sys.argv[2]
    hook=Path(__file__).resolve().with_name('hook.py')
    output=Path(sys.argv[3])
    env=dict(os.environ)
    env.update({'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'})
    for step in ('before','after'):
        result=run_process([sys.executable,'-m','pytest','-v','-p','no:cacheprovider','--',*spec['test_files']],
                           Path.cwd(),env,spec['timeout_seconds'])
        event={'hook_event_name':'PostToolUse','session_id':session,'tool_name':'Bash',
               'tool_use_id':step,'tool_input':{'command':'python -m pytest -v'},
               'tool_response':{'stdout':result['stdout'],'stderr':result['stderr'],
                                'exitCode':result['exit_code'],'interrupted':result['timed_out']}}
        adapted=subprocess.run([sys.executable,str(hook),'--source','mock'],input=json.dumps(event),
                               capture_output=True,text=True,encoding='utf-8',env=env,timeout=15)
        visible=result['stdout']
        if adapted.returncode==0 and adapted.stdout.strip():
            visible=json.loads(adapted.stdout)['hookSpecificOutput']['updatedToolOutput']['stdout']
        (output/(step+'.visible.txt')).write_text(visible,encoding='utf-8')
        (output/(step+'.stderr.txt')).write_text(result['stderr'],encoding='utf-8')
        if result['timed_out'] or result['outputs_truncated']:
            raise ValueError('Fixture tool output incomplete')
        if step=='before':
            for edit in spec['mock_edits']:
                path=source_file(Path.cwd(),edit['path'])
                content=path.read_text(encoding='utf-8')
                if content.count(edit['old'])!=1:
                    raise ValueError('Mock edit must match exactly once')
                path.write_text(content.replace(edit['old'],edit['new'],1),encoding='utf-8')
    print(json.dumps({'session_id':session,'is_error':False,'tool_calls':2,
                      'agent_version':'tokenguard-fixture-agent/1','synthetic_agent':True}))


if __name__=='__main__':
    main()
