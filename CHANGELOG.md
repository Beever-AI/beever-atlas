# Changelog

All notable changes to Beever Atlas are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor bumps may introduce breaking changes).

## [Unreleased]

### Added
- MCP server at `/mcp` with auth middleware, 16 curated tools, 5 resources,
  3 prompts, and principal-keyed rate limits (gated by `BEEVER_MCP_ENABLED=true`).
  See `docs/mcp-server.md`.
- Comprehensive MCP documentation: server architecture, tool catalog, error codes,
  rate limits, long-running job pattern, and client setup guides for Claude Code
  and Cursor.
- MCP client configuration examples for Claude Code (`.mcp.json`) and Cursor
  (mcp-remote proxy).

### Changed
- Internal capability layer extracted to `src/beever_atlas/capabilities/` —
  ADK tools and MCP tools now share a single implementation.
- README Quick Start section now includes MCP server overview with link to full docs.

### Fixed (Security)
- The legacy unauthenticated `/mcp` mount is removed (previously gated off by
  a Phase 0 hotfix). The `/mcp` mount is the sole MCP surface; it requires
  bearer authentication via `BEEVER_MCP_API_KEYS` and enforces per-tool
  channel-access checks.

### Deprecated
- The legacy `search_channel_knowledge` MCP tool is removed. Callers now
  receive a structured `tool_renamed` error pointing at `ask_channel` and
  `search_channel_facts`.

### Removed
- Legacy unauthenticated `/mcp` mount (`BEEVER_MCP_ENABLED` flag). The secure
  `/mcp` surface (`BEEVER_MCP_ENABLED`) is the single MCP endpoint. Callers
  of the old `search_channel_knowledge` tool still receive a structured
  `tool_renamed` error directing them to `ask_channel` / `search_channel_facts`.

## [0.1.1] - 2026-04-14

First public open-source release. v0.1.0 was tagged before this work
landed; 0.1.1 is the actual launch and includes the full security
hardening, correctness fixes, OSS artifacts, CI workflows, and
follow-up polish documented below.

### Added
- Apache-2.0 LICENSE, NOTICE with third-party attributions, CONTRIBUTING,
  CODE_OF_CONDUCT (Contributor Covenant 2.1), and SECURITY policy.
- Top-level `Makefile` with `install`, `test`, `lint`, `dev`, `docker-up`,
  `docker-down`, `clean` targets.
- GitHub Actions CI: backend (ruff + pytest on Python 3.12), web
  (lint + typecheck + vitest + build on Node 20), bot (build + test).
- CodeQL workflow (Python + JavaScript/TypeScript) on push, PR, weekly cron.
- Nightly workflow running contract tests against Neo4j and NebulaGraph
  service containers.
- Dependabot configuration for pip, npm (web, bot), GitHub Actions, and Docker.
- Pre-commit hooks: ruff (check + format), prettier, eslint, detect-secrets,
  gitleaks, trailing-whitespace, end-of-file-fixer, check-yaml,
  check-merge-conflict.
- Issue and pull-request templates for bug reports, feature requests,
  and PRs with a trailers checklist.
- Ask Page v2: streaming token UI, progressive-disclosure ADK Agent Skills,
  Tools panel in the chat composer, stronger output contract, broader
  table/Mermaid rendering triggers, deduped citations.
- Multilingual wiki with a citations envelope.
- QA harness with trustworthy pass-rate, soft thresholds, and refusal-aware
  canaries.
- Pluggable graph database layer with Neo4j and NebulaGraph backends
  (`GRAPH_BACKEND` env var).
- Wiki generation, channel wiki view, FAQ page, Mermaid edge labels.
- Self-service platform integrations with multi-workspace support.
- Media nodes, entity-facts sidebar, link extraction, and graph filtering.
- Multimodal media handling with text-first vision routing.

### Changed
- Python minimum version raised to 3.12; ruff `target-version = "py312"`.
- README: Apache-2.0 badge and license link; `your-org` placeholders
  replaced with `TODO-OWNER`; Redis port unified on `6379`.
- README: added a **Privacy & Telemetry** section (no telemetry collected)
  and an **API Stability** notice.
- QA prompt contract: outcome-based retrieval with anti-meta-commentary
  (behind the `QA_RICH_OUTPUT` flag, renamed from `QA_NEW_PROMPT`).
- Tools panel moved into the `ChatInputBar` composer.
- Wiki/Ask language and session fallbacks tightened; topic-title
  translation hardened.
- Slack adapter: raw Slack API for message fetch and file proxy auth;
  cleaner mrkdwn parsing; thread-aware batching with refined bot filtering.
- Enterprise UI redesign: workspace-grouped sidebar, channels page,
  breadcrumbs, widened entity panel.

### Fixed
- Mermaid v11 silent syntax-error SVGs caught via pre-validate and content
  sniffing.
- QA skills: L3 resource files wired into ADK Resources; QA tools exposed
  as siblings of `SkillToolset`.
- Follow-up suggestions: bullet-strip guard and prompt contract.
- Slack channel sync errors, connection handling, and attachment image /
  unfurl extraction.
- Teams, Telegram, and Discord adapter configs aligned with Chat SDK docs.

### Security
- All `/api/*` endpoints in 0.1.0 declared **UNSTABLE**; v0.2.0 will
  introduce a `/api/v1/*` prefix. See `SECURITY.md`.
- Published a security policy with GitHub Security Advisories as the
  private reporting channel and a 72-hour initial-response commitment.

### Removed
- Unused `ANTHROPIC_API_KEY` environment variable.

[Unreleased]: https://github.com/votee/beever-atlas/compare/HEAD...HEAD
