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

# Copy the built venv and source from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

# Ensure the venv is on PATH
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "beever_atlas.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
