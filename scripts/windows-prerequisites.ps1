[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$InstallOptionalGit
)

# Guided, non-elevating prerequisite check for the native Windows dashboard.
# Machine/User PATH values are read only; fallback Platform-Tools is user-local
# and is added to this PowerShell process only.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Compatibility = Get-Content (Join-Path $ProjectRoot 'ops/install-compatibility.json') -Raw | ConvertFrom-Json

function Write-Plan([string]$Message) { Write-Host ('[plan] ' + $Message) }
function Get-VersionObject([string]$Text) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $m = [regex]::Match($Text, '(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?')
    if (-not $m.Success) { return $null }
    $patch = if ($m.Groups[3].Success) { $m.Groups[3].Value } else { '0' }
    try { return (New-Object System.Version ($m.Groups[1].Value + '.' + $m.Groups[2].Value + '.' + $patch)) } catch { return $null }
}
function Invoke-VersionProbe([string]$FilePath, [string[]]$Arguments) {
    try {
        $out = @(& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() }) -join "`n"
        $version = Get-VersionObject $out
        if ($version -ne $null) { return [pscustomobject]@{ Path = $FilePath; Arguments = $Arguments; Version = $version; Text = $out } }
    } catch { }
    return $null
}
function Refresh-ProcessPath {
    $values = @([Environment]::GetEnvironmentVariable('Path', 'Process'), [Environment]::GetEnvironmentVariable('Path', 'User'), [Environment]::GetEnvironmentVariable('Path', 'Machine'))
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($value in $values) {
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            foreach ($part in $value.Split(';')) { $trimmed = $part.Trim(); if ($trimmed -and $seen.Add($trimmed)) { [void]$parts.Add($trimmed) } }
        }
    }
    $env:Path = [string]::Join(';', $parts.ToArray())
}
function Add-ProcessPath([string]$Directory) {
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return }
    $parts = @($env:Path -split ';' | Where-Object { $_ })
    if (-not ($parts | Where-Object { $_.TrimEnd('\') -ieq $Directory.TrimEnd('\') })) { $env:Path = $Directory + ';' + $env:Path }
}
function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($guided in $Compatibility.python.guided) {
        [void]$candidates.Add(@{ Name = 'py.exe'; Args = @('-' + $guided, '--version') })
    }
    [void]$candidates.Add(@{ Name = 'python.exe'; Args = @('--version') })
    [void]$candidates.Add(@{ Name = 'python3.exe'; Args = @('--version') })
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) {
            $probe = Invoke-VersionProbe $cmd.Source $candidate.Args
            if ($probe -and (python_supported_version $probe.Version)) {
                $runArgs = @($candidate.Args | Where-Object { $_ -ne '--version' })
                return [pscustomobject]@{ Path = $cmd.Source; Arguments = $runArgs; Version = $probe.Version }
            }
        }
    }
    return $null
}
function python_supported_version($Version) {
    foreach ($guided in $Compatibility.python.guided) {
        $v = Get-VersionObject $guided
        if ($v -and $Version.Major -eq $v.Major -and $Version.Minor -eq $v.Minor) { return $true }
    }
    return $false
}
function node_supported_version($Version) {
    foreach ($range in $Compatibility.node.ranges) {
        $text = [string]$range
        if ($text -match '^(\d+)\.(\d+)\.0-(\d+)\.x$') {
            if ($Version.Major -eq [int]$Matches[1] -and $Version.Minor -ge [int]$Matches[2]) { return $true }
        } elseif ($text -match '^>=(\d+)\.(\d+)\.(\d+)$') {
            if ($Version -ge (New-Object System.Version ($Matches[1] + '.' + $Matches[2] + '.' + $Matches[3]))) { return $true }
        }
    }
    return $false
}
function Find-Node {
    $cmd = Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    $probe = Invoke-VersionProbe $cmd.Source @('--version')
    if (-not $probe) { return $null }
    $ok = node_supported_version $probe.Version
    return [pscustomobject]@{ Path = $probe.Path; Arguments = $probe.Arguments; Version = $probe.Version; Text = $probe.Text; Unsupported = (-not $ok) }
}
function Find-CommandVersion([string]$Name, [string[]]$Arguments) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    return (Invoke-VersionProbe $cmd.Source $Arguments)
}
function Get-AdbCapabilities($Adb, $Python) {
    if (-not $Adb -or -not $Python) { return $null }
    try {
        $code = 'import json,sys; from ops.compatibility import probe_adb; print(json.dumps(probe_adb(sys.argv[1])))'
        $raw = @(& $Python.Path @($Python.Arguments) '-c' $code $Adb.Path 2>$null) -join "`n"
        if ($raw) { return ($raw | ConvertFrom-Json) }
    } catch { }
    return $null
}
function Show-Status($Python, $Node, $Npm, $Adb, $AdbCapabilities, $Git) {
    if ($Python) { Write-Host ("Python {0} OK ({1})" -f $Python.Version, $Python.Path) } else { Write-Host ("Python {0}: MISSING or unsupported" -f (($Compatibility.python.guided -join '/'))) }
    if ($Node -and -not $Node.Unsupported) { Write-Host ("Node.js {0} OK" -f $Node.Version) } elseif ($Node) { Write-Host ("Node.js {0}: below supported floor" -f $Node.Version) } else { Write-Host 'Node.js: MISSING' }
    if ($Npm) { Write-Host ("npm {0} OK" -f $Npm.Version) } else { Write-Host 'npm: MISSING (normally supplied with Node.js)' }
    if ($Adb -and $AdbCapabilities -and $AdbCapabilities.capabilities.required_for_install) { Write-Host ("adb ready for required control and pairing ({0})" -f $Adb.Path) }
    elseif ($Adb -and $AdbCapabilities) { Write-Host ("adb available but blocked: missing {0}" -f (($AdbCapabilities.capabilities.missing -join ', '))) }
    else { Write-Host 'adb / Android Platform-Tools: MISSING or capability probe unavailable' }
    if ($Git) { Write-Host ("Git {0} available (optional)" -f $Git.Version) } else { Write-Host 'Git: not found (optional; ZIP install can continue)' }
}
function Invoke-WingetPackage([string]$Id, [string]$Label) {
    $scope = if ($Id -eq 'OpenJS.NodeJS.LTS') { 'machine' } elseif ($Id -eq 'Python.Python.3.12') { 'user' } else { 'package-default' }
    $args = @('install', '--id', $Id, '--exact', '--source', 'winget', '--interactive')
    if ($scope -ne 'package-default') { $args += @('--scope', $scope) }
    Write-Host ("Installing {0} with winget ID {1} ({2} scope; follow its license/UAC prompts)..." -f $Label, $Id, $scope)
    & winget @args
    $code = $LASTEXITCODE
    if ($code -eq 3010 -or $code -eq 1641) { Write-Warning "$Label requested a reboot. Reboot, then rerun install.cmd."; return 3010 }
    return $code
}
function Invoke-VerifiedAdbFallback($Python) {
    if (-not $Python) { Write-Warning 'Verified ADB fallback requires supported Python; install Python and rerun.'; return $false }
    if ($env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'x86_64')) { Write-Warning "Unsupported Windows architecture $env:PROCESSOR_ARCHITECTURE; no network request made."; return $false }
    $spec = $Compatibility.platform_tools.windows.x86_64
    $destination = Join-Path $env:LOCALAPPDATA 'D3m0n\android'
    $installer = Join-Path $ProjectRoot 'scripts\install_platform_tools.py'
    Write-Host ("Installing verified Platform-Tools {0} user-locally (SHA-256 {1})..." -f $Compatibility.platform_tools.version, $spec.sha256)
    & $Python.Path @($Python.Arguments) $installer '--platform' 'windows' '--arch' 'x86_64' '--destination' $destination
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Verified Platform-Tools installer failed; no unverified ADB was installed.'; return $false }
    Add-ProcessPath (Join-Path $destination 'platform-tools')
    return $true
}

