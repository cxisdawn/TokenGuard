"""Owned-process execution and independent validation with pristine tests."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from .experiment_snapshot import snapshot,write_snapshot


def run_process(argv: list[str], cwd: Path, env: dict[str,str], timeout: int,
                stdin: str | None = None, output_limit: int = 16*1024*1024) -> dict:
    started=time.perf_counter()
    options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True}
    # Spool output rather than retaining arbitrarily large tool logs in RAM.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                              stdout=out,stderr=err,text=True,encoding='utf-8',errors='replace',**options)
        timed_out=False
        try:
            proc.communicate(stdin,timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True
            if os.name=='nt':
                try:
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=10)
                except (OSError,subprocess.TimeoutExpired):
                    pass
                if proc.poll() is None:proc.kill()
            else:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            proc.communicate(timeout=10)
        truncated=out.tell()>output_limit or err.tell()>output_limit
        out.seek(0);err.seek(0)
        stdout=out.read(output_limit).decode('utf-8',errors='replace')
        stderr=err.read(output_limit).decode('utf-8',errors='replace')
    return {'exit_code':proc.returncode,'timed_out':timed_out,'elapsed_ms':round((time.perf_counter()-started)*1000),
            'stdout':stdout,'stderr':stderr,'outputs_truncated':truncated}


def validate(snapshot_dir: Path, candidate: Path, spec: dict, output: Path) -> dict:
    payload,_=snapshot(snapshot_dir,spec['files'])
    for name in spec['editable_files']:
        try:
            edits,_=snapshot(candidate,[name]);payload.update(edits)
        except (OSError,ValueError):
            return {'passed':False,'exit_code':None,'reason':'editable_source_missing_or_unsafe','tests':0}
    env=dict(os.environ)
    env.update({'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'})
    # A short OS temporary path avoids MAX_PATH failures in nested Windows reports.
    # Only logs are retained in output; original/candidate directories stay untouched.
    with tempfile.TemporaryDirectory(prefix='tg-validation-') as temporary:
        root=Path(temporary)
        project=root/'worktree';write_snapshot(project,payload)
        xml=root/'pytest.xml'
        argv=[sys.executable,'-m','pytest','-q','-p','no:cacheprovider','--junitxml',str(xml),'--',*spec['test_files']]
        result=run_process(argv,project,env,spec['timeout_seconds'])
        counts={'tests':0,'failures':0,'errors':0,'skipped':0}
        if xml.exists() and xml.stat().st_size<=16*1024*1024:
            try:
                for case in ET.parse(xml).iter('testcase'):
                    counts['tests']+=1
                    for tag in ('failure','error','skipped'):
                        if case.find(tag) is not None: counts[{'failure':'failures','error':'errors','skipped':'skipped'}[tag]]+=1
            except ET.ParseError:
                counts['errors']+=1
        expected=spec['expected_tests']
        passed=(result['exit_code']==0 and not result['timed_out'] and not result['outputs_truncated'] and counts['tests']>counts['skipped']
                and counts['failures']==0 and counts['errors']==0
                and (expected is None or (counts['tests']==expected and counts['skipped']==0)))
        (output/'validation.stdout.txt').write_text(result['stdout'],encoding='utf-8')
        (output/'validation.stderr.txt').write_text(result['stderr'],encoding='utf-8')
        return {k:v for k,v in result.items() if k not in ('stdout','stderr')}|counts|{'passed':passed,'uses_pristine_protected_files':True}
