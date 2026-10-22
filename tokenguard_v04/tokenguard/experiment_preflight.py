"""Local-only Claude task preflight: help/version, trusted Hook and initial tests."""
import os
import hashlib
from pathlib import Path
import re
import tempfile
import sys
import uuid

from . import audit,doctor
from .claude_runtime import ClaudeExecutableError,resolve_claude
from .experiment import audit_scope,load_experiment,write_json
from .experiment_snapshot import changes,digest
from .experiment_modes import baseline_mode
from .experiment_validation import run_process,validate

REQUIRED_FLAGS=('--print','--output-format','--session-id','--model','--setting-sources',
                '--settings','--strict-mcp-config','--mcp-config')


def check(folder: Path, executable: str = 'claude') -> dict:
    folder=folder.resolve()
    record=load_experiment(folder)
    result={'schema':'tokenguard.task-preflight/v1','timestamp':audit.timestamp(),
            'mode':'local_only; no Agent session','experiment_id':record['experiment_id'],
            'prepared_sessions':len(record['trials']),'model_api_calls_started_by_runner':0,
            'local_checks_passed':False,'checks':{},'cli':{},
            'requested_model':record['task']['model'],
            'baseline_mode':baseline_mode(record['config']),
            'hook_probe_scope':'optimized arm; baseline may omit runner-injected Hook',
            'authentication':'unverified; no credentials inspected',
            'provider_routing':'unverified; no model request sent',
            'host_replacement_acceptance':'unverified; standalone Hook subprocess only',
            'actual_task_tokens':'unavailable','actual_billed_cost':'unavailable'}
    checks=result['checks']
    checks['fresh_experiment']=not (folder/'started.json').exists() and not (folder/'execution.lock').exists()
    checks['initial_worktrees_unchanged']=all(changes(folder/'trials'/t['trial_id']/'worktree',
                                                   record['files_sha256'],[])['only_allowed_changes']
                                                for t in record['trials'])
    base=Path(__file__).resolve().parent.parent
    hashes={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((base/'tokenguard').glob('*.py'))}
    checks['runtime_unchanged']=(digest(hashes)==record['config']['tools_sha256']
                                 and sys.version==record['config']['python'])
    checks['hook_environment_enabled']=not any(os.environ.get(name,'').lower() in ('1','true')
                                               for name in ('CLAUDE_CODE_SIMPLE','CLAUDE_CODE_SAFE_MODE'))
    checks['cli_version']=checks['cli_flags']=checks['configured_hook']=checks['initial_task']=False
    try:
        result['cli'].update(resolve_claude(executable))
        binary=result['cli']['executable']
        env=dict(os.environ)
        version=run_process([binary,'--version'],folder,env,15)
        text=version['stdout'].strip()
        checks['cli_version']=(version['exit_code']==0 and not version['timed_out']
                               and not version['outputs_truncated'] and len(text)<=200
                               and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)? \(Claude Code\)',text) is not None)
        if checks['cli_version']:
            result['cli']['version']=text
        help_result=run_process([binary,'--help'],folder,env,15)
        advertised=set(re.findall(r'(?<![\w-])--[a-z][a-z-]*',help_result['stdout']))
        missing=sorted(set(REQUIRED_FLAGS)-advertised)
        result['cli']['missing_required_flags']=missing
        # --max-turns can be supported but hidden from the public help listing.
        turns=run_process([binary,'--max-turns','1','--help'],folder,env,15)
        checks['cli_flags']=(help_result['exit_code']==0 and not help_result['timed_out']
                              and not help_result['outputs_truncated'] and not missing
                              and turns['exit_code']==0 and not turns['timed_out']
                              and not turns['outputs_truncated']
                              and '--session-id' in turns['stdout'])
        result['cli']['max_turns_help_parse']='passed' if checks['cli_flags'] else 'failed'
    except (OSError,ValueError) as exc:
        result['cli']['error_type']=type(exc).__name__
        if isinstance(exc,ClaudeExecutableError):
            result['cli']['error_code']=exc.code
    # Only read the Hook configuration owned by this experiment, never user settings.
    with tempfile.TemporaryDirectory(prefix='tg-preflight-') as temporary:
        work=Path(temporary)
        with audit_scope(work/'audit'):
            probe=doctor.check(folder/'control')
        result['hook']={k:v for k,v in probe.items() if k not in ('project','recent_diagnostics')}
        checks['configured_hook']=all(probe.get(k)=='passed' for k in
                                      ('configuration','configured_process','compression','artifact_integrity'))
        validation=validate(folder/'snapshot',folder/'snapshot',record['task'],work)
        result['initial_validation']=validation
        if record['task']['expect_initial_failure']:
            checks['initial_task']=(validation.get('exit_code')==1 and validation.get('failures',0)>0
                                    and validation.get('errors',0)==0 and not validation.get('timed_out',True)
                                    and not validation.get('outputs_truncated',True)
                                    and (record['task']['expected_tests'] is None
                                         or validation.get('tests')==record['task']['expected_tests']))
        else:
            checks['initial_task']=validation['passed']
    result['local_checks_passed']=all(checks.values())
    path=folder/('preflight-'+uuid.uuid4().hex+'.json')
    result['report_path']=str(path)
    write_json(path,result)
    return result
