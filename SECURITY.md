# Security Policy

## Reporting a vulnerability

Please **do not** open public GitHub issues for security vulnerabilities.

Report privately via GitHub Security Advisories:

- https://github.com/votee/beever-atlas/security/advisories/new

When reporting, include:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept where possible)
- Affected version(s) and configuration
- Your suggested remediation, if any

We commit to an **initial response within 72 hours** of receiving a report.
After triage we'll share a remediation plan and a coordinated disclosure
timeline. Please give us a reasonable window to ship a fix before any public
disclosure.

## Supported versions

Beever Atlas is pre-1.0. Only the latest `0.1.x` line receives security fixes.

| Version  | Supported |
|----------|-----------|
| 0.1.x    | Yes       |
| < 0.1.0  | No        |

Older minor series are not backported; upgrade to the latest `0.1.x` to
receive fixes.

## API stability

All `/api/*` endpoints in 0.1.0 are **UNSTABLE** and may change without
notice. v0.2.0 will introduce a `/api/v1/*` prefix; clients pinning the
current paths will break when the versioned prefix lands. Treat the 0.1.x
API surface as a preview — pin the exact version you depend on and review
changelog notes before upgrading.

## Scope

In scope:

- The Python backend, bot service, and web app in this repository
- Official Docker Compose configurations shipped here
- Default configuration values distributed with the project

Out of scope:

- Self-hosted deployments with modified configurations
- Third-party forks
- Vulnerabilities in upstream dependencies without a Beever Atlas-specific
  impact (please report to the upstream project)
- Issues requiring physical access to a user's machine or stolen credentials

## Credential handling

Platform credentials (Slack tokens, Discord tokens, etc.) are encrypted at
rest using AES-256-GCM with a project-provided master key
(`CREDENTIAL_MASTER_KEY`). Rotate this key periodically and store it in a
secret manager in production.
