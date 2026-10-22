# FleetAI Mobilerun runtime

The native Windows/Linux control plane remains the authority for authentication, provider credentials and model calls, owner grants, leases/fences, target inventory, ADB, discovery, H264/WebCodecs, native controls, Sync/groups/numbering, proxy, files/APKs, reboot, Agent Console and local Qwen. The isolated worker receives only bounded parent-owned protocol frames.

## Supported backends

| Backend | Fixed transport | Activation |
|---|---|---|
| Linux host isolation | `/usr/local/libexec/d3m0n-ai-worker-client` to `/run/d3m0n-ai-worker.sock` | Requires the explicit root installer, static validation, and optional no-credential probe |
| Windows + dedicated WSL2 | `wsl.exe -d DISTRO -u OPERATOR --exec /usr/local/libexec/d3m0n-ai-worker-client` | Requires an owner-selected preinstalled WSL2 distro, systemd PID 1, and the same Linux installer |
| Windows without WSL2 | No secure worker backend | FleetAI is **not-ready/blocked**; native dashboard/device features remain available |
| Plain Python process | Only with `D3M0N_ALLOW_TEST_WORKER_FALLBACK=1` in offline tests | Never activation-ready and never an installer path |

The parent selects only a logical backend. `WorkerIsolationConfig.validate()` rejects every executable, socket, source, SDK, venv, prefix, and root config path other than the reviewed fixed values. The generated Linux JSON is root-owned and nonsecret; it contains the validated non-root operator and its existing primary group. The Windows parent JSON is a separate file outside the repository: its `config_path` is a Windows path and its `linux_config_path` is `/etc/d3m0n-ai-worker/runtime.json`; the Windows file is not presented as a POSIX config.

## Linux installation

Prepare a pinned SDK in a user task directory, without system writes:

```bash
scripts/prepare-fleet-sdk.sh --task-dir "$HOME/d3m0n-fleet-sdk"
```

That command uses the fixed upstream `https://github.com/droidrun/mobilerun.git`, fetches commit `301856138c6edeb2ae692bfab85b279ace520e59`, checks version `0.7.0`, builds a wheel, installs it non-editably into a `venv --copies`, and preserves `MOBILERUN-LICENSE`. It accepts no URL or ref argument.

Choose an **existing non-root account**. The installer validates that account and records its existing primary group. Then run the actual installer explicitly as root. It validates systemd/sudo/visudo, the account, the SDK commit/version and copy-based venv, every entry in the reviewed manifest, and the absence of symlinks before writing anything:

```bash
sudo scripts/install-fleet-runtime.sh \
  --operator fleetoperator \
  --source /path/to/frozen/repo \
  --sdk-source "$HOME/d3m0n-fleet-sdk/mobilerun-source" \
  --sdk-venv "$HOME/d3m0n-fleet-sdk/venv"
```

The installer copies **only manifest-listed application files** (never the checkout's state, `.git`, `node_modules`, database, uploads, or recordings), copies the separately prepared SDK/venv, installs root-owned fixed helpers at `/usr/local/libexec`, fixed units under `/etc/systemd/system`, and writes `/etc/d3m0n-ai-worker/runtime.json`. It does not install OS packages, start a model or worker, enable app activation, contact a provider, or use ADB.

Static verification is the default. Enabling the socket is an explicit separate gate; the optional probe is also explicit and requires that gate:

```bash
sudo scripts/install-fleet-runtime.sh --enable-socket \
  --operator fleetoperator --source /path/to/frozen/repo \
  --sdk-source "$HOME/d3m0n-fleet-sdk/mobilerun-source" \
  --sdk-venv "$HOME/d3m0n-fleet-sdk/venv"
sudo runtime/host-isolation/validate-host-isolation.sh
sudo scripts/install-fleet-runtime.sh --enable-socket --probe \
  --operator fleetoperator --source /path/to/frozen/repo \
  --sdk-source "$HOME/d3m0n-fleet-sdk/mobilerun-source" \
  --sdk-venv "$HOME/d3m0n-fleet-sdk/venv"
```

`--dry-run` performs checks and prints intent but writes no root-owned file, unit, socket state, password, or secret. It never starts the worker. Re-runs/upgrades refuse active `d3m0n-ai-worker@*.service` workers. Before replacement, the installer makes a bounded backup containing the runtime directory **and** every installed helper, unit, sudoers file, and config. Restore the newest complete backup with:

```bash
sudo scripts/install-fleet-runtime.sh --rollback
```

Rollback refuses an active worker and leaves socket activation disabled. It does not remove application databases, uploads, recordings, logs, auth state, device data, or other application files.

## Windows WSL2 installation

The owner/admin must first select and provision a **dedicated** WSL2 distro. This script does not enable Windows features, install/import/reset/unregister distros, reboot WSL, create passwords, or touch a general-purpose distro. The distro must already have WSL `>=0.67.6`, systemd as PID 1, sudo/visudo, Python, and the chosen non-root operator account/group.

From PowerShell, pass the local frozen source, prepared SDK source/venv, bundle, and operator explicitly:

```powershell
.\scripts\install-fleet-runtime.ps1 `
  -Distro D3m0nFleet `
  -Operator fleetoperator `
  -Source C:\path\to\frozen\repo `
  -SdkSource C:\path\to\d3m0n-fleet-sdk\mobilerun-source `
  -SdkVenv C:\path\to\d3m0n-fleet-sdk\venv
```

The PowerShell installer cleans UTF16/NUL-containing verbose WSL output, validates the version, exact distro, systemd PID 1, and non-root operator, translates local paths, copies the reviewed bundle/source/SDK/venv into a fixed Linux task directory, and then executes **only** the fixed Linux installer through explicit `sudo -n`. It never runs a native worker fallback. On success it writes a nonsecret Windows parent config outside the repository (by default `%LOCALAPPDATA%\D3m0n\fleet-runtime-wsl2.json`) with a fixed client vector and separate Windows/Linux config paths. `-EnableSocket` and `-Probe` are explicit; `-DryRun` performs no WSL copy, root command, config write, password setup, or secret operation.

If WSL2, systemd, the selected account, manifest, socket, or probe is unavailable, activation remains blocked/not-ready. The native control plane is not degraded into an unconfined Windows worker.

## Required confinement controls

The generated wrapper and units retain `DynamicUser=yes`, `ProtectHome=yes`, `ProtectSystem=strict`, read-only `/opt/d3m0n-ai-worker`, `PrivateTmp=yes`, `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`, `NoNewPrivileges=yes`, bounded memory/tasks/processes/CPU/wall time, and `StandardError=null`. Root dispatcher authentication uses `SO_PEERCRED` against the generated operator; the client and bootstrap load the same root-owned config and reject path drift. The sudoers rule permits only the fixed wrapper and literal `--probe`.

## Release and evidence caveats

The current checkout's reviewed `source-manifest.sha256` is intentionally fail-closed and is stale for four source files identified by `audits/runtime.md`; the parent release owner must regenerate it only after freezing and reviewing the intended snapshot. No installer was run against a Linux or WSL host here. Therefore the implementation provides actual source/config/helper/unit functionality and offline parser/unit-test coverage, but physical confinement properties (DynamicUser UID, `/home` denial, AF_UNIX-only behavior, read-only enforcement, systemd socket activation and WSL transport) still require the disposable-host gates described in the audit. No signed image, signature, provider, phone, or production-readiness claim is made.
