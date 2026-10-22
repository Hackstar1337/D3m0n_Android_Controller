[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$NoStart,
    [int]$Port = 0,
    [switch]$NoBrowser,
    [switch]$InstallOptionalGit
)

# This is a local ZIP installer. It does not elevate, change execution policy persistently,
# edit firewall/WSL/live-server state, or run ADB device commands.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$prereq = Join-Path $repoRoot 'scripts\windows-prerequisites.ps1'
if (-not (Test-Path -LiteralPath $prereq -PathType Leaf)) { throw "Missing prerequisite checker: $prereq" }

# PowerShell 5.1 does not reliably splat positional script arguments; spell out
# the eight switch combinations so each switch binds by name.
if ($DryRun) {
    if ($Yes -and $InstallOptionalGit) { & $prereq -DryRun -Yes -InstallOptionalGit }
    elseif ($Yes) { & $prereq -DryRun -Yes }
    elseif ($InstallOptionalGit) { & $prereq -DryRun -InstallOptionalGit }
    else { & $prereq -DryRun }
} elseif ($Yes -and $InstallOptionalGit) { & $prereq -Yes -InstallOptionalGit }
elseif ($Yes) { & $prereq -Yes }
elseif ($InstallOptionalGit) { & $prereq -InstallOptionalGit }
else { & $prereq }
$preCode = $LASTEXITCODE
if ($preCode -ne 0) {
    if ($preCode -eq 3010) { Write-Warning 'A prerequisite requested a reboot. Reboot, then rerun install.cmd.' }
    exit $preCode
}
if ($DryRun) {
    Write-Host 'DryRun: prerequisite plan complete; ops.setup_manager was not called.'
    exit 0
}

function Get-VersionObject([string]$Text) {
    $m = [regex]::Match($Text, '(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?')
    if (-not $m.Success) { return $null }
    $patch = if ($m.Groups[3].Success) { $m.Groups[3].Value } else { '0' }
    try { return (New-Object System.Version ($m.Groups[1].Value + '.' + $m.Groups[2].Value + '.' + $patch)) } catch { return $null }
}
function Test-PythonCandidate([string]$Path, [string[]]$Selector) {
    try {
        $out = @(& $Path @($Selector + @('--version')) 2>&1) -join "`n"
        $v = Get-VersionObject $out
        if ($v -and $v.Major -eq 3 -and (($v.Minor -eq 11) -or ($v.Minor -eq 12))) {
            return [pscustomobject]@{ Path = $Path; Selector = $Selector; Version = $v }
        }
    } catch { }
    return $null
}
function Find-SetupPython {
    $choices = @(
        @{ Name = 'py.exe'; Selector = @('-3.12') },
        @{ Name = 'py.exe'; Selector = @('-3.11') },
        @{ Name = 'python.exe'; Selector = @() },
        @{ Name = 'python3.exe'; Selector = @() }
    )
    foreach ($choice in $choices) {
        $cmd = Get-Command $choice.Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) {
            $found = Test-PythonCandidate $cmd.Source $choice.Selector
            if ($found) { return $found }
        }
    }
    return $null
}
$python = Find-SetupPython
if (-not $python) { throw 'A supported Python 3.11 or 3.12 interpreter was not found after prerequisite setup.' }

# Build only fixed, validated argv tokens. Start-Process avoids shell parsing and
# handles PowerShell 5.1, where positional array splatting is inconsistent.
$pythonArgs = New-Object System.Collections.Generic.List[string]
foreach ($selector in $python.Selector) { [void]$pythonArgs.Add([string]$selector) }
[void]$pythonArgs.Add('-m'); [void]$pythonArgs.Add('ops.setup_manager'); [void]$pythonArgs.Add('install')
if ($NoStart) { [void]$pythonArgs.Add('--no-start') }
if ($Port -gt 0) { [void]$pythonArgs.Add('--port'); [void]$pythonArgs.Add([string]$Port) }
if ($NoBrowser) { [void]$pythonArgs.Add('--no-browser') }
$quotedArgs = foreach ($token in $pythonArgs) {
    if ($token -match '[\s"]') { '"' + $token.Replace('"', '\"') + '"' } else { $token }
}
$argumentString = [string]::Join(' ', $quotedArgs)

Push-Location -LiteralPath $repoRoot
try {
    Write-Host ("Running ops.setup_manager install with Python {0}..." -f $python.Version)
    $process = Start-Process -FilePath $python.Path -ArgumentList $argumentString -Wait -NoNewWindow -PassThru
    $code = $process.ExitCode
} finally {
    Pop-Location
}
if ($code -eq 3010) { Write-Warning 'A reboot is required. Reboot, then rerun install.cmd.' }
exit $code


