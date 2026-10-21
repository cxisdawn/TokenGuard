param([string]$Target = "tg-fixture")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path $Target -Force | Out-Null
$TestFile = Join-Path $Target 'test_calc.py'
@'
import pytest

def add(a, b):
    return a - b if a == 7 else a + b  # intentional bug for x=7 only

@pytest.mark.parametrize("x", range(400))
def test_add(x):
    assert add(x, 1) == x + 1
'@ | Set-Content -Path $TestFile -Encoding utf8
$LogFile = Join-Path $Target 'noisy_check.py'
@'
for i in range(1500):
    print("HEARTBEAT ok")
print("Diagnostics complete")
'@ | Set-Content -Path $LogFile -Encoding utf8
Write-Host "Created: $TestFile and $LogFile"
Write-Host "Install pytest: python -m pip install pytest"
Write-Host "Run: python -m pytest -v $Target"
