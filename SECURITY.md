# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT open a public issue**
2. Email **security@ghostinthedroid.com** or open a private security advisory on GitHub
3. Include a description of the vulnerability and steps to reproduce

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Scope

This project controls Android devices via ADB and runs subprocesses. Key security considerations:

- **Authentication and binding** — the authenticated `ops.server` app binds to `127.0.0.1:5055` by default. LAN binding is an explicit operator setting and remains password/origin protected; never expose it to the public internet.
- **ADB access** grants full device control. Only connect trusted devices.
- **Subprocess execution** — the scheduler and bot system launch Python scripts. Config is stored in the database, not user-supplied at runtime.
- **API keys** are loaded from `.env` and never logged or returned in API responses.

## Best Practices

- If LAN access is explicitly enabled, use an exact configured origin over HTTPS where possible and keep the dashboard password/session gate enabled. Never trust a subnet as authentication.
- Use `.env` for all secrets — never commit credentials
- Keep ADB debugging disabled on devices when not in use
