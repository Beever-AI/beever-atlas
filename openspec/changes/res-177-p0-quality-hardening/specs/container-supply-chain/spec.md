## ADDED Requirements

### Requirement: Every base image is pinned by `@sha256:<digest>`
Every `FROM` directive in every Dockerfile and every `image:` entry in
every compose file SHALL pin the base image by its `@sha256:<digest>`.
Floating tags (e.g. `python:3.12-slim` without a digest) SHALL NOT appear
in committed images. `COPY --from=<external-image>` stages SHALL likewise
pin by digest.

#### Scenario: Floating tag in a Dockerfile fails CI
- **WHEN** a contributor adds `FROM python:3.12-slim` (no digest)
- **THEN** CI fails with a lint step pointing at the unpinned image

#### Scenario: Every base image in the monorepo resolves to a digest
- **WHEN** an auditor greps every `FROM ` and `image:` line in
  `Dockerfile`, `web/Dockerfile`, `bot/Dockerfile`, `docker-compose.yml`,
  `docker-compose.nebula.yml`
- **THEN** every match includes `@sha256:<64-hex>`

### Requirement: Digest updates are automated via Dependabot
The `.github/dependabot.yml` config SHALL declare `package-ecosystem:
docker` for every path that contains a Dockerfile or compose file, so
Dependabot opens PRs when an upstream image publishes a new digest for
the pinned tag.

#### Scenario: Upstream digest change produces a Dependabot PR
- **WHEN** an upstream registry publishes a new digest for one of the
  pinned `<tag>` values
- **THEN** Dependabot opens a PR updating the digest, referencing the
  new upstream release notes

### Requirement: Deploy bootstrap uses only pinned images
`scripts/deploy/bootstrap.sh` (and any other deploy entry point) SHALL
invoke `docker compose build` against compose files whose images are all
digest-pinned. The bootstrap SHALL NOT mutate or strip digests at build
time.

#### Scenario: Deploy pipeline builds from pinned images only
- **WHEN** the deploy workflow runs `docker compose build`
- **THEN** every image pulled is identified by its digest, not a floating
  tag
