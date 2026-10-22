[CmdletBinding()]
param(
  [switch]$DryRun,
  [switch]$SkipInstall
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Venv = Join-Path $Repo '.venv'
$Python = $null

function Invoke-Checked([string]$File, [string[]]$Args) {
  if ($DryRun) { Write-Host "+ $File $($Args -join ' ')"; return }
  & $File @Args
  if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}
function Find-SupportedPython {
  foreach ($candidate in @(@('py','-3.12'), @('py','-3.11'), @('py','-3.10'), @('python'))) {
    try {
      $text = (& $candidate[0] $candidate[1] '--version' 2>&1 | Out-String).Trim()
      if ($text -match 'Python\s+3\.(10|11|12)(?:\.|$)') { return @($candidate[0], $candidate[1]) }
    } catch { }
  }
  throw 'Supported Python 3.10, 3.11, or 3.12 was not found. Install it per-user and rerun; no machine-level install is performed.'
}
$Python = Find-SupportedPython
Write-Host "Repository: $Repo"
Write-Host "Python: $($Python -join ' ')"
try { $NodeVersion = (& node --version 2>&1 | Out-String).Trim() } catch { throw 'Node.js ^20.19.0 or >=22.12.0 is required; install it per-user and rerun.' }
if ($NodeVersion -notmatch '^v?(20\.(1[9]|[2-9][0-9])|2[2-9]\.|[3-9][0-9]\.)') { throw "Unsupported Node.js version: $NodeVersion (need ^20.19.0 or >=22.12.0)." }
Write-Host "Node: $NodeVersion"
if (-not $SkipInstall) {
  if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) { Invoke-Checked $Python[0] @($Python[1], '-m', 'venv', $Venv) }
  $Vpy = Join-Path $Venv 'Scripts\python.exe'
  Invoke-Checked $Vpy @('-m','pip','install','--upgrade','pip')
  if (-not $DryRun) { Push-Location $Repo; try { Invoke-Checked $Vpy @('-m','pip','install','.[d3m0n]'); Invoke-Checked 'npm' @('ci','--prefix','frontend'); Invoke-Checked 'npm' @('--prefix','frontend','run','build') } finally { Pop-Location } }
  else { Write-Host '+ npm ci --prefix frontend'; Write-Host '+ npm --prefix frontend run build' }
} else { Write-Host 'Skip-install: venv/package/frontend writes were skipped.' }
if (-not (Test-Path (Join-Path $Repo '.env'))) { Write-Host 'No .env exists; leaving configuration uncreated. Copy .env.example manually if needed.' } else { Write-Host 'Existing .env preserved.' }
Write-Host "Next: $Venv\Scripts\python.exe -m ops.doctor; $Venv\Scripts\python.exe -m ops.launch --setup-password"
