"""Repeat v0.3 metadata overhead finding without modifying old sources/data."""
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = r'''
import json, os, tempfile
from tokenguard.core import compress
from tokenguard.hook import handle
raw='pulse\n'*87+'tail:'+'x'*(3000-6*87-6)+'\n'
with tempfile.TemporaryDirectory() as folder:
    os.environ['TOKENGUARD_DATA_DIR']=folder
    core,reason=compress(raw)
    result=handle({'tool_name':'Bash','tool_input':{'command':'python noisy.py'},
                   'tool_response':{'stdout':raw},'session_id':'manual-review-net-saving'})
    out=result['hookSpecificOutput']['updatedToolOutput']['stdout']
    print(json.dumps({'original_chars':len(raw),'core_chars':len(core),'emitted_chars':len(out),'net_saved_chars':len(raw)-len(out)}))
'''

if __name__=='__main__':
    old=Path(__file__).resolve().parent.parent/'tokenguard_v03'
    result=subprocess.run([sys.executable,'-c',SOURCE],cwd=old,capture_output=True,text=True,check=True)
    print(result.stdout,end='')
