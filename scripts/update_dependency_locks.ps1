[CmdletBinding()]
param([switch]$Upgrade)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = if ($env:D3M0N_PYTHON) { $env:D3M0N_PYTHON } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) {
    throw 'Guided Python 3.11/3.12 is required. Set D3M0N_PYTHON to its interpreter; this script does not install global toolchains.'
}
$version = & $Python -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")'
if ($version -notin @('3.11','3.12')) { throw "Unsupported lock-maintenance Python $version; use 3.11 or 3.12." }
$args = @((Join-Path $Root 'scripts/update_dependency_locks.py'))
if ($Upgrade) { $args += '--upgrade' }
& $Python @args
if ($LASTEXITCODE) { exit $LASTEXITCODE }
