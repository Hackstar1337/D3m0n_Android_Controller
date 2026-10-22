[CmdletBinding()]
param(
    [ValidateSet('start','stop','status','logs','rebuild','shell','frontend-shell','build-ui','edit','password','test','backup','restore')]
    [string]$Action = 'start',
    [switch]$Production,
    [string]$Archive,
    [string]$TargetProject,
    [switch]$IncludeSecrets,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$ComposeFile = if ($Production) { 'compose.prod.yaml' } else { 'compose.dev.yaml' }
$ProfileArgs = if ($Production) { @('--production') } else { @() }

function Get-BootstrapPython {
    $choices = @(
        @{Name='py.exe'; Prefix=@('-3.12')}, @{Name='py.exe'; Prefix=@('-3.11')},
        @{Name='python.exe'; Prefix=@()}, @{Name='python3.exe'; Prefix=@()}
    )
    foreach ($choice in $choices) {
        $found = Get-Command $choice.Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            $probe = @($choice.Prefix) + @('-c','import sys; assert sys.version_info[:2] in ((3,11),(3,12)); print(sys.executable)')
            try {
                $candidate = & $found.Source @probe 2>$null
                if ($LASTEXITCODE -eq 0 -and $candidate) { return [string]($candidate | Select-Object -Last 1) }
            } catch { continue }
        }
    }
    throw 'Docker helper needs existing Python 3.11 or 3.12; use the native prerequisite plan if missing. No global toolchain was installed.'
}
function Invoke-DockerBootstrap {
    param([switch]$Rebuild)
    $python = Get-BootstrapPython
    $args = @('-m', 'ops.docker_setup', 'start') + $ProfileArgs
    if ($Rebuild) { $args += '--rebuild' }
    if ($NoBrowser) { $args += '--no-browser' }
    & $python @args
    if ($LASTEXITCODE -ne 0) { throw "Docker bootstrap failed with exit code $LASTEXITCODE" }
}
function Invoke-Compose {
    param([string[]]$DockerArgs)
    $Project = if ($env:D3M0N_COMPOSE_PROJECT) { $env:D3M0N_COMPOSE_PROJECT } else { 'd3m0n-droid' }
    & docker compose -p $Project -f $ComposeFile @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed with exit code $LASTEXITCODE" }
}

switch ($Action) {
    'start' { Invoke-DockerBootstrap }
    'stop' {
        $python = Get-BootstrapPython
        & $python -m ops.docker_setup stop @ProfileArgs
        if ($LASTEXITCODE -ne 0) { throw "Docker stop failed with exit code $LASTEXITCODE" }
    }
    'status' {
        $python = Get-BootstrapPython
        & $python -m ops.docker_setup status
        if ($LASTEXITCODE -gt 1) { throw "Docker status failed with exit code $LASTEXITCODE" }
    }
    'logs' { Invoke-Compose @('logs','--follow','--tail','80') }
    'rebuild' { Invoke-DockerBootstrap -Rebuild }
    'shell' { Invoke-Compose @('exec','backend','bash') }
    'frontend-shell' {
        if ($Production) { throw 'Production target has no live frontend shell; use the development target for Vite.' }
        Invoke-Compose @('exec','frontend','bash')
    }
    'build-ui' {
        if ($Production) { throw 'Production target builds the UI in its image; use dev.ps1 rebuild -Production.' }
        Invoke-Compose @('exec','frontend','npm','run','build')
    }
    'edit' {
        $Editor = Get-Command code -ErrorAction SilentlyContinue
        if ($Editor) { & $Editor.Source (Join-Path $PSScriptRoot 'D3m0n-Droid.code-workspace') }
        else { Invoke-Item (Join-Path $PSScriptRoot 'D3m0n-Droid.code-workspace') }
    }
    'password' { Invoke-Compose @('exec','backend','python','-m','ops.dashboard_auth','set-password') }
    'test' {
        if ($Production) { throw 'Run tests against the development target; production has no frontend test service.' }
        Invoke-Compose @('exec','-T','frontend','npm','run','type-check')
        Invoke-Compose @('exec','-T','frontend','npm','test')
        Invoke-Compose @('exec','-T','backend','python','-m','pytest','-q','tests/test_compatibility.py','tests/test_docker_setup.py','tests/test_docker_lifecycle.py','tests/test_setup_manager.py','tests/test_install_state.py','tests/test_state_backup.py','tests/test_runtime_paths.py tests/test_guided_install_contract.py','tests/test_portable_install.py','tests/test_platform_config.py','tests/test_ci_contract.py','tests/test_release_package.py','ops/tests/test_dashboard_auth.py')
    }
    { $_ -in 'backup','restore' } {
        if (-not $Archive -or -not $IncludeSecrets) { throw 'Supply -Archive and -IncludeSecrets; private backups contain credentials and ADB identities.' }
        $python = Get-BootstrapPython
        $BackupArgs = @('-m','ops.docker_backup',$Action,'--archive',$Archive,'--include-secrets')
        if ($Action -eq 'restore') {
            if (-not $TargetProject) { throw 'Restore requires a new -TargetProject; live state is never overwritten.' }
            $BackupArgs += @('--target-project',$TargetProject)
        }
        & $python @BackupArgs
        if ($LASTEXITCODE -ne 0) { throw 'Private Docker backup/restore failed; no live volume was overwritten.' }
    }
}
