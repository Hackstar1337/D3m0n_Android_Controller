[CmdletBinding()]
param(
  [switch]$DryRun,
  [switch]$Rollback,
  [switch]$EnableSocket,
  [switch]$Probe,
  [string]$Distro = 'D3m0nFleet',
  [string]$Operator,
  [string]$Source,
  [string]$SdkSource,
  [string]$SdkVenv,
  [string]$Bundle = (Join-Path $PSScriptRoot '..\runtime\host-isolation'),
  [string]$ParentConfigPath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'D3m0n\fleet-runtime-wsl2.json')
)
$ErrorActionPreference = 'Stop'
$FixedRoot = '/tmp/d3m0n-fleet-runtime'
$FixedLinuxInstaller = "$FixedRoot/scripts/install-fleet-runtime.sh"
$FixedLinuxConfig = '/etc/d3m0n-ai-worker/runtime.json'
$FixedClient = '/usr/local/libexec/d3m0n-ai-worker-client'

function Info([string]$Message) { Write-Host "[fleet-runtime] $Message" }
function Fail([string]$Message) { throw "[fleet-runtime] $Message" }
function Clean-Text([object]$Value) {
  if ($null -eq $Value) { return '' }
  return (([string]$Value) -replace "`0", '').Trim()
}
function Invoke-WslRead([string[]]$Arguments) {
  $text = (& wsl.exe @Arguments 2>&1 | Out-String)
  $code = $LASTEXITCODE
  $clean = Clean-Text $text
  if ($code -ne 0) { Fail "fixed WSL prerequisite command failed: $($Arguments -join ' ')" }
  return $clean
}
function Invoke-WslCopy([string[]]$Arguments) {
  if ($DryRun) { Info "dry-run: would call wsl.exe $($Arguments -join ' ')"; return }
  & wsl.exe @Arguments
  if ($LASTEXITCODE -ne 0) { Fail "fixed WSL transfer command failed: $($Arguments -join ' ')" }
}
function Convert-ToWslPath([string]$WindowsPath) {
  $full = [IO.Path]::GetFullPath($WindowsPath)
  $out = Invoke-WslRead @('-d', $Distro, '--exec', '/usr/bin/wslpath', '-a', '-u', '--', $full)
  if ([string]::IsNullOrWhiteSpace($out) -or $out -notmatch '^/') { Fail "could not translate local path for WSL: $full" }
  return $out
}
function Test-AccountName([string]$Name, [string]$Field) {
  if ([string]::IsNullOrWhiteSpace($Name) -or $Name -notmatch '^[A-Za-z][A-Za-z0-9._-]{0,62}$' -or $Name -eq 'root') { Fail "$Field must be an existing non-root account name" }
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Fail 'wsl.exe is missing; install/enable WSL2 manually. This script never changes Windows features.' }
Test-AccountName $Distro 'Distro'
if ($Rollback -and $Probe) { Fail '--rollback and --probe cannot be combined' }
if ($Probe -and -not $EnableSocket) { Fail '--Probe requires explicit --EnableSocket' }
if (-not $Rollback) {
  Test-AccountName $Operator 'Operator'
  foreach ($path in @($Bundle, $Source, $SdkSource, $SdkVenv)) {
    if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path -PathType Container)) { Fail "required local directory is missing: $path" }
  }
}
if (-not (Test-Path -LiteralPath $Bundle -PathType Container)) { Fail "reviewed host-isolation bundle is missing: $Bundle" }

$versionText = Invoke-WslRead @('--version')
$versionMatch = [regex]::Match($versionText, '(?im)WSL\s+version\s*:\s*(\d+)\.(\d+)\.(\d+)')
if (-not $versionMatch.Success) { Fail 'could not parse verbose WSL version output (UTF16/NUL-cleaned)'
}
$version = [version]::new([int]$versionMatch.Groups[1].Value, [int]$versionMatch.Groups[2].Value, [int]$versionMatch.Groups[3].Value)
if ($version -lt [version]'0.67.6') { Fail "WSL $version is too old; WSL 0.67.6+ is required for systemd" }
$null = Invoke-WslRead @('--status')
$distroText = Invoke-WslRead @('--list', '--quiet')
$distros = @($distroText -split "`r?`n" | ForEach-Object { Clean-Text $_ } | Where-Object { $_ })
if ($distros -notcontains $Distro) { Fail "dedicated distro '$Distro' is not installed; import/bootstrap it explicitly as the owner" }
$pid1 = Invoke-WslRead @('-d', $Distro, '--exec', '/usr/bin/cat', '/proc/1/comm')
if ($pid1 -ne 'systemd') { Fail "dedicated distro '$Distro' is not running systemd as PID 1" }
if (-not $Rollback) {
  $uidText = Invoke-WslRead @('-d', $Distro, '--exec', '/usr/bin/id', '-u', '--', $Operator)
  [int]$uid = 0
  if (-not [int]::TryParse($uidText, [ref]$uid) -or $uid -le 0) { Fail "WSL operator '$Operator' is missing or resolves to root" }
  $groupText = Invoke-WslRead @('-d', $Distro, '--exec', '/usr/bin/id', '-gn', '--', $Operator)
  Test-AccountName $groupText 'Operator primary group'
}

