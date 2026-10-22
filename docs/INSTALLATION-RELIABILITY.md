# Installation reliability and release checks

This document describes what the repository verifies automatically and what still
requires an owner decision. The checks are intentionally source-only and offline
with respect to phones, providers, and LAN discovery.

## CI gates

Every pull request and `main` push runs `.github/workflows/ci.yml`:

- **Native contract matrix:** Ubuntu and Windows, Python 3.11 and 3.12. Each
  entry installs `requirements/build.lock` and `requirements/dev.lock` with
  hashes, then performs a non-isolated editable install and runs the complete
  compatibility/setup-manager/Docker-setup/install-state/backup/release/
  portable/platform/auth contract set. The native shell/PowerShell dry-run is
  also exercised without changing OS prerequisites.
- **Frontend:** Node 24, `npm ci`, type checking, portable frontend tests, and a
  production build.
- **Native install integration:** supported-host Ubuntu and Windows jobs create
  a temporary state root, inject only a throwaway password hash through stdin,
  run `ops.setup_manager install --no-browser`, stop in cleanup, and run the
  install again to prove saved stages are resumable. No OS package installer is
  invoked by this stage.
- **Candidate source ZIP:** the package helper copies an explicit allowlist,
  including `compose.prod.yaml` and all runtime/build locks, then runs the
  redacted `scripts/release_guard.py`.
- **Clean Docker acceptance:** Linux creates two explicitly named disposable
  Compose projects: one from the checkout and one from the extracted candidate
  ZIP. Each builds and starts fresh services, installs a throwaway hash through
  stdin, checks frontend/API liveness, authenticated proxy access and denial of
  an unauthenticated protected request, and runs backend/frontend tests. Only
  those explicit projects and their temporary volumes are cleaned.
- **Final source ZIP:** the release artifact is built only after the Docker
  acceptance job succeeds, from a proof containing `docker_acceptance: true`.

A release ZIP cannot be created with `--release` unless a successful CI proof JSON
contains `status=success`, a workflow name, the tested commit, and
`docker_acceptance: true`. CI adds the Docker acceptance marker before creating
the final proof. This is a release gate,
not a replacement for repository branch protection or human review. The workflow
does not publish, push, contact external model services, or scan a LAN.

## Local checks

From a checkout, without starting services:

```bash
python scripts/package_release.py --check-only
python -m pytest -q tests/test_guided_install_contract.py tests/test_portable_install.py tests/test_release_package.py tests/test_ci_contract.py
```

To create a development ZIP after those checks, omit `--release` and provide an
output path. A release artifact must be made by CI (or by a maintainer holding an
independently saved successful CI proof):

```bash
python scripts/package_release.py --release --ci-proof ci-proof.json --output d3m0n-source.zip
```

The ZIP contains Docker/editor configuration, native entry points, the backend,
development, and build locks, tests, and source documentation. It never includes `.env`,
private authentication state, databases, ADB identity directories, build outputs,
node modules, or machine-specific runtime directories.

## Hermetic authentication fixtures

`ops/tests/test_dashboard_auth.py` creates a temporary state directory and a
throwaway password hash for every test case. The test middleware and auth state are
restored in teardown. Tests use reserved documentation IPs and a test-only HTTPS
origin; they do not read or rewrite the installed state directory. Set
`D3M0N_STATE_DIR`, `D3M0N_DASHBOARD_AUTH_FILE`, and allowed-origin variables only in
a disposable test process when writing additional integration tests. Never point a
test at the live Docker `private_state` or `adb_identity` volumes.

## Native versus Docker evidence

The matrix proves source contracts and hermetic backend behavior on supported
Python versions; it does not claim that a particular workstation has USB drivers,
ADB pairing, package-manager access, iOS signing, model credentials, or Fleet AI
isolation. Docker CI proves image/Compose construction on Linux. Docker on Windows
is a development and wireless-debugging option: USB passthrough and Windows host
ADB remain separate, deliberate setup choices. The Docker health check is process
liveness; first-password configuration and protected API behavior still require an
owner-authenticated setup check.

## Failure recovery

A failed check names its stage. Fix the named prerequisite and rerun. Do not delete
private volumes as a generic repair, run the installer as root, weaken the password
gate, expose a bind address publicly, or stop an unrelated listener. Back up state
before upgrades; unsupported database migrations are rejected rather than claimed
reversible.
