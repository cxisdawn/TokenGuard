"""Run pytest and expose bounded failure details as GitHub check annotations."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def escaped(value: str) -> str:
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='tokenguard-ci-') as folder:
        report = Path(folder) / 'pytest.xml'
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--junitxml', str(report)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        if result.returncode and os.environ.get('GITHUB_ACTIONS') == 'true' and report.exists():
            annotations = 0
            for case in ET.parse(report).iter('testcase'):
                failure = case.find('failure')
                if failure is None:
                    failure = case.find('error')
                if failure is None:
                    continue
                label = case.get('classname', '') + '.' + case.get('name', '')
                detail = (failure.text or failure.get('message', ''))[-6000:]
                print('::error title=pytest failure::' + escaped(label + '\n' + detail))
                annotations += 1
                if annotations >= 10:
                    break
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
