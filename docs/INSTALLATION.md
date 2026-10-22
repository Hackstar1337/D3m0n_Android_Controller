# Guided installation

## Windows: download and double-click

Download/extract this repository or clone it, then double-click **install.cmd**. Git is not required for an extracted ZIP. The batch file starts only the local reviewed PowerShell installer with process-scoped execution permission; it does not change machine-wide execution policy. Organization policies may still block scripts: ask your administrator rather than disabling security settings.

Setup checks Python 3.11/3.12, supported Node.js/npm and ADB. Existing supported software is reused. Missing tools produce an exact WinGet install plan and a default-no Yes/No prompt. Python/Platform-Tools request per-user scope; Node's MSI may require machine scope and visible UAC approval. License prompts remain visible. If a package requests a reboot, reboot yourself and rerun install.cmd. Setup never reboots the computer.

Windows needs Microsoft's App Installer/WinGet. If it is absent/disabled, setup points to App Installer and official downloads, not a hidden third-party bootstrap. Locked-down Windows images may need administrator help. Internet is needed for missing tools and project dependencies.

```powershell
.\install.cmd -DryRun
.\install.cmd -NoBrowser
.\install.cmd -NoStart
.\install.cmd -Port 5056
.\install.cmd -Yes
```

`-Yes` means you reviewed and approve the prerequisite plan. It does not bypass Windows security, licenses, the first dashboard password or phone pairing.

## Linux: one command

From the extracted/cloned folder:

```bash
bash install.sh
```

The guided package flow supports Ubuntu/Debian and Fedora. It checks Python 3.11/3.12 with venv/ensurepip, Node.js/npm, ADB and archive-verification tools. It prints scoped apt/dnf operations and any official Node download, then asks default-no Yes before changing software. Scoped package commands may ask for sudo, but the dashboard and its state stay owned by your normal user. Do not run `sudo bash install.sh`.

A missing/unsupported Node is installed from a pinned official Linux x64/arm64 archive into your user-local directory after SHA-256 verification and archive checks. Existing system Node, shell startup files, package repositories, firewalls and OS security settings are not rewritten. Unsupported distros/architectures or unavailable package versions produce an error rather than a guessed system change.

```bash
bash install.sh --dry-run
bash install.sh --no-browser
bash install.sh --no-start
bash install.sh --port 5056
bash install.sh --yes
```

Dry-run performs no installation/download/privilege change, creates no password and starts no server. Existing .env and private runtime state are preserved. Declining stops before those proposed changes; partially completed package operations are not silently rolled back/uninstalled. Fix the named failure and rerun.

## What setup completes

After prerequisites, the shared manager creates/reuses project `.venv`, installs backend dependencies, installs frontend packages from the lockfile and produces the production build. It prompts privately for a first dashboard password and confirmation; existing auth records are preserved. Passwords are not put in command arguments, repository files or cloud requests by setup.

An owned background launcher starts the protected dashboard on **127.0.0.1**, then verifies readiness before printing/opening:

```text
DASHBOARD READY: http://127.0.0.1:PORT/?discover
```

Port 5055 is preferred. An occupied port is not taken over; setup chooses another free local port. A failure prints a private diagnostic-log location, not a success URL. `--no-start` installs only. The URL works on this computer; LAN/public exposure needs separate deliberate bind/origin/HTTPS configuration.

The dashboard remains running after setup closes while the computer is on. Reboot/login auto-start, root services, firewall changes, HTTPS tunnels and production rollout are not installed silently.

## Start, status and stop

Windows: double-click **start.cmd**, or:

```powershell
.venv\Scripts\python.exe -m ops.setup_manager start
.venv\Scripts\python.exe -m ops.setup_manager status
.venv\Scripts\python.exe -m ops.setup_manager stop
```

Linux:

```bash
bash start.sh
.venv/bin/python -m ops.setup_manager status
.venv/bin/python -m ops.setup_manager stop
```

A healthy owned instance is reused. Stop authenticates to a private local supervisor; it never kills a PID merely because it was read from disk or stops another service. Stop this checkout before rerunning installation. Different checkouts must not be deliberately pointed at the same running database/state. Advanced foreground servers must also be stopped by their owner before updating that checkout.

Private state uses Windows LOCALAPPDATA or Linux XDG state outside the source tree. Existing .env and explicit state overrides remain supported. Do not include another user's encrypted settings/ADB keys in an installation archive or publish launcher tokens/logs.

## Connect phones

Discover devices reads USB/mDNS candidates. Select an interface and explicitly Scan selected LAN for legacy ADB endpoints. USB RSA approval, Wireless Debugging pairing codes and older Android USB-to-TCP setup remain owner decisions. Guest Wi-Fi, client isolation and firewalls can prevent reachability. Sharing Wi-Fi does not authorize or expose all smartphones. Debugging/accessibility/Portal is never silently enabled.

## Optional Fleet AI and models

Full Mobilerun Fleet AI needs [separate isolated runtime setup](FLEET-AI-RUNTIME.md): root-owned systemd confinement on Linux or a dedicated systemd-enabled WSL2 distro/non-root operator on Windows. Core setup does not silently enable Windows features, import/reset WSL or weaken isolation. If unprepared, Fleet AI reports not-ready while ordinary controls remain available.

API keys, model weights, proxy credentials, device authorizations, iOS signing, OTPs, CAPTCHAs and identity decisions cannot be supplied by a Yes button. Configure these optional capabilities in their documented setup/UI. All existing files/APKs/proxy/Sync/reboot/Agent Console/Fleet AI source is retained; core installation does not imply every optional workflow is already configured or physically verified on a new computer.

## Diagnostics and upgrades

Run installed `.venv` Python `-m ops.doctor` for read-only diagnostics. A password mismatch stops safely: rerun and enter matching values. A missing prerequisite or package failure names what to repair; never delete runtime state to fix an install error. Review/redact private logs before sharing them.

The older scripts/bootstrap.* and foreground scripts/start.* remain available for advanced manual use. Do not mix foreground and background servers against the same state. Back up state before upgrading. The project has no complete Alembic revision chain: arbitrary `alembic upgrade head` is not an install requirement.

Official setup references: [Microsoft WinGet](https://learn.microsoft.com/en-us/windows/package-manager/winget/install), [Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools), [ADB debugging](https://developer.android.com/tools/adb), [Node.js downloads](https://nodejs.org/en/download/), [Python for Windows](https://www.python.org/downloads/windows/).
