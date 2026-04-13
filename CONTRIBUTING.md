# Contributing to Beever Atlas

Thanks for your interest in contributing. This project ships under the Apache
License 2.0, and all contributions are accepted under the same terms.

By participating, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Beever Atlas is a three-service monorepo (Python backend, TypeScript bot,
React web app) plus local databases via Docker.

### Prerequisites

- **Python 3.12+** with [uv](https://docs.astral.sh/uv/)
- **Node.js 20+** and npm
- **Docker** and Docker Compose (for local databases)

### Bootstrap

```bash
# Install all language toolchains
make install

# Start local databases (Weaviate, Neo4j, MongoDB, Redis)
make docker-up

# Run backend + web + bot in dev mode
make dev
```

### Running tests and linters

```bash
make test    # pytest + web vitest + bot build check
make lint    # ruff, eslint, typecheck
```

## Branch model

- `main` — always deployable
- `feature/<short-slug>` — new features
- `fix/<short-slug>` — bug fixes
- `oss-launch/<phase>` — open-source preparation work

Open pull requests against `main`. Feature branches are squash-merged by
default; keep history clean and trailers intact (see below).

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/) with
structured git trailers to preserve decision context. A commit looks like:

```
fix(auth): prevent silent session drops during long-running ops

Auth service returns inconsistent status codes on token expiry,
so the interceptor catches all 4xx and triggers inline refresh.

Constraint: Auth service does not support token introspection
Rejected: Extend token TTL to 24h | security policy violation
Confidence: high
Scope-risk: narrow
Directive: Error handling is intentionally broad — do not narrow without verifying upstream
Not-tested: Auth service cold-start latency >500ms
```

Recommended trailers (include when applicable, skip for trivial commits):

- `Constraint:` — active constraint that shaped this decision
- `Rejected:` — alternative considered | reason for rejection
- `Directive:` — warning or instruction for future modifiers of this code
- `Confidence:` — `high` | `medium` | `low`
- `Scope-risk:` — `narrow` | `moderate` | `broad`
- `Not-tested:` — edge case or scenario not covered by tests

## Developer Certificate of Origin (DCO)

All commits must be signed off to certify your right to contribute under the
project license. We follow the [Developer Certificate of Origin 1.1](https://developercertificate.org/).

Add a `Signed-off-by` trailer automatically with:

```bash
git commit -s -m "feat(...): ..."
```

This adds:

```
Signed-off-by: Your Name <your.email@example.com>
```

CI enforces the sign-off on every commit in a pull request.

## Pull request checklist

- [ ] Tests pass locally (`make test`)
- [ ] Linters pass locally (`make lint`)
- [ ] New or changed behavior has tests
- [ ] Commits follow the Conventional Commits format with trailers
- [ ] Every commit is signed off (`git commit -s`)
- [ ] User-visible changes are noted in `CHANGELOG.md` under `[Unreleased]`
- [ ] Breaking changes are called out in the PR description

## Reporting bugs and security issues

- **Functional bugs**: open a GitHub issue using the Bug Report template.
- **Security vulnerabilities**: do not open a public issue. Follow
  [SECURITY.md](SECURITY.md) to report privately via GitHub Security
  Advisories.

## Questions

For general questions and discussion, use GitHub Discussions. For anything
that belongs on the roadmap or requires design work, open an issue first so
we can align on direction before code review.
