FROM python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286 AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.7.13@sha256:6c1e19020ec221986a210027040044a5df8de762eb36d5240e382bc41d7a9043 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./
COPY src/ src/
# Runtime imports from ``scripts/`` — keep this list minimal to limit the
# in-image attack surface (only the modules the running server imports
# should ship). Two paths import from ``scripts.*`` today:
#   1. ``server/app.py`` -> ``scripts.migrate_to_endpoint_catalog`` —
#      idempotent Endpoint+Assignment seeding shim (the bridge between
#      ``.env`` and the DB-as-source-of-truth model). Without this, the
#      first-boot migration silently fails with ModuleNotFoundError and
#      the operator's UI surfaces an empty Endpoint catalog.
#   2. ``services/embedding_migration_job.py`` -> ``scripts.reembed_facts``
#      — the re-embed worker used when an operator switches the embedding
#      Endpoint to a new dimension.
# When adding any other ``scripts.*`` runtime import, EXTEND THIS COPY
# rather than reverting to ``COPY scripts/ scripts/`` — the latter ships
# 600+ KB of dev-only tooling (dry runs, benchmarks, smoke tests) that
# expand attack surface for no runtime benefit.
COPY scripts/__init__.py scripts/migrate_to_endpoint_catalog.py scripts/reembed_facts.py scripts/

# Install dependencies into a virtual env using the lockfile
RUN uv sync --frozen --no-dev

FROM python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286

WORKDIR /app

# Issue #39 — drop root. UID 10001 is fixed (not `--system`'s arbitrary 100-999)
# for k8s `runAsNonRoot` SCC compatibility — survives base-image upgrades unchanged.
# `chown app:app /app` ensures the WORKDIR itself is `app`-owned so the file-import
# staging feature (`/app/.omc/imports`, see infra/config.py:61, api/imports.py:148)
# can `mkdir -p` at runtime under non-root.
RUN addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --ingroup app --no-create-home app && \
    chown app:app /app

# Copy the built venv and source from builder, owned by `app`.
COPY --chown=app:app --from=builder /app/.venv /app/.venv
COPY --chown=app:app --from=builder /app/src /app/src
# Mirrors the selective builder-stage COPY above — only modules the
# running server imports from ``scripts.*``. See builder COPY comment
# for the canonical list and why minimising matters.
COPY --chown=app:app --from=builder /app/scripts /app/scripts
COPY --chown=app:app --from=builder /app/pyproject.toml /app/pyproject.toml

# Ensure the venv is on PATH
ENV PATH="/app/.venv/bin:$PATH"

# All RUN commands below execute as 'app'. New writable dirs MUST be created
# here, before USER, OR with explicit `chown app:app` after USER (otherwise the
# directory is root-owned and the runtime user can't write to it).
USER app

# MCP Registry ownership annotation — MUST byte-for-byte equal `name` in
# server.json (io.github.Beever-AI/beever-atlas — the GitHub org slug case is
# significant; the registry's namespace authorization is case-sensitive). The
# registry validator pulls the published manifest and reads this label to verify
# OCI artifact ownership.
LABEL io.modelcontextprotocol.server.name="io.github.Beever-AI/beever-atlas"

EXPOSE 8000

CMD ["uvicorn", "beever_atlas.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