Refresh-ProcessPath
$python = Find-Python
$node = Find-Node
$npm = Find-CommandVersion 'npm.cmd' @('--version')
$adb = Find-CommandVersion 'adb.exe' @('version')
$adbCapabilities = Get-AdbCapabilities $adb $python
$git = Find-CommandVersion 'git.exe' @('--version')
Write-Host 'D3m0n Windows prerequisite check'
Show-Status $python $node $npm $adb $adbCapabilities $git
$adbFallbackNeeded = (-not $adb -or -not $adbCapabilities -or -not $adbCapabilities.capabilities.required_for_install)
$plans = New-Object System.Collections.Generic.List[object]
if (-not $python) { [void]$plans.Add([pscustomobject]@{ Label = 'Python'; Id = 'Python.Python.3.12'; Manual = 'Install a guided Python version from https://www.python.org/downloads/windows/.' }) }
if (-not $node -or $node.Unsupported -or -not $npm) { [void]$plans.Add([pscustomobject]@{ Label = 'Node.js LTS (includes npm)'; Id = 'OpenJS.NodeJS.LTS'; Manual = 'Install the current LTS from https://nodejs.org/en/download/; npm is bundled.' }) }
if (-not $git -and $InstallOptionalGit) { [void]$plans.Add([pscustomobject]@{ Label = 'Git (optional)'; Id = 'Git.Git'; Manual = 'Install Git from https://git-scm.com/download/win; Git is not required for a downloaded ZIP.' }) }
if ($adbFallbackNeeded) { Write-Plan ("ADB missing/old: verified user-local fallback uses {0} with SHA-256 {1}; no global PATH write." -f $Compatibility.platform_tools.windows.x86_64.url, $Compatibility.platform_tools.windows.x86_64.sha256) }
if ($plans.Count -eq 0 -and -not $adbFallbackNeeded) { Write-Host 'All required native prerequisites are ready. Git is optional.'; if ($DryRun) { Write-Host 'DryRun: no installer or package manager was invoked.' }; exit 0 }
Write-Host ''; Write-Host 'Missing prerequisite plan (no changes have been made yet):'
foreach ($plan in $plans) { Write-Plan ("{0}: winget install --id {1} --exact; follow visible license/UAC prompts; manual alternative: {2}" -f $plan.Label, $plan.Id, $plan.Manual) }
Write-Plan 'Fleet AI is optional and separate; this installer does not install WSL, import/reset distributions, or configure a live server.'
if ($DryRun) { Write-Host 'DryRun: plan reported; no prompt, network install, OS package install, PATH write, or parent installer call was made.'; exit 0 }
$winget = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($plans.Count -gt 0 -and -not $winget) { Write-Warning 'winget.exe is unavailable for the listed Python/Node/Git plan. No bootstrap or remote script was run.'; Write-Host 'Manual action: install/enable App Installer, or follow each official URL above. Verified ADB fallback can run after supported Python is available.'; exit 2 }
$answer = ''
if (-not $Yes) { $answer = Read-Host 'Proceed with the listed prerequisite plan (visible package/UAC prompts; user-local ADB fallback)? [y/N]' }
if ($answer -notmatch '^(?i:y|yes)$' -and -not $Yes) { Write-Host 'No changes made. Rerun and answer Yes after reviewing the plan.'; exit 2 }
$reboot = $false
foreach ($plan in $plans) {
    $result = Invoke-WingetPackage $plan.Id $plan.Label
    if ($result -eq 3010) { $reboot = $true; break }
    if ($result -ne 0) { Write-Warning ("{0} did not complete (exit code {1}); existing software was not removed." -f $plan.Label, $result); exit $result }
    Refresh-ProcessPath
}
if ($reboot) { Write-Warning 'A prerequisite installer requested a reboot. Reboot, then rerun install.cmd.'; exit 3010 }
Refresh-ProcessPath
$python = Find-Python; $node = Find-Node; $npm = Find-CommandVersion 'npm.cmd' @('--version'); $adb = Find-CommandVersion 'adb.exe' @('version'); $adbCapabilities = Get-AdbCapabilities $adb $python
$adbFallbackNeeded = (-not $adb -or -not $adbCapabilities -or -not $adbCapabilities.capabilities.required_for_install)
if ($adbFallbackNeeded -and -not (Invoke-VerifiedAdbFallback $python)) { exit 2 }
Refresh-ProcessPath
$python = Find-Python; $node = Find-Node; $npm = Find-CommandVersion 'npm.cmd' @('--version'); $adb = Find-CommandVersion 'adb.exe' @('version'); $adbCapabilities = Get-AdbCapabilities $adb $python
Show-Status $python $node $npm $adb $adbCapabilities $git
if (-not $python -or -not $node -or $node.Unsupported -or -not $npm -or -not $adb -or -not $adbCapabilities -or -not $adbCapabilities.capabilities.required_for_install) { Write-Warning 'One or more required tools are still unavailable. Reopen PowerShell and rerun install.cmd.'; exit 2 }
exit 0
