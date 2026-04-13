"""External knowledge tool: Tavily web search."""

from __future__ import annotations

import asyncio
import logging

from beever_atlas.agents.tools._citation_decorator import cite_tool_output

logger = logging.getLogger(__name__)

SUPPORTED_MODES = frozenset({"general", "documentation", "best_practices"})


@cite_tool_output(kind="web_result")
async def search_external_knowledge(query: str, mode: str = "general") -> dict:
    """Search external web knowledge via Tavily API.

    Cost: ~$0.01. Target latency: ~1s.
    Requires TAVILY_API_KEY environment variable.

    Args:
        query: Search query.
        mode: "general", "documentation", or "best_practices".

    Returns:
        Dict with answer, results list, source attribution, or error info.
    """
    if mode not in SUPPORTED_MODES:
        mode = "general"

    try:
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        api_key = settings.tavily_api_key
        if not api_key:
            return {
                "error": "tavily_unavailable",
                "message": "TAVILY_API_KEY is not configured. External search unavailable.",
                "results": [],
                "source": "external",
            }

        from tavily import TavilyClient  # type: ignore[import]

        client = TavilyClient(api_key=api_key)
        search_depth = "advanced" if mode in ("documentation", "best_practices") else "basic"

        response: dict = await asyncio.to_thread(
            client.search,
            query=query,
            search_depth=search_depth,
            max_results=5,
            include_answer=True,
        )

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", "")[:500],
                # Expose a `text` field so the citation decorator can
                # pick it up as the source excerpt.
                "text": item.get("content", "")[:500],
                "score": item.get("score", 0.0),
            }
            for item in response.get("results", [])
        ]

        return {
            "answer": response.get("answer", ""),
            "results": results,
            "source": "external_tavily",
            "mode": mode,
        }

    except ImportError:
        return {
            "error": "tavily_not_installed",
            "message": "tavily package not installed. Run: pip install tavily-python",
            "results": [],
            "source": "external",
        }
    except Exception:
        logger.exception("search_external_knowledge failed for query=%s", query)
        return {
            "error": "search_failed",
            "message": "External search failed. Answering from internal memory only.",
            "results": [],
            "source": "external",
        }
