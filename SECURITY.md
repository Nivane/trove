# Security Policy

## Supported versions

Security fixes are applied to the latest 0.1.x release line. Older snapshots and
feature branches are not supported.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting:
**Security → Advisories → Report a vulnerability** on
<https://github.com/Nivane/trove>.

Do not open a public issue, and do not include credentials, tokens, customer
data, or a working exploit in public channels. We will acknowledge the report,
investigate it, and coordinate a fix and disclosure with you.

## Scope

In scope:

- Authentication and authorization (tokens, OIDC, roles, row/column grants)
- Semantic-model and KB write paths (confirmation gates, merge path)
- SQL execution boundaries (read-only enforcement, masking, provider allowlist)
- Server API endpoints, MCP tools, and the admin console
- Dependency vulnerabilities reachable from the shipped code

Out of scope:

- Attacks requiring a compromised host, database, or identity provider
- Denial of service from deliberately extreme local configuration
- Issues in third-party services or the datasources you connect
- Reports that require a secret you have already exposed publicly

## Handling secrets

Trove runs with your LLM and datasource credentials. Keep them in environment
variables or `~/.trove/conf`; never commit them. If a credential is exposed,
rotate it first, then report the exposure path.
