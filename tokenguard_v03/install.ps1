param([string]$Project = (Get-Location).Path)
$ErrorActionPreference = "Stop"
$Python = (Get-Command python -ErrorAction Stop).Source
$Root = (Resolve-Path $PSScriptRoot).Path
$HookScript = Join-Path $Root "tokenguard\hook.py"
$SettingsDir = Join-Path $Project ".claude"
New-Item -ItemType Directory -Path $SettingsDir -Force | Out-Null
$SettingsFile = Join-Path $SettingsDir "settings.local.json"
if (Test-Path $SettingsFile) {
    Copy-Item $SettingsFile "$SettingsFile.backup" -Force
    $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json -AsHashtable
} else { $settings = @{} }
if (-not $settings.ContainsKey('hooks')) { $settings['hooks'] = @{} }
$hooks = $settings['hooks']
if (-not $hooks.ContainsKey('PostToolUse')) { $hooks['PostToolUse'] = @() }
# JSON shell quoting: command uses double quotes around absolute paths (Windows spaces)
$cmd = '"' + $Python + '" "' + $HookScript + '"'
$entry = @{matcher = 'Bash'; hooks = @(@{type = 'command'; command = $cmd; timeout = 10})}
$hooks['PostToolUse'] = @($hooks['PostToolUse'] | Where-Object { -not (($_.hooks | ConvertTo-Json -Compress) -match 'tokenguard[\\/]hook.py') }) + @($entry)
$settings | ConvertTo-Json -Depth 20 | Set-Content -Path $SettingsFile -Encoding utf8
Write-Host "Installed project-local TokenGuard Hook: $SettingsFile"
Write-Host "To disable: rename settings.local.json or remove the TokenGuard PostToolUse entry."
