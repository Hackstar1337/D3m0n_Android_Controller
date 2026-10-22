[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw "Missing $Python; run scripts/bootstrap.ps1 first." }
Push-Location $Repo
try {
  & $Python -m ops.launch @Arguments
  $Code = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $Code
