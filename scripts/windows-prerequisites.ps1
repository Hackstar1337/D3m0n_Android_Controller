[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$InstallOptionalGit
)

# Guided, non-elevating prerequisite check for the native Windows dashboard.
# This file deliberately has no persistent environment/configuration writes.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'

function Write-Plan([string]$Message) {
    Write-Host ('[plan] ' + $Message)
}

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
        if ($version -ne $null) {
            return [pscustomobject]@{ Path = $FilePath; Arguments = $Arguments; Version = $version; Text = $out }
        }
    } catch { }
    return $null
}

function Refresh-ProcessPath {
    # Only this PowerShell process is changed; User/Machine PATH values are read, never written.
    $values = @(
        [Environment]::GetEnvironmentVariable('Path', 'Process'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
    )
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($value in $values) {
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            foreach ($part in $value.Split(';')) {
                $trimmed = $part.Trim()
                if ($trimmed -and $seen.Add($trimmed)) { [void]$parts.Add($trimmed) }
            }
        }
    }
    $env:Path = [string]::Join(';', $parts.ToArray())
}

function Find-Python {
    $candidates = @(
        @{ Name = 'py.exe'; Args = @('-3.12', '--version') },
        @{ Name = 'py.exe'; Args = @('-3.11', '--version') },
        @{ Name = 'python.exe'; Args = @('--version') },
        @{ Name = 'python3.exe'; Args = @('--version') }
    )
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) {
            $probe = Invoke-VersionProbe $cmd.Source $candidate.Args
            if ($probe -and (($probe.Version.Major -eq 3) -and (($probe.Version.Minor -eq 11) -or ($probe.Version.Minor -eq 12)))) {
                $runArgs = @($candidate.Args | Where-Object { $_ -ne '--version' })
                return [pscustomobject]@{ Path = $cmd.Source; Arguments = $runArgs; Version = $probe.Version }
            }
        }
    }
    return $null
}

function Find-Node {
    $cmd = Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    $probe = Invoke-VersionProbe $cmd.Source @('--version')
    if (-not $probe) { return $null }
    # Supported project floor: Node 22.12+, or Node 20.19+.
    $ok = (($probe.Version.Major -eq 22 -and $probe.Version -ge (New-Object System.Version '22.12.0')) -or
           ($probe.Version.Major -eq 20 -and $probe.Version -ge (New-Object System.Version '20.19.0')) -or
           ($probe.Version.Major -gt 22))
    if ($ok) { return [pscustomobject]@{ Path = $probe.Path; Arguments = $probe.Arguments; Version = $probe.Version; Text = $probe.Text; Unsupported = $false } }
    return [pscustomobject]@{ Path = $cmd.Source; Version = $probe.Version; Unsupported = $true }
}

function Find-CommandVersion([string]$Name, [string[]]$Arguments) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    return (Invoke-VersionProbe $cmd.Source $Arguments)
}

function Show-Status($Python, $Node, $Npm, $Adb, $Git) {
    if ($Python) { Write-Host ("Python {0} OK ({1})" -f $Python.Version, $Python.Path) } else { Write-Host 'Python 3.11/3.12: MISSING or unsupported' }
    if ($Node -and -not $Node.Unsupported) { Write-Host ("Node.js {0} OK" -f $Node.Version) } elseif ($Node) { Write-Host ("Node.js {0}: below supported floor" -f $Node.Version) } else { Write-Host 'Node.js 22.12+ (or 20.19+): MISSING' }
    if ($Npm) { Write-Host ("npm {0} OK" -f $Npm.Version) } else { Write-Host 'npm: MISSING (normally supplied with Node.js)' }
    if ($Adb) { Write-Host ("adb available ({0})" -f $Adb.Path) } else { Write-Host 'adb / Android Platform-Tools: MISSING' }
    if ($Git) { Write-Host ("Git {0} available (optional)" -f $Git.Version) } else { Write-Host 'Git: not found (optional; ZIP install can continue)' }
}

function Invoke-WingetPackage([string]$Id, [string]$Label) {
    # --interactive intentionally keeps package license and UAC prompts visible.
    $scope = if ($Id -eq 'OpenJS.NodeJS.LTS') { 'machine' } elseif ($Id -eq 'Python.Python.3.12') { 'user' } else { 'package-default' }
    $args = @('install', '--id', $Id, '--exact', '--source', 'winget', '--interactive')
    if ($scope -ne 'package-default') { $args += @('--scope', $scope) }
    Write-Host ("Installing {0} with winget ID {1} ({2} scope; follow its license/UAC prompts)..." -f $Label, $Id, $scope)
    & winget @args
    $code = $LASTEXITCODE
    if ($code -eq 3010 -or $code -eq 1641) {
        Write-Warning "$Label requested a reboot. Reboot, then rerun install.cmd."
        return 3010
    }
    return $code
}

