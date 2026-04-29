FROM python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286 AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.7.13@sha256:6c1e19020ec221986a210027040044a5df8de762eb36d5240e382bc41d7a9043 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./
COPY src/ src/

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
COPY --chown=app:app --from=builder /app/pyproject.toml /app/pyproject.toml

# Ensure the venv is on PATH
ENV PATH="/app/.venv/bin:$PATH"

# All RUN commands below execute as 'app'. New writable dirs MUST be created
# here, before USER, OR with explicit `chown app:app` after USER (otherwise the
# directory is root-owned and the runtime user can't write to it).
USER app

EXPOSE 8000

CMD ["uvicorn", "beever_atlas.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
