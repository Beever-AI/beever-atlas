"""External knowledge tool: web search."""

from __future__ import annotations

import asyncio
import logging

from beever_atlas.agents.tools._citation_decorator import cite_tool_output

logger = logging.getLogger(__name__)

SUPPORTED_MODES = frozenset({"general", "documentation", "best_practices"})


@cite_tool_output(kind="web_result")
async def search_external_knowledge(query: str, mode: str = "general") -> dict:
    """Search external web knowledge via Tavily or Olostep.

    Cost: ~$0.01. Target latency: ~1s.
    Requires TAVILY_API_KEY or OLOSTEP_API_KEY environment variable.

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
        provider = settings.web_search_provider
        logger.info("web_search.provider=%s mode=%s", provider, mode)
        if provider == "olostep":
            api_key = settings.olostep_api_key
            if not api_key:
                return {
                    "error": "olostep_unavailable",
                    "message": "OLOSTEP_API_KEY is not configured. External search unavailable.",
                    "results": [],
                    "source": "external",
                }

            results = await asyncio.to_thread(
                search_with_olostep,
                query,
                api_key,
                5,
            )

            return {
                "answer": "",
                "results": results,
                "source": "external_olostep",
                "mode": mode,
            }

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
            include_raw_content="markdown",
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


def search_with_olostep(query: str, api_key: str, max_results: int = 5) -> list[dict]:
    """Search the web using Olostep /searches endpoint.

    Returns a list of result dicts with 'title', 'url', 'content', 'text',
    and 'score' keys to match the shape expected by callers.
    """
    import httpx

    response = httpx.post(
        "https://api.olostep.com/v1/searches",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    links = data.get("result", {}).get("links", [])
    if not links:
        logger.warning("olostep response missing links: %s", list(data.keys()))
    links = links[:max_results]

    return [
        {
            "title": link.get("title", ""),
            "url": link.get("url", ""),
            "content": link.get("description", "")[:500],
            "text": link.get("description", "")[:500],
            "score": 0.0,
        }
        for link in links
    ]
