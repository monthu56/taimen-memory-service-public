# Security Policy

## Supported versions

Taimen is pre-1.0. Security fixes are made on the `main` branch of each
component and included in the next platform release; there are no long-term
support branches yet.

| Platform version | Supported |
|---|---|
| latest `main` / latest `v0.x` tag | yes |
| older tags | no |

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report privately through GitHub's private vulnerability reporting: open the
**Security** tab of the affected repository and choose **Report a vulnerability**.
If that option is not available to you, open an issue that only says
"security: please contact me" without any details, and a maintainer will reach
out through a private channel. Include in the private report:

- the component and version (tag or commit),
- steps to reproduce or a proof of concept,
- the impact you expect (what an attacker gains),
- whether the issue is already public.

You will get an acknowledgement within 5 business days and a status update at
least every 14 days until the issue is resolved. We ask for coordinated
disclosure: please give us up to 90 days to ship a fix before publishing details.
Credit is given in the release notes unless you prefer to stay anonymous.

## Scope

In scope: the code in this repository — the Taimen Memory Service (engine,
HTTP service, MCP server, CLI, the `client/` SDK and the `deploy/` files).

Out of scope: third-party and upstream dependencies such as PostgreSQL, Apache
AGE, pgvector and `platform-auth-sdk` (report upstream; tell us if a fix
requires a coordinated update), demo data, and deployments run by third parties.

## Hardening notes for operators

- Keep the service bound to `127.0.0.1` and expose it only through a reverse
  proxy that terminates TLS. Never publish `/console`: it has no authentication
  of its own, so leave `CB_CONSOLE_ENABLED=false` (the default) unless the proxy
  authenticates administrators in front of it — see `deploy/RUNBOOK.md`.
- Prefer IAM verification for callers (`CB_IAM_ENABLED=true`, ADR-018) with
  `CB_IAM_JWKS_URL` pointing at an internal JWKS endpoint. Where static keys are
  used, scope them per knowledge base with `CB_API_KEYS` grants (ADR-017) instead
  of handing out the full-access `CB_SERVER_API_KEY`; keep `.env` and any file
  holding keys at mode `0600` and outside git.
- Leave the public demo surface off (`CB_DEMO_PUBLIC_ENABLED=false`, the
  default) unless a rate-limited showcase is intended, and keep
  `CB_PII_PROTECTION=true` on any deployment that may hold personal data.
