# Beever Atlas Web

The Beever Atlas v2 web app — React/TypeScript frontend for the knowledge-memory platform.

## Prerequisites

- Node.js 20+
- A running Beever Atlas backend (see root README)

## Commands

| Command | Description |
|---|---|
| `npm install` | Install dependencies |
| `npm run dev` | Start Vite dev server at `http://localhost:5173` with HMR |
| `npm test` | Run Vitest unit tests |
| `npm run lint` | Run ESLint across `src/` |
| `npm run build` | Production build into `dist/` (Vite bundles and tree-shakes) |

## Environment Variables

The web app reads env from the **root `.env`** (via Vite's `envDir`). There is no separate `web/.env` — edit the root file and the change applies to both `npm run dev` and Docker builds. See [`../.env.example`](../.env.example) §1.4 for the VITE_* vars.

| Variable | Purpose |
|---|---|
| `VITE_API_URL` | Base URL of the backend API (default: `http://localhost:8000`) |
| `VITE_BEEVER_API_KEY` | Bearer token injected into every `/api/*` request |
| `VITE_BEEVER_ADMIN_TOKEN` | Admin token for `/api/dev/*` calls |

> **Note**: Vite inlines these values at build time. They are visible in the browser bundle — treat `VITE_BEEVER_API_KEY` as a low-privilege read-only key.

## Project Layout

| Path | Contents |
|---|---|
| `src/pages/` | Top-level route pages (one file per route) |
| `src/components/` | Reusable UI components, organized by feature area |
| `src/hooks/` | Custom React hooks |
| `src/lib/` | API client (`api.ts`) and shared utilities |
| `public/` | Static assets served as-is (favicons, logo) |

## Further Reading

- [Root README](../README.md) — architecture overview, quick-start, Docker setup
- [CONTRIBUTING.md](../CONTRIBUTING.md) — commit conventions, PR workflow, pre-commit hooks