Refresh-ProcessPath
$python = Find-Python
$node = Find-Node
$npm = Find-CommandVersion 'npm.cmd' @('--version')
$adb = Find-CommandVersion 'adb.exe' @('version')
$git = Find-CommandVersion 'git.exe' @('--version')

Write-Host 'D3m0n Windows prerequisite check'
Show-Status $python $node $npm $adb $git

$plans = New-Object System.Collections.Generic.List[object]
if (-not $python) { [void]$plans.Add([pscustomobject]@{ Label = 'Python 3.12'; Id = 'Python.Python.3.12'; Manual = 'Install Python 3.12 from https://www.python.org/downloads/windows/ and select the per-user option; then reopen this installer.' }) }
if (-not $node -or $node.Unsupported -or -not $npm) { [void]$plans.Add([pscustomobject]@{ Label = 'Node.js LTS (includes npm)'; Id = 'OpenJS.NodeJS.LTS'; Manual = 'Install the current LTS from https://nodejs.org/en/download/; npm is bundled.' }) }
if (-not $adb) { [void]$plans.Add([pscustomobject]@{ Label = 'Android SDK Platform-Tools (adb)'; Id = 'Google.PlatformTools'; Manual = 'Install the official ZIP from https://dl.google.com/android/repository/platform-tools-latest-windows.zip (linked by https://developer.android.com/tools/releases/platform-tools), extract it to a folder you own, and add that folder to your user PATH.' }) }
if (-not $git -and $InstallOptionalGit) { [void]$plans.Add([pscustomobject]@{ Label = 'Git (optional)'; Id = 'Git.Git'; Manual = 'Install Git from https://git-scm.com/download/win; Git is not required for a downloaded ZIP.' }) }

if ($plans.Count -eq 0) {
    Write-Host 'All required native prerequisites are ready. Git is optional.'
    if ($DryRun) { Write-Host 'DryRun: no installer or package manager was invoked.' }
    exit 0
}

Write-Host ''
Write-Host 'Missing prerequisite plan (no changes have been made):'
foreach ($plan in $plans) {
    $scope = if ($plan.Id -eq 'OpenJS.NodeJS.LTS') { 'machine (UAC confirmation)' } elseif ($plan.Id -eq 'Python.Python.3.12') { 'user' } else { 'package-default (follow visible prompts)' }
    Write-Plan ("{0}: winget install --id {1} --exact; scope: {2}; manual alternative: {3}" -f $plan.Label, $plan.Id, $scope, $plan.Manual)
}
Write-Plan 'WinGet package installs are individual and interactive: package licenses and UAC prompts remain visible; no --silent, --force, or agreement suppression is used.'
Write-Plan 'Fleet AI is optional and separate; this installer does not install WSL, import/reset distributions, or configure a live server.'

if ($DryRun) {
    Write-Host 'DryRun: plan reported; no prompt, network install, OS package install, PATH write, or parent installer call was made.'
    exit 0
}

$winget = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $winget) {
    Write-Warning 'winget.exe is unavailable. No bootstrap or remote script was run.'
    Write-Host 'Manual action: install/enable App Installer from the Microsoft Store or your organization-managed Windows image, then rerun this installer. Alternatively follow each official URL above.'
    exit 2
}

$answer = ''
if (-not $Yes) { $answer = Read-Host 'Proceed with the listed winget plan (Node may request UAC)? [y/N]' }
if ($answer -notmatch '^(?i:y|yes)$' -and -not $Yes) {
    Write-Host 'No changes made. Rerun and answer Yes after reviewing the plan.'
    exit 2
}

$reboot = $false
foreach ($plan in $plans) {
    $result = Invoke-WingetPackage $plan.Id $plan.Label
    if ($result -eq 3010) { $reboot = $true; break }
    if ($result -ne 0) { Write-Warning ("{0} did not complete (exit code {1}). Existing software was not removed or changed by this script." -f $plan.Label, $result); exit $result }
    Refresh-ProcessPath
}
if ($reboot) { Write-Warning 'A prerequisite installer requested a reboot. Reboot, then rerun install.cmd.'; exit 3010 }

Refresh-ProcessPath
$python = Find-Python
$node = Find-Node
$npm = Find-CommandVersion 'npm.cmd' @('--version')
$adb = Find-CommandVersion 'adb.exe' @('version')
Show-Status $python $node $npm $adb $git
if (-not $python -or -not $node -or $node.Unsupported -or -not $npm -or -not $adb) {
    Write-Warning 'One or more required tools are still unavailable. Reopen PowerShell (or reboot if requested) and rerun install.cmd.'
    exit 2
}
exit 0



