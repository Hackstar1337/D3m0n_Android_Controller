# Docker development and Qoder

Use the **same cloned checkout** for editing, testing, committing, and pushing. Do not create a second source copy for Qoder. Docker uses live source mounts from this folder; Git uses its existing `.git` directory and `origin` remote.

## Start and edit

Docker Desktop must be running with Linux containers (supported image/ADB target: x86-64). The dependency-free host helper needs Python 3.11 or 3.12; Node 24 and the application's Python/Node dependencies live in containers. Native Windows remains the easier route for ordinary USB access.

```powershell
# From the repository root; process-scoped policy only.
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Mode Docker
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev.ps1 edit
```

Alternatively use `start-dev.cmd`, `edit-dev.cmd`, and `stop-dev.cmd`. On Linux use `bash install.sh --mode docker` and `bash dev.sh start`. First installation prompts for a new password in the interactive terminal with hidden input; a rerun preserves the existing password. There is no default password.

The helper inspects ownership before adopting an existing project, selects free localhost ports rather than killing another listener, records the actual mapping privately, and prints **READY** only when the page, API/frontend proxy, configured authentication, and denied unauthenticated protected requests all pass. Default URLs are <http://127.0.0.1:5173/> for hot reload and <http://127.0.0.1:5055/> for the built dashboard/API; use the actual printed URLs if a default port was occupied.

Edit Vue/TypeScript/CSS in `frontend/src/` for Vite hot reload. Python edits in `ops/`, `gitd/`, `integrations/`, and `ai_contracts/` restart the backend worker. Polling supports Windows bind mounts. Stop active device work first: backend reloads interrupt running streams/jobs.

Open `D3m0n-Droid.code-workspace` in Qoder. Its tasks cover start, stop, readiness, logs, shell, UI build, password setup, and regression tests. The optional Dev Containers configuration gives a container Python interpreter (`/usr/local/bin/python`); frontend tools run in the separate frontend service. Recommendations do not silently install editor extensions.

## Commands

```powershell
.\dev.ps1 start
.\dev.ps1 status
.\dev.ps1 logs
.\dev.ps1 shell
.\dev.ps1 frontend-shell
.\dev.ps1 build-ui
.\dev.ps1 password
.\dev.ps1 test
.\dev.ps1 rebuild
.\dev.ps1 stop
```

Use the `powershell ... -File` form above if direct scripts are restricted. Linux equivalents are `bash dev.sh ACTION`. Source edits do not need a rebuild. Dependency/config changes use the cached rebuild path. The frontend entrypoint checks the lockfile fingerprint **before** starting Vite and refreshes stale dependencies only after stopping the old process; failed installation never marks the volume current. Backend dependencies and SDK downloads use source-independent image layers, hash-pinned requirements, verified Platform-Tools, and pinned base-image digests.

The built dashboard does not hot-reload. `build-ui` refreshes its separate build volume; use the Vite URL for instant editing. The test command runs frontend type checks/portable tests plus installer, integrity, and authentication regressions using temporary test state.

### Production-like local mode

Stop development first, then start the production profile:

```powershell
.\dev.ps1 stop
.\dev.ps1 start -Production
.\dev.ps1 status
.\dev.ps1 stop -Production
```

Production serves its image-built frontend from the single backend origin, without source mounts or Python reload. Rebuild after changes (`rebuild -Production`). This is a localhost deployment, not public hosting. Private state and ADB identity volumes are shared with the default development project and survive recreation. Do not run both profiles simultaneously under one project name.

## Qoder → GitHub

This checkout's normal `main` branch should track the intended `origin/main`. Verify before publishing:

```bash
git remote -v
git branch -vv
git status --short
```

In Qoder's Source Control panel: review changes, stage the intended files, commit, and **Push**. The equivalent terminal workflow is:

```bash
git diff --check
git add <reviewed-files>
git commit -m "Describe the change"
git push origin main
```

Git uses your existing credential manager. No token/password is embedded in the workspace and no automatic commit/push is configured. A push triggers GitHub Actions. Check its result before publishing a release; candidate ZIPs are not final release artifacts. The final source ZIP is gated on Windows/Linux contracts, frontend checks, actual native install/resume, and fresh Docker checkout/ZIP installation acceptance. Repository branch protection is an owner setting, separate from the workflow.

## Private state and backups

Default volumes are `d3m0n-droid_private_state` (database, encrypted settings, password hash), `d3m0n-droid_adb_identity` (device trust), and separate frontend dependency/build volumes. They are outside Git and are **not** OneDrive backups. Never use `down -v` as routine repair.

Backup is explicitly secret-bearing and requires acknowledgment. Stop this project for a consistent database snapshot. Choose an archive path outside the source checkout and protect the archive yourself; backups are private but not additionally encrypted by this command.

```powershell
.\dev.ps1 stop
.\dev.ps1 backup -Archive <private-backup-path.tar.gz> -IncludeSecrets
.\dev.ps1 start
```

Restore never overwrites live volumes. It validates member paths, sizes, duplicates, and hashes, then creates a **new** volume namespace:

```powershell
.\dev.ps1 restore -Archive <private-backup-path.tar.gz> -TargetProject d3m0n-restored -IncludeSecrets
```

Inspect the restored state separately before choosing any migration. To use that isolated project in a new terminal, set `$env:D3M0N_COMPOSE_PROJECT='d3m0n-restored'`, then start. Existing target volumes are refused. Interrupted restore volumes are left isolated for inspection, never automatically deleted. No arbitrary database-schema upgrade/rollback is promised.

On Linux use `python3 -m ops.docker_backup backup|restore --archive PATH --include-secrets` with `--target-project NEW_NAME` for restore. Native state has separate `ops.setup_manager backup/restore` commands described in [installation reliability](docs/INSTALLATION-RELIABILITY.md).

For multiple independent checkouts, set a distinct `D3M0N_COMPOSE_PROJECT` before the first start. CI uses disposable `d3m0n-ci-*` projects and refuses to reuse existing acceptance volumes. Its cleanup never targets the default local project.

## Safety and device limitations

Published ports bind **only to 127.0.0.1**. Application containers run non-root with dropped capabilities, no-new-privileges, and no Docker socket access. Installation performs no discovery, pairing, taps, APK installation, streams, provider requests, or AI jobs. No public URL, firewall change, reboot auto-start, Windows USB driver, or WSL passthrough is configured.

Docker has its own ADB server. Explicitly pair only your or owner-authorized devices using their Wireless Debugging IP/ports. Docker Desktop may not receive LAN mDNS broadcasts; pairing and debugging ports are different, and manual IP/port entry may be necessary. Native Windows is preferable when physical USB access is required. A remote Windows ADB server does not automatically satisfy container-local H.264 socket forwarding.

Fleet AI/Mobilerun retains its separately documented isolated systemd/WSL requirements; a dashboard container is not that runtime. iOS, emulators, local models, Portal, and provider keys remain optional/unconfigured. Do not weaken isolation or authentication to bypass readiness.

Google Platform-Tools 37.0.1 archives are pinned and SHA-256/version checked by the shared installer. The Windows native fallback uses the verified official `win.zip` release alias; Linux uses the verified x86-64 archive. Unsupported architectures fail before download. SDK license notices remain included. See [Google's Platform-Tools notes](https://developer.android.com/tools/releases/platform-tools).