$windowsScript = Join-Path $PSScriptRoot 'install-fleet-runtime.sh'
if (-not (Test-Path -LiteralPath $windowsScript -PathType Leaf)) { Fail "Linux installer is missing: $windowsScript" }
if ($DryRun) {
  Info "validated WSL $version, dedicated distro '$Distro', systemd PID1, and non-root operator '$Operator'"
  Info "dry-run: no WSL copy, sudo/root command, Windows config write, feature change, password setup, worker, model, ADB, phone, or provider action"
  Info "would invoke only fixed Linux installer: $FixedLinuxInstaller"
  exit 0
}

$scriptWsl = Convert-ToWslPath $windowsScript
$bundleWsl = Convert-ToWslPath $Bundle
$sourceWsl = Convert-ToWslPath $Source
$sdkSourceWsl = Convert-ToWslPath $SdkSource
$sdkVenvWsl = Convert-ToWslPath $SdkVenv
Invoke-WslCopy @('-d', $Distro, '--exec', '/usr/bin/mkdir', '-p', "$FixedRoot/scripts", "$FixedRoot/runtime/host-isolation", "$FixedRoot/inputs/source", "$FixedRoot/inputs/sdk-source", "$FixedRoot/inputs/sdk-venv")
Invoke-WslCopy @('-d', $Distro, '--exec', '/usr/bin/cp', '-a', '--', $scriptWsl, $FixedLinuxInstaller)
Invoke-WslCopy @('-d', $Distro, '--exec', '/usr/bin/cp', '-a', '--', "$bundleWsl/.", "$FixedRoot/runtime/host-isolation/")
if (-not $Rollback) {
  Invoke-WslCopy @('-d', $Distro, '--exec', '/usr/bin/cp', '-a', '--', "$sourceWsl/.", "$FixedRoot/inputs/source/")
  Invoke-WslCopy @('-d', $Distro, '--exec', '/usr/bin/cp', '-a', '--', "$sdkSourceWsl/.", "$FixedRoot/inputs/sdk-source/")
  Invoke-WslCopy @('-d', $Distro, '--exec', '/usr/bin/cp', '-a', '--', "$sdkVenvWsl/.", "$FixedRoot/inputs/sdk-venv/")
}
$linuxArgs = @('-d', $Distro, '--exec', '/usr/bin/sudo', '-n', '--', $FixedLinuxInstaller)
if ($Rollback) {
  $linuxArgs += '--rollback'
} else {
  $linuxArgs += @('--operator', $Operator, '--source', "$FixedRoot/inputs/source", '--sdk-source', "$FixedRoot/inputs/sdk-source", '--sdk-venv', "$FixedRoot/inputs/sdk-venv")
  if ($EnableSocket) { $linuxArgs += '--enable-socket' }
  if ($Probe) { $linuxArgs += '--probe' }
}
Info "executing only the fixed Linux installer through explicit sudo in dedicated distro '$Distro'"
& wsl.exe @linuxArgs
if ($LASTEXITCODE -ne 0) { Fail 'fixed Linux installer failed; no native Windows worker fallback exists' }

$configFull = [IO.Path]::GetFullPath($ParentConfigPath)
$config = [ordered]@{
  mode = 'wsl2-host-isolation'
  config_path = $configFull
  linux_config_path = $FixedLinuxConfig
  wsl_distro = $Distro
  worker_user = $Operator
  worker_group = $Operator
  client_path = $FixedClient
  command = @('wsl.exe', '-d', $Distro, '-u', $Operator, '--exec', $FixedClient)
  note = 'Windows parent metadata; config_path is a Windows file. linux_config_path is consumed only inside WSL.'
}
$parentDir = Split-Path -Parent $configFull
if (-not (Test-Path -LiteralPath $parentDir)) { New-Item -ItemType Directory -Force -Path $parentDir | Out-Null }
$config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configFull -Encoding UTF8
Info "wrote nonsecret Windows parent config outside the repository: $configFull"
Info 'FleetAI activation remains owner-controlled; this installer never enables app activation or starts a model.'
