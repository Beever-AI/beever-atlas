"""Wiki compiler benchmark harness.

Runs WikiCompiler.compile N times against a synthetic fixture with a cassette
fake LLM (no network). Records duration, parse failures, empty content, and
dash-wall pages. Writes results to a baseline JSON file.

CLI:
    python scripts/wiki_bench.py \\
        --fixture tests/wiki/fixtures/gathered_bench.json \\
        --cassette tests/wiki/fixtures/cassette_llm.json \\
        --runs 5 \\
        --out tests/wiki/fixtures/baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

# Add project root to path so imports work when run directly.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))


_ALNUM_RE = re.compile(r"[a-zA-Z0-9]")
_DASH_ROW_RE = re.compile(r"^\s*\|[\s\-\|:]+\|\s*$")


def _count_alnum(text: str) -> int:
    return len(_ALNUM_RE.findall(text))


def _is_dash_wall(content: str) -> bool:
    lines = content.splitlines()
    max_run = 0
    run = 0
    for line in lines:
        if _DASH_ROW_RE.match(line):
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_run >= 5


def _is_low_alnum(content: str) -> bool:
    if not content:
        return True
    return _count_alnum(content) / max(len(content), 1) < 0.2


def _is_degenerate_page(page) -> bool:
    content = getattr(page, "content", "") or ""
    if len(content) < 80:
        return True
    if _is_low_alnum(content):
        return True
    if _is_dash_wall(content):
        return True
    return False


class CassetteLLM:
    """Fake LLM that replays pre-recorded responses from a cassette file.

    Keyed lookup: first tries page_kind, then falls back to a generic response.
    On cache miss: raises CassetteMissError with the unhit key so callers know
    they need to re-record.
    """

    class CassetteMissError(RuntimeError):
        pass

    def __init__(self, cassette_path: Path) -> None:
        data = json.loads(cassette_path.read_text())
        self._entries: dict[str, Any] = data.get("entries", {})
        self._hits: set[str] = set()
        self._misses: list[str] = []

    def _lookup(self, key: str) -> dict | None:
        if key in self._entries:
            self._hits.add(key)
            return self._entries[key]
        return None

    def response_for(self, prompt: str, page_kind: str = "topic") -> str:
        entry = self._lookup(page_kind)
        if entry is None:
            # Try generic fallback keys.
            for fallback in ("topic", "overview"):
                entry = self._lookup(fallback)
                if entry is not None:
                    break
        if entry is None:
            self._misses.append(page_kind)
            raise self.CassetteMissError(
                f"Cassette miss for page_kind={page_kind!r}. "
                "Re-record required: run scripts/rerecord_cassettes.py"
            )
        # Return JSON-encoded response so _parse_llm_json can handle it.
        if isinstance(entry, dict) and "content" in entry:
            return json.dumps(entry)
        # analysis / translation entries are already dicts without content key.
        return json.dumps(entry)

    @property
    def misses(self) -> list[str]:
        return list(self._misses)

    def all_keys_hit(self) -> bool:
        return len(self._misses) == 0


def _load_fixture(fixture_path: Path) -> dict:
    """Load gathered_bench.json and deserialize domain objects."""
    from beever_atlas.models.domain import AtomicFact, ChannelSummary, TopicCluster

    raw = json.loads(fixture_path.read_text())

    channel_summary = ChannelSummary(**raw["channel_summary"])

    clusters = [TopicCluster(**c) for c in raw["clusters"]]

    cluster_facts: dict[str, list[AtomicFact]] = {}
    for cid, facts_raw in raw.get("cluster_facts", {}).items():
        cluster_facts[cid] = [AtomicFact(**f) for f in facts_raw]

    recent_facts = [AtomicFact(**f) for f in raw.get("recent_facts", [])]
    media_facts = [AtomicFact(**f) for f in raw.get("media_facts", [])]

    return {
        "channel_id": raw["channel_id"],
        "channel_name": raw["channel_name"],
        "channel_summary": channel_summary,
        "clusters": clusters,
        "cluster_facts": cluster_facts,
        "recent_facts": recent_facts,
        "media_facts": media_facts,
        "decisions": raw.get("decisions", []),
        "technologies": raw.get("technologies", []),
        "projects": raw.get("projects", []),
    }


def _make_fake_llm(cassette: CassetteLLM):
    """Return an async function that replaces WikiCompiler._llm_generate_json."""

    async def _fake_llm_generate_json(self_inner, prompt: str, temperature: float = 0.2) -> str:
        # Determine page_kind from prompt content heuristics.
        page_kind = _infer_page_kind(prompt)
        return cassette.response_for(prompt, page_kind)

    return _fake_llm_generate_json


def _infer_page_kind(prompt: str) -> str:
    """Heuristically determine page_kind from prompt text."""
    p = prompt.lower()
    if "translate" in p and "title" in p:
        return "translation"
    if "analyze" in p and "subpage" in p:
        return "analysis"
    if "overview" in p and "channel" in p and "cluster" in p:
        return "overview"
    if "people" in p or "team member" in p:
        return "people"
    if "decision" in p and "decided by" in p:
        return "decisions"
    if "faq" in p or "frequently asked" in p:
        return "faq"
    if "glossary" in p or "term" in p:
        return "glossary"
    if "activity" in p or "recent activity" in p:
        return "activity"
    if "resource" in p or "media" in p or "link" in p:
        return "resources"
    if "connection pool" in p or "database connection" in p:
        return "topic_database_connection_pooling"
    if "api redesign" in p or "rest architecture" in p:
        return "topic_api_redesign_and_rest_architecture"
    if "deployment pipeline" in p or "ci/cd" in p:
        return "topic_deployment_pipeline_and_ci_cd_improvements"
    if "monitoring" in p or "observability" in p:
        return "topic_monitoring_and_observability_stack"
    if "authentication" in p or "security hardening" in p:
        return "topic_authentication_and_security_hardening"
    if "engineering resource" in p or "documentation links" in p:
        return "topic_engineering_resources_and_documentation_links"
    return "topic"


def _count_warnings(log_records: list[logging.LogRecord], substring: str) -> int:
    return sum(1 for r in log_records if substring in r.getMessage())


class _WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _make_fake_provider():
    """Return a mock LLMProvider that returns a fixed model name."""
    from unittest.mock import MagicMock
    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    provider.get_model.return_value = "gemini-2.5-flash"
    return provider


async def _run_once(gathered: dict, cassette: CassetteLLM) -> tuple[float, list[logging.LogRecord], dict]:
    """Run one compile pass. Returns (wall_ms, log_records, pages)."""
    from beever_atlas.wiki.compiler import WikiCompiler

    handler = _WarningCapture()
    handler.setLevel(logging.WARNING)
    wiki_logger = logging.getLogger("beever_atlas.wiki.compiler")
    wiki_logger.addHandler(handler)
    wiki_logger.setLevel(logging.WARNING)

    fake_fn = _make_fake_llm(cassette)
    fake_provider = _make_fake_provider()

    try:
        t0 = time.perf_counter()
        # Patch at the compiler module's own reference so WikiCompiler.__init__
        # gets the fake provider without hitting the uninitialized singleton.
        with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=fake_provider):
            with patch.object(WikiCompiler, "_llm_generate_json", fake_fn):
                compiler = WikiCompiler()
                pages = await compiler.compile(gathered)
        wall_ms = (time.perf_counter() - t0) * 1000
    finally:
        wiki_logger.removeHandler(handler)

    return wall_ms, handler.records, pages


async def _bench_loop(
    gathered: dict,
    cassette: CassetteLLM,
    n_runs: int,
) -> tuple[list[float], int, int, int, int]:
    """Run every benchmark iteration in a single event loop.

    Returns (durations_ms, parse_failures_total, empty_content_total,
    dash_wall_pages_total, page_count). Per-iteration progress prints stay
    inside this coroutine so timing output matches the pre-#54 cadence.
    """
    durations: list[float] = []
    parse_failures_total = 0
    empty_content_total = 0
    dash_wall_pages_total = 0
    page_count = 0

    for run_idx in range(n_runs):
        wall_ms, records, pages = await _run_once(gathered, cassette)
        durations.append(wall_ms)

        parse_fail = _count_warnings(records, "failed to parse LLM JSON")
        empty = _count_warnings(records, "empty content after")
        parse_failures_total += parse_fail
        empty_content_total += empty

        dash_walls = sum(1 for p in pages.values() if _is_degenerate_page(p))
        dash_wall_pages_total += dash_walls
        page_count = len(pages)

        print(
            f"  run {run_idx + 1}/{n_runs}: {wall_ms:.0f}ms  "
            f"pages={page_count}  parse_fail={parse_fail}  "
            f"empty={empty}  dash_wall={dash_walls}"
        )

    return (
        durations,
        parse_failures_total,
        empty_content_total,
        dash_wall_pages_total,
        page_count,
    )


def _p_percentile(values: list[float], p: int) -> int:
    if not values:
        return 0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    idx = min(idx, len(sorted_vals) - 1)
    return int(sorted_vals[idx])


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def run_bench(
    fixture_path: Path,
    cassette_path: Path,
    n_runs: int,
    out_path: Path,
) -> dict:
    """Run the benchmark and write baseline.json. Returns the baseline dict."""
    import datetime

    gathered = _load_fixture(fixture_path)
    cassette = CassetteLLM(cassette_path)

    # Issue #54 — single asyncio.run() drives every iteration through one
    # event loop instead of N (was: asyncio.run() inside the for body),
    # avoiding loop-churn overhead and the FD-leak risk if any future
    # _run_once change adds real async resources.
    (
        durations,
        parse_failures_total,
        empty_content_total,
        dash_wall_pages_total,
        page_count,
    ) = asyncio.run(_bench_loop(gathered, cassette, n_runs))

    if cassette.misses:
        raise CassetteLLM.CassetteMissError(
            f"Cassette misses detected for keys: {cassette.misses}. "
            "Re-record required."
        )

    baseline = {
        "commit_sha": _git_sha(),
        "recorded_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "n_runs": n_runs,
        "page_count": page_count,
        "duration_ms_p50": _p_percentile(durations, 50),
        "duration_ms_p95": _p_percentile(durations, 95),
        "parse_failures_total": parse_failures_total,
        "empty_content_total": empty_content_total,
        "dash_wall_pages_total": dash_wall_pages_total,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(baseline, indent=2))
    print(f"\nBaseline written to {out_path}")
    print(json.dumps(baseline, indent=2))
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser(description="Wiki compiler benchmark harness")
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--cassette", required=True, type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    print(f"Running wiki bench: fixture={args.fixture} cassette={args.cassette} runs={args.runs}")
    run_bench(
        fixture_path=args.fixture,
        cassette_path=args.cassette,
        n_runs=args.runs,
        out_path=args.out,
    )


if __name__ == "__main__":
    main()
