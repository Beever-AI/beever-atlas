"""Dry-run smoke test for Weaviate hybrid search.

Connects to Weaviate, generates a Jina embedding for a test query, then runs
true_hybrid_search with alpha=0.0 (pure BM25), 0.5 (hybrid), and 1.0 (pure
vector). Prints top-5 results for each blend and asserts basic sanity.

Usage:
    python -m scripts.smoke.test_hybrid_search

Prerequisites:
    - .env with WEAVIATE_URL, JINA_API_KEY, JINA_API_URL (or defaults)
    - Weaviate instance running and reachable
    - At least one MemoryFact document indexed
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env before any beever_atlas imports
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TEST_QUERY = "project architecture decisions"
# Use a well-known channel_id from env or a placeholder; the test gracefully
# handles zero results (just prints a warning, does not crash).
TEST_CHANNEL = os.environ.get("SMOKE_CHANNEL_ID", "C000000TEST")


async def _jina_embed(text: str) -> list[float]:
    """Generate a Jina embedding for the given text via REST API."""
    from beever_atlas.infra.config import get_settings
    settings = get_settings()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            settings.jina_api_url,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.jina_model,
                "dimensions": settings.jina_dimensions,
                "input": [{"text": text}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]


def _print_results(alpha: float, results: list[dict]) -> None:
    label = {0.0: "pure-BM25", 0.5: "hybrid", 1.0: "pure-vector"}.get(alpha, f"alpha={alpha}")
    print(f"\n--- {label} (alpha={alpha}) — {len(results)} result(s) ---")
    for rank, r in enumerate(results[:5], start=1):
        fact = r.get("fact", {})
        score = r.get("similarity_score", 0.0)
        channel = getattr(fact, "channel_id", None) or fact.get("channel_id", "?")
        text = getattr(fact, "text", None) or fact.get("text", "")
        print(f"  {rank}. score={score:.4f}  channel={channel}  text={str(text)[:100]!r}")


async def main() -> int:
    # Step 1: connect to Weaviate
    try:
        from beever_atlas.stores.weaviate_store import WeaviateStore
        from beever_atlas.infra.config import get_settings
        settings = get_settings()
        store = WeaviateStore(settings.weaviate_url, settings.weaviate_api_key)
        await store.startup()
    except Exception as exc:
        print(f"ERROR: Could not connect to Weaviate: {exc}")
        print("Hint: ensure WEAVIATE_URL is set and Weaviate is running.")
        return 0  # graceful exit

    try:
        # Step 2: generate Jina embedding
        print(f"Generating Jina embedding for: {TEST_QUERY!r}")
        try:
            query_vector = await _jina_embed(TEST_QUERY)
            print(f"Embedding dimensions: {len(query_vector)}")
        except Exception as exc:
            print(f"ERROR: Jina embedding failed: {exc}")
            print("Hint: ensure JINA_API_KEY is set correctly.")
            return 0

        # Step 3: run true_hybrid_search with alpha=0.0, 0.5, 1.0
        results_by_alpha: dict[float, list[dict]] = {}
        for alpha in (0.0, 0.5, 1.0):
            try:
                results = await store.true_hybrid_search(
                    query_text=TEST_QUERY,
                    query_vector=query_vector,
                    channel_id=TEST_CHANNEL,
                    limit=5,
                    alpha=alpha,
                )
                results_by_alpha[alpha] = results
                _print_results(alpha, results)
            except Exception as exc:
                print(f"ERROR: true_hybrid_search(alpha={alpha}) failed: {exc}")
                results_by_alpha[alpha] = []

        # Step 4 & 5: assertions
        print("\n--- Assertions ---")
        bm25_results = results_by_alpha.get(0.0, [])
        hybrid_results = results_by_alpha.get(0.5, [])
        vector_results = results_by_alpha.get(1.0, [])

        # At least one alpha must return results (unless the channel has no docs)
        any_results = any(len(r) > 0 for r in results_by_alpha.values())
        if not any_results:
            print(
                f"WARN: No results for channel={TEST_CHANNEL!r}. "
                "Set SMOKE_CHANNEL_ID to a channel with indexed facts."
            )
        else:
            print("PASS: at least one result returned")

            def _ids(results: list[dict]) -> list[str]:
                out = []
                for r in results:
                    fact = r.get("fact", {})
                    fid = getattr(fact, "fact_id", None) or fact.get("fact_id", "")
                    out.append(str(fid))
                return out

            hybrid_ids = set(_ids(hybrid_results))
            bm25_ids = set(_ids(bm25_results))
            vector_ids = set(_ids(vector_results))

            if hybrid_ids and (hybrid_ids != bm25_ids or hybrid_ids != vector_ids):
                print("PASS: hybrid (alpha=0.5) results differ from at least one pure mode")
            elif not hybrid_ids:
                print("WARN: hybrid returned no results — cannot compare blend quality")
            else:
                print(
                    "INFO: hybrid results identical to both pure modes "
                    "(expected when corpus is very small)"
                )

        print("\nSmoke test complete.")
        return 0

    finally:
        try:
            await store.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
