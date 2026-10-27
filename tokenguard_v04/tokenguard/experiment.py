"""Paired task execution. Planning never launches processes; paid runs are opt-in."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from . import audit, usage
from .claude_runtime import resolve_claude
from .experiment_snapshot import changes, digest, read_spec, snapshot, write_snapshot
from .experiment_modes import BASELINE_MODES,baseline_mode,hook_mode,settings_path
from .experiment_validation import run_process, validate

SCHEMA='tokenguard.task-experiment/v1'
BASE=Path(__file__).resolve().parent.parent


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value,indent=2,ensure_ascii=True)+'\n',encoding='utf-8')


@contextmanager
def locked(folder: Path):
    # A crash deliberately leaves this marker: do not accidentally reuse sessions.
    path=folder/'execution.lock'
    with path.open('x',encoding='ascii') as stream:
        stream.write(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


def prepare(project: Path, spec_path: Path, output: Path, rounds: int = 1,
            baseline: str = 'disabled_hook', *, pytest_mode: str = 'compact') -> Path:
    if type(rounds) is not int or not 1<=rounds<=20:
        raise ValueError('rounds must be 1..20')
    if baseline not in BASELINE_MODES:
        raise ValueError('Unknown baseline mode')
    if pytest_mode not in ('compact', 'indexed', 'off'):
        raise ValueError('Unknown pytest mode')
    spec=read_spec(usage.load(spec_path))
    project=project.resolve()
    payload,hashes=snapshot(project,spec['files'])
    folder=output.resolve()/('task-experiment-'+uuid.uuid4().hex)
    if folder.is_relative_to(project):
        raise ValueError('Experiment output must be outside the source project')
    folder.mkdir(parents=True,exist_ok=False)
    write_snapshot(folder/'snapshot',payload)
    write_json(folder/'task.json',spec)
    # Intervention settings stay outside candidate code; permissions are untouched.
    import installer
    settings=installer.install(folder/'control',Path(sys.executable),pytest_mode=pytest_mode)
    settings_copy=folder/'settings.json'
    settings_copy.write_bytes(settings.read_bytes())
    write_json(folder/'no-hook-settings.json',{})
    write_json(folder/'mcp.json',{'mcpServers':{}})
    tool_hashes={str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((BASE/'tokenguard').glob('*.py'))}
    config={'max_turns':spec['max_turns'],'timeout_seconds':spec['timeout_seconds'],
            'baseline_mode':baseline,'pytest_mode':pytest_mode,
            'task_spec_sha256':digest(spec),
            'python':sys.version,'tools_sha256':digest(tool_hashes),
            'settings_sha256':hashlib.sha256(settings_copy.read_bytes()).hexdigest(),
            'no_hook_settings_sha256':hashlib.sha256((folder/'no-hook-settings.json').read_bytes()).hexdigest(),
            'mcp_sha256':hashlib.sha256((folder/'mcp.json').read_bytes()).hexdigest(),
            'setting_sources':'user','strict_mcp_config':True}
    trials=[]
    for number in range(1,rounds+1):
        for arm in (('baseline','optimized') if number%2 else ('optimized','baseline')):
            name=f'{number:03d}-{arm}'
            write_snapshot(folder/'trials'/name/'worktree',payload)
            trials.append({'trial_id':name,'pair_id':f'round-{number:03d}','arm':arm,
                           'session_id':str(uuid.uuid4())})
    record={'schema':SCHEMA,'experiment_id':folder.name,'task':spec,'files_sha256':hashes,
            'initial_snapshot_sha256':digest(hashes),
            'prompt_sha256':hashlib.sha256(spec['prompt'].encode()).hexdigest(),
            'config':config,'config_sha256':digest(config),'trials':trials}
    write_json(folder/'experiment.json',record)
    return folder


def load_experiment(folder: Path) -> dict:
    folder=folder.resolve()
    record=usage.load(folder/'experiment.json')
    if not isinstance(record,dict) or record.get('schema')!=SCHEMA:
        raise ValueError('Invalid task experiment')
    spec=read_spec(record['task'])
    _,hashes=snapshot(folder/'snapshot',spec['files'])
    if hashes!=record['files_sha256'] or digest(hashes)!=record['initial_snapshot_sha256']:
        raise ValueError('Initial snapshot changed')
    if read_spec(usage.load(folder/'task.json'))!=spec:
        raise ValueError('Task specification changed')
    if hashlib.sha256(spec['prompt'].encode()).hexdigest()!=record['prompt_sha256']:
        raise ValueError('Prompt changed')
    config=record['config']
    if config.get('pytest_mode', 'compact') not in ('compact', 'indexed', 'off'):
        raise ValueError('Unknown pytest mode')
    mode=baseline_mode(config)
    if mode=='no_hook' or 'no_hook_settings_sha256' in config:
        if hashlib.sha256((folder/'no-hook-settings.json').read_bytes()).hexdigest()!=config.get('no_hook_settings_sha256'):
            raise ValueError('No-hook settings changed')
        if usage.load(folder/'no-hook-settings.json')!={}:
            raise ValueError('No-hook settings must be an empty intervention')
    if digest(spec)!=config['task_spec_sha256']:
        raise ValueError('Task criteria changed')
    for name,field in [('settings.json','settings_sha256'),('mcp.json','mcp_sha256')]:
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=config[field]:
            raise ValueError('Experiment configuration changed')
    if digest(config)!=record['config_sha256']:
        raise ValueError('Configuration hash changed')
    seen=set()
    trials=record['trials']
    if not isinstance(trials,list) or not trials or len(trials)>40:
        raise ValueError('Invalid trial list')
    pairs={}
    for trial in trials:
        name=trial.get('trial_id','')
        import re
        if not re.fullmatch(r'[0-9]{3}-(baseline|optimized)',name) or trial.get('arm')!=name[4:] or trial.get('pair_id')!='round-'+name[:3]:
            raise ValueError('Invalid trial identity')
        session=str(uuid.UUID(trial['session_id']))
        if name in seen or session in seen:
            raise ValueError('Trial/session reused')
        seen.update((name,session))
        pairs.setdefault(trial['pair_id'],set()).add(trial['arm'])
        if (folder/'trials'/name).resolve()!=folder/'trials'/name:
            raise ValueError('Linked trial directory rejected')
    if any(arms!={'baseline','optimized'} for arms in pairs.values()):
        raise ValueError('Each round needs both arms')
    return record


def command(folder: Path, record: dict, trial: dict, driver: str, executable: str) -> list[str]:
    if driver=='mock':
        return [sys.executable,str(BASE/'tokenguard'/'experiment_mock.py'),str(folder/'task.json'),
                trial['session_id'],str(folder/'trials'/trial['trial_id']/'driver'),hook_mode(record,trial),
                record['config'].get('pytest_mode', 'compact')]
    if driver!='claude':
        raise ValueError('Unknown driver')
    return [executable,'--print','--output-format','json','--session-id',trial['session_id'],
            '--model',record['task']['model'],'--max-turns',str(record['task']['max_turns']),
            '--setting-sources',record['config']['setting_sources'],'--settings',str(settings_path(folder,record,trial)),
            '--strict-mcp-config','--mcp-config',str(folder/'mcp.json')]


@contextmanager
def audit_scope(folder: Path):
    previous=os.environ.get('TOKENGUARD_DATA_DIR')
    os.environ['TOKENGUARD_DATA_DIR']=str(folder)
    try:
        yield
    finally:
        if previous is None:os.environ.pop('TOKENGUARD_DATA_DIR',None)
        else:os.environ['TOKENGUARD_DATA_DIR']=previous


def execute(folder: Path, driver: str = 'mock', execute_run: bool = False,
            allow_paid: bool = False, max_paid_sessions: int = 2, executable: str = 'claude') -> dict:
    folder=folder.resolve()
    record=load_experiment(folder)
    plan={'dry_run':not execute_run,'driver':driver,'sessions':len(record['trials']),
          'pytest_mode':record['config'].get('pytest_mode','compact'),
          'baseline_mode':baseline_mode(record['config']),
          'trial_hook_modes':{t['trial_id']:hook_mode(record,t) for t in record['trials']},
          'paid_sessions':len(record['trials']) if driver=='claude' else 0,
          'commands':[command(folder,record,t,driver,executable) for t in record['trials']],
          'usage':'unavailable until explicitly imported; mock does not simulate tokens',
          'isolation':'independent directories and processes; NOT an OS security sandbox'}
    if not execute_run:
        return plan
    if driver=='claude' and (not allow_paid or type(max_paid_sessions) is not int or len(record['trials'])>max_paid_sessions):
        raise ValueError('Paid execution needs --allow-paid and a sufficient --max-paid-sessions (default 2)')
    if (folder/'started.json').exists():
        raise ValueError('Experiment already started; prepare a new experiment, never reuse sessions')
    # Reject altered prepared worktrees before launching even local validation.
    for trial in record['trials']:
        check=changes(folder/'trials'/trial['trial_id']/'worktree',record['files_sha256'],[])
        if not check['only_allowed_changes']:
            raise ValueError('Prepared trial worktree changed')
    tool_hashes={str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted((BASE/'tokenguard').glob('*.py'))}
    if digest(tool_hashes)!=record['config']['tools_sha256'] or sys.version!=record['config']['python']:
        raise ValueError('Runtime changed since preparation; prepare a new experiment')
    with locked(folder):
        if (folder/'started.json').exists():
            raise ValueError('Experiment already started')
        agent_version='tokenguard-fixture-agent/2'
        if driver=='claude':
            executable=resolve_claude(executable)['executable']
            version=run_process([executable,'--version'],folder,dict(os.environ),15)
            if version['exit_code']!=0 or version['timed_out'] or version['outputs_truncated'] or not 1<=len(version['stdout'].strip())<=200:
                raise ValueError('Unable to establish bounded Claude version')
            agent_version=version['stdout'].strip()
        write_json(folder/'started.json',{'driver':driver,'timestamp':audit.timestamp()})
        write_json(folder/'runtime.json',{'agent_version':agent_version,'driver':driver})
        completed = 0
        for trial in record['trials']:
            result = _run_trial(folder,record,trial,driver,executable,agent_version)
            completed += 1
            if result['stage_errors']:
                write_json(folder/'execution-stopped.json', {
                    'trial_id': trial['trial_id'], 'timestamp': audit.timestamp(),
                    'stage_errors': result['stage_errors'],
                    'reason': 'Local evidence unavailable; no further sessions launched; no automatic retry'})
                break
    return {'dry_run':False,'driver':driver,'completed_trials':completed,'planned_trials':len(record['trials']),
            'report':str(folder/'report.json')}


def _run_trial(folder: Path, record: dict, trial: dict, driver: str, executable: str, version: str) -> dict:
    started=time.perf_counter()
    root=folder/'trials'/trial['trial_id']
    spec=record['task']
    for name in ('initial-validation','final-validation','driver'):
        (root/name).mkdir()
    before=validate(folder/'snapshot',root/'worktree',spec,root/'initial-validation')
    baseline_ok=(before.get('exit_code')==1 and before.get('failures',0)>0 and before.get('errors',0)==0
                 and not before.get('timed_out',True) and not before.get('outputs_truncated',True)
                 and (spec['expected_tests'] is None or before['tests']==spec['expected_tests'])) if spec['expect_initial_failure'] else before['passed']
    env=dict(os.environ)
    env.update({'TOKENGUARD_ENABLED':'1' if trial['arm']=='optimized' else '0',
                'TOKENGUARD_DATA_DIR':str(root/'tg-data'),'TOKENGUARD_DIAGNOSTICS':'1',
                'TOKENGUARD_EXPERIMENT_ID':record['experiment_id'],
                'TOKENGUARD_SOURCE':'mock' if driver=='mock' else 'claude_hook',
                'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'})
    result={'exit_code':None,'timed_out':False,'elapsed_ms':0,'stdout':'','stderr':''}
    response={};error=None
    if baseline_ok:
        try:
            result=run_process(command(folder,record,trial,driver,executable),root/'worktree',env,
                               spec['timeout_seconds'],spec['prompt'] if driver=='claude' else None)
            if result['outputs_truncated']:
                raise ValueError('Agent JSON output exceeded 16 MiB')
            response=usage.parse(result['stdout'])
            if not isinstance(response,dict) or response.get('session_id')!=trial['session_id']:
                raise ValueError('Agent response session mismatch')
        except (OSError,ValueError,TypeError) as exc:
            error=type(exc).__name__
    else:
        error='initial_validation_failed'
    for stream in ('stdout','stderr'):
        (root/'driver'/('agent.'+stream+'.txt')).write_text(result[stream],encoding='utf-8')
    metrics={k:'unavailable' for k in ('hook_process_count','hook_process_elapsed_ms','hook_errors',
                                      'original_stdout_chars','visible_stdout_chars')}
    if driver=='mock' and result['exit_code']==0 and error is None:
        try:
            measured=usage.load(root/'driver'/'worker-metrics.json')
            if measured.get('session_id')!=trial['session_id'] or measured.get('hook_mode')!=hook_mode(record,trial):
                raise ValueError('Mock metric identity differs')
            for key in metrics:
                if type(measured.get(key)) is not int or not 0<=measured[key]<=2**63-1:
                    raise ValueError('Invalid mock measurement')
            if measured['hook_process_count']!=(0 if hook_mode(record,trial)=='no_hook' else 2):
                raise ValueError('Unexpected mock Hook count')
            if hook_mode(record,trial)=='no_hook' and (measured['hook_process_elapsed_ms'] or measured['hook_errors']):
                raise ValueError('No-hook mode reported Hook work')
            metrics={key:measured[key] for key in metrics}
        except (OSError,ValueError,TypeError,AttributeError):
            error='invalid_mock_metrics'
    check=changes(root/'worktree',record['files_sha256'],spec['editable_files'])
    after=validate(folder/'snapshot',root/'worktree',spec,root/'final-validation')
    success=(baseline_ok and result['exit_code']==0 and not result['timed_out'] and error is None
             and response.get('is_error') is False and after['passed'] and check['only_allowed_changes'])
    stage_errors = []
    stats = {'data_available': False, 'compressions': None, 'recovery_count': None,
             'saved_chars': None, 'counts_status': 'unavailable', 'records': []}
    diagnostics = {'matching_invocations': None, 'data_available': False}
    try:
        with audit_scope(root/'tg-data'):
            stats=audit.summary(trial['session_id'],record['experiment_id'])
            diagnostics=audit.diagnostics(trial['session_id'],record['experiment_id'],limit=100)
        if any(value.get('data_available', True) is not True for value in (stats, diagnostics)):
            stage_errors.append({'stage': 'audit', 'type': 'AuditIntegrityError'})
    except Exception as exc:
        stats = {**stats, 'data_available': False}
        stage_errors.append({'stage': 'audit', 'type': type(exc).__name__})
    row={**trial,'driver':driver,'agent_version':version,'fresh_session':True,
         'baseline_mode':baseline_mode(record['config']),'hook_mode':hook_mode(record,trial),**metrics,
         'task_success':success and not stage_errors,'tests_passed':after['passed'],
         'tests_unchanged':not check['protected_files_changed'],
         'initial_validation':before,'final_validation':after,'source_changes':check,
         'elapsed_ms':result['elapsed_ms'],'agent_exit_code':result['exit_code'],
         'total_execution_ms':round((time.perf_counter()-started)*1000),
         'timed_out':result['timed_out'],'execution_error':error,
         'execution_status':'infrastructure_failed' if stage_errors else 'completed',
         'stage_errors':stage_errors,
         'tool_calls':2 if driver=='mock' and success else None,
         'bash_hook_invocations':diagnostics['matching_invocations'],
         'compression':{k:v for k,v in stats.items() if k!='records'},
         'actual_api_input_tokens':'unavailable','actual_cost':'unavailable',
         'execution_provenance':'controller-produced local record; not tamper-proof certification',
         'model_visibility':'fixture adapter only' if driver=='mock' else 'not independently verified'}
    write_json(root/'result.json',row)
    return row


def attach_usage(folder: Path, trial_id: str, source: Path, format_name: str, evidence: str,
                 coverage: str, request_count: int | None = None, billing: Path | None = None) -> Path:
    record=load_experiment(folder)
    selected=[t for t in record['trials'] if t['trial_id']==trial_id]
    if len(selected)!=1:raise ValueError('Unknown trial')
    root=folder/'trials'/trial_id
    with locked(folder):
        result=usage.load(root/'result.json')
        if result['driver']=='mock' and evidence!='synthetic':
            raise ValueError('Mock trials can only attach synthetic usage')
        if (root/'measurement.json').exists():
            raise ValueError('Usage already attached; no silent measurement overwrite')
        manifest={key:record[key] for key in ('experiment_id','prompt_sha256','initial_snapshot_sha256','config_sha256')}
        manifest.update({key:result[key] for key in ('arm','pair_id','session_id','agent_version','fresh_session',
                                                    'task_success','tests_passed','tests_unchanged','tool_calls','elapsed_ms')})
        manifest.update(schema_version=1,task_id=record['task']['task_id'],provider=record['task']['provider'],
                        baseline_mode=baseline_mode(record['config']),
                        model=record['task']['model'],evidence_kind=evidence,coverage=coverage,
                        expected_request_count=request_count,recovery_count=result['compression']['recovery_count'])
        write_json(root/'usage-manifest.json',manifest)
        bundle=usage.import_usage(source,root/'usage-manifest.json',format_name,billing)
        write_json(root/'measurement.json',bundle)
    return root/'measurement.json'
