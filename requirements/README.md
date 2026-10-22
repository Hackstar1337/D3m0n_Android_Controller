# Python dependency locks

These files are generated from the repository `pyproject.toml`, not from a
developer environment. They target the guided **Python 3.11 and 3.12**
installations on supported Windows/Linux x86_64 hosts. Every package entry is
version-pinned and carries hashes so pip can reject altered artifacts.

## Lock roles

| File | Contents | Installation interface |
| --- | --- | --- |
| `backend.lock` | Runtime dependencies plus the `d3m0n` dashboard extra | `python -m pip install --require-hashes -r requirements/backend.lock` |
| `dev.lock` | Backend lock set plus `pytest` and `ruff` | `python -m pip install --require-hashes -r requirements/dev.lock` |
| `build.lock` | Isolated editable-build dependencies from `[build-system]` | `python -m pip install --require-hashes -r requirements/build.lock` |

For an editable install, install the build lock first, then install the local
project without allowing pip to resolve a second dependency graph:

```text
python -m pip install --require-hashes -r requirements/build.lock
python -m pip install --no-build-isolation --no-deps -e .
```

Install `dev.lock` instead of `backend.lock` when running the installation and
lint tests. The `ops/install-compatibility.json` manifest is included in the
`ops` package data by `pyproject.toml`. Build and runtime locks have separate roles; shared dependencies must use the same
version. Build-lock regeneration is constrained by the staged runtime lock.

## Refreshing locks

Use an active project virtual environment containing `pip-tools` (do not
install a global toolchain):

```text
# Windows PowerShell
.\scripts\update_dependency_locks.ps1
.\scripts\update_dependency_locks.ps1 -Upgrade

# Linux
./scripts/update_dependency_locks.sh
./scripts/update_dependency_locks.sh --upgrade
```

The default refresh preserves pins where possible. `--upgrade` is a deliberate
maintenance event: review the diff, run the lock checks and installation tests,
and update `ops/release-manifest.json` only after the tested runtime/image
provenance is refreshed. `PIP_COMPILE` may point to a project-local
`pip-compile`; otherwise the updater invokes `python -m piptools compile`.

`pip-compile` resolves the host platform. The updater therefore reapplies the
reviewed universal overlay: `pywin32` is marked Windows-only and the tested
Linux x86_64 `uvloop` wheels for Python 3.11/3.12 are retained with hashes.
When changing that overlay, independently test both guided Python versions and
both operating systems before publishing.

`requirements/platform-overlays.json` preserves both targets even when generation
runs on only one operating system. Explicit `--upgrade` refreshes its artifact
hashes from PyPI; outputs are resolved and checked in staging before replacement.
Run `python scripts/update_dependency_locks.py --check` to validate checked-in
pins, hashes, and platform markers without downloading or changing anything.

## Tested provenance

See [`../ops/release-manifest.json`](../ops/release-manifest.json) for the
source revision, guided runtimes, Node range, tested base-image digests, and
Google Platform-Tools checksum. The lock files do not contain credentials,
private state, ADB identities, or machine-specific paths.
