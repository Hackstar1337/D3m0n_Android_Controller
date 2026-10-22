# D3m0n release runbook

## Scope

This source package is intended for a **private** repository and controlled human-reviewed releases. It contains source, tracked tests, schema initialization code, Alembic scaffolding, documentation, and dependency manifests only. It does not contain production databases, uploads, runs, logs, recordings, screenshots, phone inventories, proxy state, credentials, or generated build output.

## Pre-upload checks

- Review `AUDITED-CANDIDATE-MANIFEST.json` and confirm the candidate hashes are unchanged.
- Inspect `git status --ignored` after initializing a local repository; `.env` and generated/runtime paths must remain ignored.
- Run the textual privacy scan from the evidence report or an equivalent secret scanner. Review every finding; synthetic fixtures must be clearly marked.
- Keep the repository private and require review before changing visibility or adding CI/CD.

## Build and test

- Python: use an isolated Python 3.10+ environment and `pip install -e ".[test,d3m0n]"`.
- Frontend: `npm --prefix frontend ci` then `npm --prefix frontend run build`.
- Run only tests supported by the available environment. Device, iOS, proxy, live-stream, and integration suites need their respective local services/devices; packaging does not claim clean-install test success.
- Database initialization is the `gitd.app.lifespan` startup path: SQLAlchemy `create_all` plus `ensure_additive_columns` for the enumerated legacy additions. No Alembic revision chain exists in this source snapshot; do not imply a general `upgrade head` path is available. For existing databases, make a private backup and explicitly validate the intended schema change. Never point a test at production state.

## Staged SSH rollout

An authorized operator should use an approved SSH identity and host alias configured outside this repository. The package does not prescribe a live host. Verify backup, idle-agent/device state, service health, and rollback before transfer. Build and smoke-test in a staging directory, compare hashes, then activate the smallest reviewed change. Afterward check API/auth boundaries, native WebSocket handshake, frontend assets, stream recovery, and unrelated device/proxy/group/number/settings state. Stop and roll back on drift.

No command in this document performs deployment; owner approval is required for every live change. All upstream workflow YAML is archived with a `.disabled` suffix in `docs/upstream-workflows/`, not executable Actions configuration. No automatic publishing/deployment is enabled.

## Portability note

A legacy geometry test in the upstream tree may refer to the historical `<historical-source-path>` checkout. In this package it is treated as an export-only portability concern and must be run from the checkout root or adapted locally by an operator; no runtime/live source is modified.
