# D3m0n Android Controller

A password-protected Android fleet dashboard for Windows and Linux: H.264/WebCodecs live screens, direct phone controls, selected-device Sync, persistent groups and numbering, per-device proxy management, files/APKs, confirmed reboot/recovery, local/cloud Agent Console, and a permission-aware Mobilerun Fleet AI Command Centre.

## Download → install → open your dashboard

Download **Code → Download ZIP** and extract it, or clone this repository. Git is not required for ZIP installation. Use a directory you own.

### Windows 10/11

Double-click **`install.cmd`**. The installer checks Python, Node.js/npm and Android Platform-Tools. If something is missing, it shows the required changes and asks **Yes/No** before installing through Windows Package Manager. Follow any Windows UAC and software-license prompts. Create your own dashboard password when asked.

### Linux

Open a terminal in the extracted/cloned folder and run:

```bash
bash install.sh
```

Ubuntu/Debian and Fedora are supported by the guided prerequisite flow. Review the plan and answer **Yes**; the package manager may ask for your sudo password. Do not run the whole installer as root. A supported official Node.js archive is installed under your user account only if needed; its SHA-256 is verified before use.

### Your URL

Both installers create a project-local Python environment, install backend dependencies, build the dashboard and start it. **Only after the local server passes its readiness check**, setup prints and opens a URL such as:

```text
DASHBOARD READY: http://127.0.0.1:5055/?discover
```

If that port is already occupied, setup chooses a free local port rather than stopping another service. Keep the printed URL. The background dashboard continues after setup closes while your computer remains on; login/reboot auto-start is not installed silently.

Next time, double-click **`start.cmd`** on Windows or run **`bash start.sh`** on Linux. [Full installation, lifecycle commands and troubleshooting](docs/INSTALLATION.md).

Missing tools are never installed without approval. `-Yes` / `--yes` is available for an owner who has already reviewed the plan; UAC, package-license prompts and your first password may still require input. Use `install.cmd -DryRun` or `bash install.sh --dry-run` to inspect without changes. Windows machines without WinGet/App Installer and unsupported Linux distributions receive explicit setup instructions instead of a hidden remote-script bootstrap.

## Find your phones

The dashboard opens **Operations → Discover devices**. It lists USB and advertised Android wireless-debugging candidates. Choose your local interface and **Scan selected LAN** to find older ADB TCP endpoints, then Connect or Pair & connect. The bounded scan is restricted to attached private networks. Authorized phones appear in Live Grid.

**Phones need Android debugging enabled and owner authorization.** Sharing Wi-Fi alone does not expose every smartphone. USB needs the RSA approval on the phone. Android 11+ Wireless Debugging needs its pairing code; older Android normally needs initial authorized USB setup. The controller does not bypass these trust prompts, discover iPhones with ADB, create accounts unattended or guarantee every router/OEM configuration. [Official ADB guide](https://developer.android.com/tools/adb).

Installation/discovery never sends taps, rotates/reboots phones, changes proxies, installs Portal, starts Sync/streams, or launches AI jobs. Pairing and high-impact actions remain explicit owner decisions.

## AI and optional features

D3m0n owns native devices, credentials, jobs, policies, budgets, leases, receipts and approval gates. **Mobilerun is the sole Fleet AI task runtime**. The secure runtime remains separate from the dashboard: Linux needs its root-owned systemd worker; Windows uses a dedicated WSL2/systemd Linux worker. See [Fleet AI runtime setup](docs/FLEET-AI-RUNTIME.md). Core install does not silently enable Windows features, import/reset WSL, install root services or weaken isolation. Missing isolation is shown as not-ready, not bypassed.

Supply your own model API profiles or configure local Ollama/Qwen for Agent Console. No API key, proxy password, model weights, phone inventory or runtime database is shipped. OTPs, passwords, CAPTCHAs, terms and account identity decisions pause for you. File/APK/reboot AI operations require a one-time owner confirmation. Portal/accessibility, iOS, emulator and other preserved upstream integrations retain their own OS/device prerequisites; installation does not claim these optional stacks are already configured.

## Security, preservation and validation

The default URL is local to the computer. LAN/public access requires deliberate bind, exact allowed origins and secure hosting configuration; the installer does not alter a firewall, publish a public URL or deploy to an existing host. Dashboard passwords and encrypted settings stay in protected per-user state outside the source tree. Existing `.env`, passwords, phone numbers/groups and stored profiles are preserved. Back up state before upgrading; stop this checkout before rerunning its dependency installation.

All prior source modules and features remain. [Validation evidence](docs/GUIDED-INSTALL-VALIDATION.md) distinguishes tested Windows/Linux lifecycle behavior, mocked prerequisites and optional real-device/runtime setup. [Earlier backend/frontend coverage](docs/PORTABLE-RELEASE-VALIDATION.md) is also retained. No automatic production deployment is attached to this repository.

Never commit `.env`, private keys, ADB identities, credentials, profiles, databases, uploads, recordings or logs. See [SECURITY.md](SECURITY.md), [.env.example](.env.example), and the source-only release guard in `scripts/release_guard.py`.

## License

MIT; see [LICENSE](LICENSE). Original modules/documentation are retained in [UPSTREAM-README.md](UPSTREAM-README.md). Mobilerun is installed separately at its reviewed pin. Bundled scrcpy server licensing is included in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
