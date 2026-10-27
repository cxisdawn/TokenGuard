"""Explicit source snapshots for paired tasks; never discovers credentials."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .visible_tests import canonical_path

FORBIDDEN = {'.git','.claude','.codex','.aws','.ssh','.venv','venv','__pycache__','.pytest_cache',
             'reports','artifacts','.tokenguard','node_modules'}
PRIVATE_SUFFIXES = {'.pem','.key','.pfx','.p12','.zip','.exe','.msi','.jsonl','.log'}
MAX_FILE = 1024*1024
MAX_TOTAL = 16*1024*1024


def relative_file(value: Any) -> str:
    if not isinstance(value,str) or not value or '\\' in value or ':' in value or any(ord(c)<32 for c in value):
        raise ValueError('Files must be canonical relative POSIX paths')
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or path.as_posix()!=value:
        raise ValueError('Relative file escaped or is not canonical')
    lower=[p.lower() for p in path.parts]
    if any(p.endswith((' ','.')) or any(c in p for c in '<>"|?*') or
           p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*('COM'+str(n) for n in range(1,10)),*('LPT'+str(n) for n in range(1,10))}
           for p in path.parts):
        raise ValueError('Nonportable Windows filename rejected')
    if any(p in FORBIDDEN or p.startswith('.env') or p.startswith('credentials') or p.startswith('secrets') for p in lower) or path.suffix.lower() in PRIVATE_SUFFIXES:
        raise ValueError('Private/configuration/generated file cannot enter a snapshot')
    return value


def source_file(folder: Path, relative: str) -> Path:
    relative_file(relative)
    folder=folder.resolve()
    path=folder/relative
    current=folder
    for part in PurePosixPath(relative).parts:
        current=current/part
        if current.is_symlink() or current.resolve()!=folder.joinpath(*current.relative_to(folder).parts):
            raise ValueError('Snapshot links or reparse paths forbidden')
    if not path.resolve().is_relative_to(folder):
        raise ValueError('Snapshot path escaped project')
    return path


def digest(records: dict[str,Any]) -> str:
    return hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def read_spec(raw: Any) -> dict:
    if not isinstance(raw,dict) or type(raw.get('schema_version')) is not int or raw['schema_version']!=1:
        raise ValueError('Task schema_version must be 1')
    result={'schema_version':1}
    for field in ('task_id','provider','model'):
        value=raw.get(field)
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}',value):
            raise ValueError('Invalid task identifier: '+field)
        result[field]=value
    prompt=raw.get('prompt')
    if not isinstance(prompt,str) or not 1<=len(prompt)<=16000:
        raise ValueError('Task prompt must have 1..16000 characters')
    result['prompt']=prompt
    for field in ('files','editable_files','test_files'):
        values=raw.get(field)
        if not isinstance(values,list) or not values or len(values)>1000:
            raise ValueError('Nonempty bounded file list required: '+field)
        values=[relative_file(v) for v in values]
        if len({v.casefold() for v in values})!=len(values):
            raise ValueError('Duplicate snapshot path')
        result[field]=sorted(values)
    selected=set(result['files'])
    if not set(result['editable_files']).issubset(selected) or not set(result['test_files']).issubset(selected) or set(result['editable_files']) & set(result['test_files']):
        raise ValueError('Editable/tests must be disjoint subsets of files')
    for field,default,minimum,maximum in [('timeout_seconds',180,1,600),('max_turns',8,1,100),('expected_tests',None,1,100000)]:
        value=raw.get(field,default)
        if field=='expected_tests' and value is None:
            result[field]=None; continue
        if type(value) is not int or not minimum<=value<=maximum:
            raise ValueError('Invalid task bound: '+field)
        result[field]=value
    result['expect_initial_failure']=raw.get('expect_initial_failure',True)
    if type(result['expect_initial_failure']) is not bool:
        raise ValueError('expect_initial_failure must be Boolean')
    edits=raw.get('mock_edits',[])
    if not isinstance(edits,list) or len(edits)>100:
        raise ValueError('Invalid mock edit list')
    result['mock_edits']=[]
    for edit in edits:
        if not isinstance(edit,dict) or relative_file(edit.get('path')) not in result['editable_files']:
            raise ValueError('Mock edits must target editable files')
        if any(not isinstance(edit.get(k),str) or not 1<=len(edit[k])<=10000 for k in ('old','new')):
            raise ValueError('Mock edits require bounded literal text')
        result['mock_edits'].append({k:edit[k] for k in ('path','old','new')})
    return result


def snapshot(project: Path, files: list[str]) -> tuple[dict[str,bytes],dict[str,str]]:
    payload={}; hashes={}; total=0
    for relative in files:
        path=source_file(project,relative)
        if not path.is_file() or path.stat().st_size>MAX_FILE:
            raise ValueError('Snapshot file missing or exceeds 1 MiB')
        with path.open('rb') as stream:
            content=stream.read(MAX_FILE+1)
        total+=len(content)
        if len(content)>MAX_FILE or total>MAX_TOTAL:
            raise ValueError('Snapshot exceeds size budget')
        if b'\0' in content:
            raise ValueError('Binary snapshot input rejected')
        payload[relative]=content
        hashes[relative]=hashlib.sha256(content).hexdigest()
    return payload,hashes


def write_snapshot(folder: Path, payload: dict[str,bytes]) -> None:
    folder.mkdir(parents=True,exist_ok=False)
    for name,data in payload.items():
        path=folder/relative_file(name)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)


def changes(folder: Path, expected: dict[str,str], editable: list[str]) -> dict:
    # Compare descendants of one canonical root: Windows 8.3 aliases are
    # alternate spellings, not links. Check actual reparse/link attributes
    # before resolving so a linked root is still rejected.
    folder = canonical_path(folder)
    altered=[]; missing=[]; unexpected=[]
    for name,wanted in expected.items():
        try:
            path=source_file(folder,name)
            actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and path.stat().st_size<=MAX_FILE else None
        except (OSError,ValueError):
            actual=None
        if actual!=wanted:
            (missing if actual is None else altered).append(name)
    # Only interpreter/pytest cache directories are incidental generated files.
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in list(dirs):
            path=Path(root)/name
            if name in ('__pycache__','.pytest_cache'):
                dirs.remove(name)
            elif path.is_symlink() or path.resolve()!=path.absolute():
                unexpected.append(path.relative_to(folder).as_posix())
                dirs.remove(name)
        for name in files:
            path=Path(root)/name
            relative=path.relative_to(folder).as_posix()
            if path.is_symlink() or relative not in expected:
                unexpected.append(relative)
    forbidden=sorted((set(altered)|set(missing))-set(editable))
    return {'changed_files':sorted(altered),'missing_files':sorted(missing),'unexpected_files':sorted(set(unexpected)),
            'protected_files_changed':forbidden,'only_allowed_changes':not forbidden and not missing and not unexpected}
