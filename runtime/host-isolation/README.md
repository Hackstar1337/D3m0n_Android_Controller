# D3m0n SDK worker: Linux host-isolation staging

These are **prepared artifacts only**. They have not been installed, started, or physically tested. No SSH, provider, phone, database, dashboard-secret, or network action was performed.

## Files

- `d3m0n-ai-worker-wrapper.sh` — install as `/usr/local/libexec/d3m0n-ai-worker` (`root:root`, `0755`). It accepts no arguments or the literal `--probe`, then runs `systemd-run --pipe --wait --collect --quiet`.
- `bootstrap.py` — install as `/opt/d3m0n-ai-worker/bootstrap.py`; starts the venv interpreter with `-E -s -X utf8`, removes the script/CWD import slot, verifies the source root is a real root-owned directory, inserts only `/opt/d3m0n-ai-worker/source`, and calls `ops.ai_fleet_sidecar.main`.
- `source-manifest.sha256` — reviewed source-subset hashes. The installer fails closed on drift.
- `d3m0n-ai-worker.sudoers` — installer-rendered rule for the validated non-root operator: only the wrapper and wrapper `--probe`; no wildcard, interpreter, shell, arbitrary argument, or `SETENV`.
- `install-host-isolation.sh` — root-only, fail-closed staging script; does not start the worker.
- `validate-host-isolation.sh` — static checks by default; `--runtime` invokes only the fixed `sudo -n ... --probe` rule.
- `rollback-host-isolation.sh` — removes only this wrapper, sudoers drop-in, and `/opt/d3m0n-ai-worker`.
- `fake-syntax-test.sh` — local shell/Python parsing only; safe to run on this Windows checkout through a Linux shell if available.

## Installation contract

The integrated `scripts/install-fleet-runtime.sh` must receive an existing non-root `--operator` account (whose primary group has the same name) plus three already-reviewed local paths:

```text
sudo scripts/install-fleet-runtime.sh --operator fleetoperator --source <frozen checkout> --sdk-source <pinned SDK source> --sdk-venv <copy-based SDK venv>
```

The SDK source is copied to `/opt/d3m0n-ai-worker/sdk`; the copy-based venv is copied to `/opt/d3m0n-ai-worker/venv`. The venv must contain `bin/python`. Input symlinks are rejected. Application source is a strict whitelist from the manifest; no broker, settings launcher, dashboard, database, or native client is staged. All `/opt` trees are root-owned and write-protected. The SDK must be installed at the pinned Mobilerun identity `301856138c6edeb2ae692bfab85b279ace520e59` / `0.7.0` and its dependency/license review remains a parent gate.

The host must have functional `systemd-run`, systemd, `sudo`, and `visudo`; otherwise installation stops before writing anything. The integrated installer refuses active workers, backs up the runtime plus every helper/unit/config before replacement, and restores those artifacts with `--rollback`. It leaves the socket disabled unless `--enable-socket` is explicit.

## Source subset and import closure

The required D3m0n files are:

| Path | Why it is present |
| --- | --- |
| `ops/ai_fleet_sidecar.py` | fixed JSON stdin/stdout entry point; rejects args except `--probe`; sends app diagnostics to stderr |
| `ops/ai_fleet_jobs.py` | `JobStore` contract imported by the runtime |
| `ops/ai_fleet_settings.py` | transitively imported by the closed gateway adapter for typed envelopes; it is not given owner profile state |
| `ai_contracts/__init__.py`, `ai_contracts/models.py` | Pydantic job/lease/capability models |
| `gitd/services/sdk_worker_client.py` | remote pipe facades; no raw native/provider authority |
| `gitd/services/sdk_worker_protocol.py` | bounded newline-JSON request/response models |
| `gitd/services/mobilerun_runtime.py` | reviewed SDK runtime, allowlists, redaction, leases, and disabled features |
| `gitd/services/native_observation.py` | lazy mapping of parent-owned public UI state |
| `gitd/services/sdk_gateway_llm.py` | typed LLM facade that routes to the parent gateway |
| `gitd/services/cloud_gateway.py` | typed `GatewayResult`/`UIEnvelope` and adapter import closure; it must not be configured with profile state in the worker |
| package `__init__.py` files | import package markers only |

The worker never stages or imports the parent broker, launcher/service config, dashboard, DB state, native bridge, or SDK client settings. The `mobilerun` application is installed in the isolated venv; it is not added through `PYTHONPATH` or an ambient checkout. `-E -s` blocks environment/user-site Python injection.

## Isolation properties

The wrapper requests all of the following on every invocation:

- `DynamicUser=yes`: transient non-root UID; it must not be `0`.
- `ProtectHome=yes`: `/home`, `/root`, and `/run/user` are inaccessible to the worker.
- `ProtectSystem=strict` and `ReadOnlyPaths=/opt/d3m0n-ai-worker`: source, SDK app, venv, bootstrap, and wrapper are not writable.
- `PrivateTmp=yes`: private temporary directory.
- `PrivateNetwork=yes` and `RestrictAddressFamilies=AF_UNIX`: no provider, LAN, phone, or internet socket; only local pipe semantics are possible.
- `NoNewPrivileges=yes`: no privilege escalation.
- `MemoryMax=512M`, `TasksMax=32`, `LimitNPROC=32`, `CPUQuota=50%`, `RuntimeMaxSec=180s`, `LimitNOFILE=256`: bounded resource and wall time.
- `StandardError=null`: SDK/application diagnostics cannot contaminate the JSON stdout pipe. The sidecar retains the parent-owned stdout as its JSON output and redirects its own debug output to stderr.

The broker remains the only authority for model/native actions. The worker receives only the bootstrap envelope and can request `observe`, bounded `action`, `authorize`, `llm`, and `finish` frames over its private pipe. Provider credentials, DB state, dashboard secrets, raw native handles, and arbitrary shell/code/URL/MCP tools are not passed to it.

## Parent validation assertions

Run the static validator first. Only after local gates pass, the optional probe is the fixed non-root client command (the generated operator is read from root-owned config):

```bash
sudo -u fleetoperator -- /usr/local/libexec/d3m0n-ai-worker-client --probe
```

Expected probe output is one JSON line with the pinned SDK identity and no credential-like fields. This is an SDK/readiness check only; it is **not** a physical-device or provider test.

For an auditable host-property check, run a temporary root-owned validation unit (not through the `<operator>` sudoers rule) with the same properties and a harmless read-only command. Expected outcomes: `id -u` is nonzero; checking `/home/<operator>` fails; creating a file under `/opt/d3m0n-ai-worker/source` fails; opening an AF_INET socket fails; an AF_UNIX socket remains possible. During a short `--probe`, `systemctl show <transient-unit> -p DynamicUser -p User -p ProtectHome -p ProtectSystem -p PrivateNetwork -p RestrictAddressFamilies -p NoNewPrivileges -p MemoryMax -p TasksMax -p CPUQuota -p RuntimeMaxUSec` can corroborate the loaded properties. `--collect` may remove the transient unit after exit, so capture `systemctl show` while it is active or use the unit's journal/audit record.

The installed source must remain `root:root`, non-writable to the DynamicUser, and match `source-manifest.sha256`. `visudo -cf /etc/sudoers.d/d3m0n-ai-worker` must pass. Do not grant the worker a second sudo rule or add `PYTHONPATH`, `--setenv`, shell, interpreter, or arbitrary-argument entries.
