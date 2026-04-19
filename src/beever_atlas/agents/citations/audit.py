"""Audit & query helpers for citation data (Phase 3).

Exposes:
- `verify_tool_coverage()` — inspects `agents/tools/*.py` and flags any
  retrieval tool that isn't wrapped by `@cite_tool_output`.
- Query helpers backed by `ChatHistoryStore` and `QAHistoryStore` for the
  admin endpoints and any future reporting.

All reads funnel through the envelope shim so legacy rows (flat
`Citation[]`) are normalized before aggregation.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---- tool coverage audit ---------------------------------------------


# Tools that intentionally don't produce citable output and therefore
# don't need the decorator. Keep this list narrow and justified.
EXEMPT_TOOLS: set[str] = {
    # Resolvers and helper factories — they produce channel metadata,
    # not citations.
    "resolve_channel_name",
    # Factory that may or may not be citable depending on call shape.
    "search_weaviate_hybrid",
    # Neo4j traversals that return aggregated views, not per-source records.
    "search_relationships",
    # People-ranking — not citable per-person.
    "find_experts",
    # MCP-registry glue helpers don't produce citations themselves.
    "get_tier0_summary",
    "get_tier1_clusters",
    "traverse_neo4j",
    "temporal_chain",
    "comprehensive_traverse",
    "get_episodic_weaviate_ids",
    "search_tavily",
    "upsert_fact",
    "upsert_entity",
    "create_episodic_link",
    # Phase 6 orchestration tools — return structured operational dicts
    # (job envelopes, connection/channel lists, job status) rather than
    # citeable retrieval results. Citations don't apply to action outcomes.
    "list_connections_tool",
    "list_channels_tool",
    "trigger_sync_tool",
    "refresh_wiki_tool",
    "get_job_status_tool",
}


def verify_tool_coverage() -> dict[str, list[str]]:
    """Walk `beever_atlas.agents.tools`, return decorator coverage.

    A tool is considered "citable" when it is:
    - defined at module top-level, AND
    - declared as `async def`, AND
    - its name doesn't start with `_`, AND
    - not in the `EXEMPT_TOOLS` allowlist.

    A tool is considered "covered" when its function object has our
    decorator's sentinel attribute `_cite_tool_kind`. This is strictly
    narrower than `hasattr(fn, "__wrapped__")` (which `functools.wraps`
    sets for any wrapping decorator — logging, timing, etc.) so the
    audit cannot false-positive on unrelated wrappers.
    """
    covered: list[str] = []
    uncovered: list[str] = []
    exempt: list[str] = []

    package_name = "beever_atlas.agents.tools"
    try:
        pkg = importlib.import_module(package_name)
    except Exception:
        logger.warning("verify_tool_coverage: could not import %s", package_name, exc_info=True)
        return {"covered": [], "uncovered": [], "exempt": []}

    for modinfo in pkgutil.iter_modules(getattr(pkg, "__path__", []), f"{package_name}."):
        if modinfo.name.endswith("._citation_decorator"):
            continue
        try:
            mod = importlib.import_module(modinfo.name)
        except Exception:
            logger.debug("verify_tool_coverage: skipping %s (import failed)", modinfo.name)
            continue

        for name, fn in inspect.getmembers(mod, inspect.iscoroutinefunction):
            if name.startswith("_"):
                continue
            if getattr(fn, "__module__", "") != mod.__name__:
                continue  # Imported symbol, not defined here.
            if name in EXEMPT_TOOLS:
                exempt.append(f"{mod.__name__}.{name}")
                continue
            # Our decorator stamps `_cite_tool_kind` — presence of
            # `__wrapped__` alone is not enough (any functools.wraps-based
            # decorator would set it).
            if getattr(fn, "_cite_tool_kind", None) is not None:
                covered.append(f"{mod.__name__}.{name}")
            else:
                uncovered.append(f"{mod.__name__}.{name}")

    return {
        "covered": sorted(covered),
        "uncovered": sorted(uncovered),
        "exempt": sorted(exempt),
    }


# ---- query helpers ---------------------------------------------------


def _normalize_envelope(raw: Any) -> dict[str, Any]:
    """Local copy of the read shim to avoid import cycles through stores."""
    from beever_atlas.agents.citations.persistence import upgrade_envelope

    return upgrade_envelope(raw)


def iter_sources_from_message(msg: dict) -> Iterable[dict]:
    """Yield source dicts for a single message, regardless of citation regime."""
    env = _normalize_envelope(msg.get("citations"))
    for s in env.get("sources") or []:
        yield s


def dedup_by_id(sources: Iterable[dict]) -> list[dict]:
    """Dedup an iterable of source dicts by `id`, preserving first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for s in sources:
        sid = s.get("id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(s)
    return out
