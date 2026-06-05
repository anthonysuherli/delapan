# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately**. Do not open a public issue for
a security report.

Email: **security@delapan.dev** *(update to your real contact before publishing)*

Include a description, reproduction steps, and impact. We aim to acknowledge within
72 hours and will coordinate a fix and disclosure timeline with you.

## Scope

The local tier binds to `127.0.0.1` and runs without authentication by design
(single user, loopback only). Do **not** expose the local server on a public
interface. Multi-tenant / network deployments must run the cloud backend with auth
enabled.
