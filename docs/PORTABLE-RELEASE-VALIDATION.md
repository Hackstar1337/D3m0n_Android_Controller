# Portable source release validation — 23 October 2026

## Release scope

This is a GitHub-ready Windows/Linux source release of the existing D3m0n dashboard. It is not an automatic production deployment, a public-repository conversion, or proof that all smartphones on every network can be discovered without debugging/authorization. Existing production services, phones, proxies, numbering, groups, credentials and AI activation were not changed during this portability work. No Git commit or push was performed.

## Passing gates

| Gate | Evidence |
|---|---|
| Existing source preservation | All 718 files from the earlier sanitized source baseline remain; no required baseline source file is missing. Original root documentation is also preserved as UPSTREAM-README.md. |
| Customized Windows backend | 54/54 isolated suites passed; unittest reported 419 cases, 7 explicitly skipped, 412 executed successfully. |
| Customized Linux backend | 54/54 isolated suites passed in a clean Linux virtual environment using the existing WSL interpreter; 419 reported, 7 explicitly skipped, 412 executed successfully. |
| SDK-only cloud gateway adapter | 6/6 offline tests passed in the separate pinned Mobilerun 0.7.0 environment. SDK dependencies were not added to the main backend. |
| Install/origin/state/release guard | 12 focused tests passed on Windows and Linux; final Windows rerun passed after neutral fixture and launcher changes. |
| Frontend | All 32 regression suites passed; Vue/TypeScript checking and the production build passed. The Fleet AI scrolling fix remains included. |
| Windows privacy | Real native owner-only protected DACL creation/readback passed on throwaway directories/files; proxy/file tests assert ACL privacy rather than incorrect POSIX mode assumptions. |
| Strict Sync scheduling | The original 3ms touch-down, 6ms release spread and 300ms-duration test limits are unchanged. Absolute call_at deadlines and a clock-resolution-aware shared lead prevent coarse Windows clock drift. No phone commands were used in these tests. |
| Clean install/package | Wheel and source distribution built successfully; wheel installed with dashboard dependencies in clean Windows and Linux project-local virtual environments. The H.264 server asset is verified against the official scrcpy 5.0.1 SHA-256 and its Apache license is included. |
| Runtime static contracts | Fixed command vectors/paths, non-root identity, template confinement, pinned SDK identity, manifest-only staging, explicit socket/probe gates, backup boundaries and Bash/PowerShell syntax gates passed. |
| Source privacy | A source-only export passed the redacted release guard with zero findings after removal of deployment literals, generated data and credential-like fixture values. |

The full suites ran in separate processes to prevent historical test modules that replace requests/gitd in sys.modules from polluting unrelated tests. Provider/device/network operations were fenced by an external acceptance guard; only task-private Windows ACL operations and exact read-only shell/PowerShell syntax parsing were allowed. Upstream tests requiring phones/iOS/emulators/models were not substituted with claims of live success. One SDK-only module is intentionally skipped by the ordinary backend; the separate SDK adapter run covers it.

## Preserved capabilities

H.264/WebCodecs streams and intent/Stop handling; direct taps/swipes/keys/Unicode input; selected-device Sync and control isolation; persistent groups/phone numbering; encrypted per-device proxy workflows; private files/APKs; confirmed reboot and USB recovery; dashboard password/session and origin protections; existing local/cloud Agent Console; and Mobilerun Fleet AI jobs, leases, budgets, model policy, encrypted profiles, receipts, checkpoints and owner grants remain in the source.

Discovery is additive: passive USB/mDNS visibility, explicit private LAN probing, issued-candidate Connect/Pair actions, stdin-only pairing codes, dynamic connect-port rediscovery and inventory refresh. It does not enable debugging, pair devices, rotate/reboot phones, install Portal, change proxies, start streams/Sync or launch AI jobs during installation/scanning. Discovery tests are deterministic synthetic tests, not a claim of physical validation across every OEM/router/firewall.

## Deployment/readiness limits

**Fresh-machine Linux/Windows WSL worker installation and physical confinement remain an operator staging gate.** The packaging task did not install systemd services, import/reset WSL, enable OS features, execute root setup, run a real worker or submit cloud/device tasks on a new user's computer. The implementation includes real setup scripts, but their physical systemd/peer-identity/PrivateNetwork/NoNewPrivileges/read-only checks must pass on that computer before activation. Windows needs a dedicated systemd-enabled WSL2 distro and non-root operator; the main dashboard runs natively. Missing isolation is not-ready, with no unconfined fallback.

No API/proxy credential, dashboard hash, ADB identity, runtime DB, phone inventory, recording, upload or private screenshot is included. Cloud providers/models, local models, proxies, optional Portal, iOS signing, emulator/KVM and optional recording tools require owner setup. Account credentials, OTPs, CAPTCHAs, terms and identity decisions pause for the owner; unattended Gmail account creation is not verified.

## Reproduce and review

Read INSTALLATION.md and FLEET-AI-RUNTIME.md. Use isolated state and test guards before running broader suites; do not run integration tests against a production DB, phone or provider. Run frontend npm test/build from the checkout, and the release guard against a fresh source-only export, not a working tree containing node_modules/dist/test caches. Generated builds, logs, dependencies and tests' private state are intentionally excluded from the GitHub package.

The source archive includes an exact relative-path/hash manifest. Keep the repository private unless its owner explicitly chooses public visibility; source preparation does not grant publication or deployment permission.
