param(
    [string]$Project = (Get-Location).Path,
    [string]$Python = (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"),
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Installer = Join-Path $PSScriptRoot "installer.py"
$Arguments = @($Installer, "--project", $Project, "--python", $Python)
if ($Uninstall) { $Arguments += "--uninstall" }
& $Python @Arguments
if ($LASTEXITCODE -ne 0) { throw "TokenGuard installation failed ($LASTEXITCODE)" }
Write-Host "Project-local hook updated. Restart Claude Code to load settings."
