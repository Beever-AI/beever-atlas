# `scripts/` — Developer & Operations Scripts

Top-level helper scripts for development, benchmarking, and one-off ops tasks.
The `deploy/` subdirectory has its own README.

## Sync & Ingestion

| Script | Description |
|---|---|
| `dry_run_ingestion.py` | End-to-end ingestion dry-run with rich diagnostic logging against synthetic messages (no data persisted). |
| `dry_run_live.py` | Live dry-run — fetches real Slack messages via the bridge and runs them through ingestion stages (no data persisted). |
| `dry_run_pipeline_hardening.py` | Tests all 7 pipeline-hardening features in isolation (coreference, dedup, multimodal, semantic search, temporal facts, thread context, orphan handling) without live services. |
| `dry_run_enrichment.py` | Dry-run for the knowledge-tier consolidation pipeline using rich mock data. |
| `dry_run_eof_hardening.py` | Validates output-aware adaptive batching and the LLM-EOF truncation detector without calling Gemini. |
| `dry_run_file_import.py` | Dry-run the CSV/XLSX file-import pipeline against a local file; prints detected format and column mapping without writing to any store. |
| `bench_ingest.py` | Benchmark harness — triggers a real sync via the backend API and polls status; measures throughput and latency. |
| `test_ingestion_mock.py` | Full pipeline test with synthetic messages covering all hardening features (no live platform connections). |

## Graph & Entity

| Script | Description |
|---|---|
| `backfill_name_vectors.py` | Compute and store `name_vector` embeddings for existing Neo4j entities that are missing them. |
| `dedupe_teams_connections.py` | One-off cleanup: remove duplicate Microsoft Teams platform connections with the same `appId`. |
| `nebula_setup.py` | NebulaGraph connectivity helper — registers storage hosts and verifies the connection. |

## QA Harness

| Script | Description |
|---|---|
| `qa_test_harness.py` | Multi-persona QA agent test harness with "existing user" and "onboarding" question sets; grades accuracy and attribution. |
| `dry_run_ask_session_scoped.py` | Validates the session-scoped Ask refactor via code/import checks and optional live HTTP probes. |
| `wiki_bench.py` | Wiki compiler benchmark — runs `WikiCompiler.compile` N times against a cassette LLM and writes a baseline JSON file. |

## Media & Local Models

| Script | Description |
|---|---|
| `test_gemma4_local.py` | Tests Gemma 4 via Ollama + ADK LiteLLM integration (structured output, image description). |

## Platform & Fixtures

| Script | Description |
|---|---|
| `test_platform_bridge.py` | Dry-run test of multi-platform bridge endpoints — verifies HTTP status codes and response shapes for channels, messages, and file proxy. |
| `check_mattermost_channels.py` | Lists all channels a Mattermost bot can see along with message counts. |
| `dry_run_policy.py` | Dry-run for the configurable sync-policy system (policy resolution cascade, preset definitions, MongoDB integration). |
| `dry_run_ui_layout.py` | Validates the UI layout redesign by checking sidebar nav items, routes, and component contracts. |
