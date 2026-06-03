"""Media processor — downloads, extracts text, and describes media attachments.

Supports:
- PDF text extraction via pypdf
- Image description via Gemini vision (text-first routing: only when message text is insufficient)
- Bounded-async processing with per-message timeout and concurrency control
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, overload

import httpx

from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


class _Oversize:
    """Sentinel for a download rejected solely by the size cap.

    ``_download_file`` normally collapses every failure (auth, expired URL,
    HTML login page, network error, oversize) into ``None``. The durable
    backfill needs to tell a genuine size-cap rejection apart from those so it
    can report it under ``too_large`` instead of ``download_failed``. Callers
    opt in via ``return_oversize_sentinel=True``; everyone else keeps the
    ``None``-means-failure contract unchanged. Falsy on purpose so existing
    ``if not data`` guards still treat it as "no bytes".
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False


# Module-level singleton — identity-compared by callers (``is OVERSIZE``).
OVERSIZE = _Oversize()


@dataclass(frozen=True, slots=True)
class MediaContext:
    """Per-message context threaded into durable media persistence.

    Carries the channel/message/source identifiers that scope a stored blob
    and its ref so the channel-purge fan-out can drop them later. The platform
    URL remains the identifier used everywhere else; this is storage-only.
    """

    channel_id: str = ""
    message_id: str = ""
    source_id: str = ""


# Patterns that suggest the user is referencing an attachment
_ATTACHMENT_REF_PATTERNS = re.compile(
    r"see attached|check this|look at this|attached|screenshot|see above|here'?s the",
    re.IGNORECASE,
)

# Maximum characters of extracted PDF text to include
_MAX_PDF_TEXT_CHARS = 5000

# Filenames matching these patterns suggest visual content worth describing
_VISUAL_FILENAME_RE = re.compile(
    r"screenshot|diagram|chart|graph|whiteboard|mockup|wireframe|design|sketch",
    re.IGNORECASE,
)


class MediaProcessor:
    """Download, extract, and describe media attachments from Slack messages."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._sem = asyncio.Semaphore(3)
        self._supported_images = set(self._settings.media_supported_image_types.split(","))
        self._supported_docs = set(self._settings.media_supported_doc_types.split(","))
        self._max_bytes = self._settings.media_max_file_size_mb * 1024 * 1024
        self._last_pdf_chunks: list[str] = []
        self._http_client: httpx.AsyncClient | None = None

    @staticmethod
    def get_registry():
        """Get the singleton media extractor registry."""
        from beever_atlas.services.media_extractors import create_default_registry

        if not hasattr(MediaProcessor, "_registry"):
            MediaProcessor._registry = create_default_registry()
        return MediaProcessor._registry

    # ── Public API ──────────────────────────────────────────────────────

    async def process_message_media(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Process all media attachments for a single message.

        Returns dict with:
            description: str — formatted text to append to message content
            media_urls: list[str] — URLs of processed attachments
            media_type: str — primary media type ("image", "pdf", "")
        """
        attachments = msg.get("attachments") or []
        files = msg.get("files") or []
        all_media = attachments + files

        if not all_media:
            return {"description": "", "media_urls": [], "media_type": ""}

        message_text = msg.get("text") or msg.get("content") or ""

        # Message context for durable media persistence. The platform URL stays
        # the identifier everywhere; these fields scope the stored blob/ref so
        # the channel-purge fan-out can drop it later. message_id falls back
        # through the platform-specific keys that may carry it.
        media_ctx = MediaContext(
            channel_id=msg.get("channel_id", ""),
            message_id=(msg.get("message_id") or msg.get("msg_id") or msg.get("ts") or ""),
            source_id=(msg.get("platform") or msg.get("source_id") or ""),
        )
        descriptions: list[str] = []
        media_urls: list[str] = []
        media_type = ""

        timeout = self._settings.media_vision_timeout_seconds

        async def _safe_process(att: dict[str, Any]) -> dict[str, str]:
            """Process a single attachment with timeout and error handling."""
            try:
                return await asyncio.wait_for(
                    self._process_attachment(att, message_text, ctx=media_ctx),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                name = att.get("name", "unknown")
                url = att.get("url", "")
                att_type = att.get("type", "file")
                logger.warning(
                    "MediaProcessor: timeout processing attachment %s (limit=%ds)",
                    name,
                    timeout,
                )
                return {
                    "description": f"[Attachment: {name} ({att_type}, processing timed out)]",
                    "media_url": url,
                    "media_type": att_type,
                }
            except Exception:
                name = att.get("name", "unknown")
                logger.warning(
                    "MediaProcessor: failed to process attachment %s",
                    name,
                    exc_info=True,
                )
                return {"description": "", "media_url": "", "media_type": ""}

        results = await asyncio.gather(*[_safe_process(att) for att in all_media])

        for result in results:
            if result["description"]:
                descriptions.append(result["description"])
            if result["media_url"]:
                media_urls.append(result["media_url"])
            if result["media_type"] and not media_type:
                media_type = result["media_type"]

        result: dict[str, Any] = {
            "description": "\n\n".join(descriptions),
            "media_urls": media_urls,
            "media_type": media_type,
        }
        # Pass through PDF chunks for virtual message expansion
        if hasattr(self, "_last_pdf_chunks") and self._last_pdf_chunks:
            result["chunks"] = self._last_pdf_chunks
            self._last_pdf_chunks = []
        return result

    # ── Internal methods ────────────────────────────────────────────────

    async def _process_attachment(
        self,
        att: dict[str, Any],
        message_text: str,
        *,
        ctx: MediaContext | None = None,
    ) -> dict[str, str]:
        """Route a single attachment to the appropriate extractor via registry."""
        url = att.get("url") or att.get("url_private") or ""
        name = att.get("name") or "file"
        att_type = att.get("type") or ""
        mimetype = att.get("mimetype") or ""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        if not url:
            return {"description": "", "media_url": "", "media_type": ""}

        # Infer mimetype from att_type/ext when not provided
        if not mimetype:
            if att_type == "image" or ext in self._supported_images:
                mimetype = f"image/{ext}" if ext else "image/png"
            elif ext in self._supported_docs:
                mimetype = "application/pdf"

        registry = self.get_registry()
        extractor = registry.get_extractor(mimetype, name)

        if extractor is None:
            return {
                "description": f"[Attachment: {name} ({att_type or ext})]",
                "media_url": url,
                "media_type": att_type or ext,
            }

        # Download file
        async with self._sem:
            data = await self._download_file(url)
        if not data:
            return {
                "description": f"[Attachment: {name} ({att_type or ext})]",
                "media_url": url,
                "media_type": att_type or ext,
            }

        # Persist the raw bytes durably (best-effort) before extraction — the
        # platform URL rots but the content-addressed blob survives.
        await self._persist_blob_best_effort(
            data=data,
            mime_type=mimetype,
            filename=name,
            url=url,
            ctx=ctx,
        )

        # Extract content via registry
        content = await extractor.extract(data, name, metadata={"message_text": message_text})

        # Pass through PDF chunks for virtual message expansion
        if content.chunks:
            self._last_pdf_chunks = content.chunks

        return {
            "description": content.text,
            "media_url": url,
            "media_type": content.media_type or att_type or ext,
        }

    async def _persist_blob_best_effort(
        self,
        *,
        data: bytes,
        mime_type: str,
        filename: str,
        url: str,
        ctx: MediaContext | None,
    ) -> None:
        """Store the downloaded bytes in the durable blob store, best-effort.

        Persistence is a side effect of ingestion that must NEVER block or
        break extraction: any failure (disabled flag, missing store, Mongo
        error) is swallowed so the caller continues to the extractor. The
        platform ``url`` is passed through as the ref identity; the store
        normalizes it to a stable host+path ``url_key`` (and skips the ref
        entirely for Telegram / unparseable URLs).
        """
        settings = self._settings
        if not settings.channel_media_persist:
            return

        # Imported lazily so unit tests that don't init stores (and module
        # import) stay cheap — mirrors the registry/import-on-use convention.
        from beever_atlas.stores import get_stores

        try:
            blob_store = getattr(get_stores(), "media_blob_store", None)
        except Exception:
            # Stores not initialized (CLI entry points, partial test setup).
            blob_store = None
        if blob_store is None:
            return

        media_ctx = ctx or MediaContext()
        try:
            await blob_store.save_blob(
                content=data,
                mime_type=mime_type,
                filename=filename,
                channel_id=media_ctx.channel_id,
                source_id=media_ctx.source_id,
                message_id=media_ctx.message_id,
                platform_url=url,
            )
        except Exception:
            logger.warning(
                "MediaProcessor: durable media persist failed url=%s",
                url,
                exc_info=True,
            )

    async def _handle_pdf(
        self, url: str, name: str, *, ctx: MediaContext | None = None
    ) -> dict[str, str]:
        """Download and extract text from a PDF using chunked extraction."""
        async with self._sem:
            data = await self._download_file(url)

        if not data:
            return {"description": "", "media_url": url, "media_type": "pdf"}

        await self._persist_blob_best_effort(
            data=data,
            mime_type="application/pdf",
            filename=name,
            url=url,
            ctx=ctx,
        )

        from beever_atlas.services.media_extractors import PdfExtractor

        extractor = PdfExtractor()
        content = await extractor.extract(data, name)

        # Store chunks for passthrough to preprocessor
        if len(content.chunks) > 1:
            self._last_pdf_chunks = content.chunks

        return {"description": content.text, "media_url": url, "media_type": "pdf"}

    async def _handle_image(
        self, url: str, name: str, message_text: str, *, ctx: MediaContext | None = None
    ) -> dict[str, str]:
        """Download and optionally describe an image via vision LLM."""
        if not self.should_use_vision(message_text, {"name": name}):
            # Text is sufficient — metadata only
            return {
                "description": f"[Attachment: {name} (image)]",
                "media_url": url,
                "media_type": "image",
            }

        async with self._sem:
            data = await self._download_file(url)

        if not data:
            return {
                "description": f"[Attachment: {name} (image)]",
                "media_url": url,
                "media_type": "image",
            }

        await self._persist_blob_best_effort(
            data=data,
            mime_type="image/png",
            filename=name,
            url=url,
            ctx=ctx,
        )

        description = await self._describe_image(data, message_text)
        size_kb = len(data) // 1024

        if description:
            desc = f"[Attachment: {name} (image, {size_kb} kB)]\n[Image description]: {description}"
        else:
            desc = f"[Attachment: {name} (image, {size_kb} kB)]"

        return {"description": desc, "media_url": url, "media_type": "image"}

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Return a shared httpx client, creating it lazily."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self) -> None:
        """Close the shared httpx client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    @overload
    async def _download_file(
        self,
        url: str,
        _retries: int = ...,
        connection_id: str | None = ...,
        *,
        return_oversize_sentinel: Literal[False] = ...,
    ) -> bytes | None: ...

    @overload
    async def _download_file(
        self,
        url: str,
        _retries: int = ...,
        connection_id: str | None = ...,
        *,
        return_oversize_sentinel: Literal[True],
    ) -> "bytes | _Oversize | None": ...

    async def _download_file(
        self,
        url: str,
        _retries: int = 3,
        connection_id: str | None = None,
        *,
        return_oversize_sentinel: bool = False,
    ) -> "bytes | _Oversize | None":
        """Download a file via the bridge file proxy with retry on 429.

        The raw ``url`` originates from platform message attachments or
        file-import pipelines and is attacker-controllable. We therefore
        validate it against the platform allowlist and encode it before
        forwarding — mitigation for security finding H3.

        Returns the raw bytes on success, ``None`` on any failure. When
        ``return_oversize_sentinel`` is set, a download rejected *only* because
        it exceeds the size cap returns the falsy :data:`OVERSIZE` sentinel
        instead of ``None`` so the backfill can report it under ``too_large``;
        the default keeps the ``None``-means-failure contract for every other
        caller.
        """
        from urllib.parse import quote, urlparse

        from beever_atlas.infra.http_safe import validate_proxy_url

        try:
            encoded_url = validate_proxy_url(url)
        except (PermissionError, ValueError) as exc:
            host = urlparse(url).hostname if url else None
            logger.warning(
                "MediaProcessor: rejected non-allowlisted file url host=%s reason=%s",
                host,
                type(exc).__name__,
            )
            return None

        settings = self._settings
        proxy_url = f"{settings.bridge_url}/bridge/files?url={encoded_url}"
        if connection_id:
            proxy_url += f"&connection_id={quote(connection_id, safe='')}"
        headers: dict[str, str] = {}
        if settings.bridge_api_key:
            headers["Authorization"] = f"Bearer {settings.bridge_api_key}"

        try:
            client = await self._get_http_client()
            resp = await client.get(proxy_url, headers=headers)

            # Retry on 429 (rate limited) with exponential backoff
            if resp.status_code == 429 and _retries > 0:
                retry_after = int(resp.headers.get("retry-after", "3"))
                wait = max(retry_after, 2)
                logger.info(
                    "MediaProcessor: rate limited (429), retrying in %ds url=%s",
                    wait,
                    url[:80],
                )
                await asyncio.sleep(wait)
                return await self._download_file(
                    url,
                    _retries - 1,
                    connection_id=connection_id,
                    return_oversize_sentinel=return_oversize_sentinel,
                )

            if resp.status_code != 200:
                logger.warning(
                    "MediaProcessor: download failed status=%d url=%s",
                    resp.status_code,
                    url[:80],
                )
                return None

            # Detect HTML responses (e.g. Slack login page instead of actual file)
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct or resp.content[:15].lstrip().startswith(b"<!DOC"):
                logger.warning(
                    "MediaProcessor: got HTML instead of file content url=%s",
                    url[:80],
                )
                return None

            if len(resp.content) > self._max_bytes:
                logger.info(
                    "MediaProcessor: skipping file >%dMB: %s",
                    settings.media_max_file_size_mb,
                    url[:80],
                )
                return OVERSIZE if return_oversize_sentinel else None

            return resp.content
        except Exception:
            logger.warning("MediaProcessor: download error url=%s", url[:80], exc_info=True)
            return None

    def _extract_pdf_text(self, data: bytes) -> str:
        """Extract text from PDF bytes using pypdf with page-aware truncation."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            total_pages = len(reader.pages)
            pages: list[str] = []
            char_count = 0
            pages_extracted = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text.strip())
                    char_count += len(text.strip())
                    pages_extracted += 1
                    if char_count >= _MAX_PDF_TEXT_CHARS:
                        break
            result = "\n\n".join(pages)
            if char_count >= _MAX_PDF_TEXT_CHARS:
                result = result[:_MAX_PDF_TEXT_CHARS]
                remaining = total_pages - pages_extracted
                if remaining > 0:
                    result += f"\n[...truncated, {remaining} more pages]"
            return result
        except Exception:
            logger.warning("MediaProcessor: PDF text extraction failed", exc_info=True)
            return ""

    async def _describe_image(self, data: bytes, message_context: str) -> str:
        """Describe an image via the LiteLLM funnel (OpenAI multimodal shape)."""
        try:
            import base64

            from beever_atlas.services.llm_dispatch import (
                dispatch_completion,
                normalize_litellm_model,
                sniff_provider,
            )

            prompt = (
                "Describe this image concisely for a knowledge extraction system. "
                "Focus on: key data points, text visible in the image, chart/graph values, "
                "names, dates, and any actionable information. "
                "Keep the description under 200 words."
            )
            if message_context:
                prompt += f"\n\nMessage context: {message_context[:200]}"

            model_name = self._settings.media_vision_model
            data_url = f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"

            response = await asyncio.wait_for(
                dispatch_completion(
                    provider=sniff_provider(model_name),
                    model=normalize_litellm_model(model_name),
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": data_url}},
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                ),
                timeout=60,
            )

            return response.choices[0].message.content or ""  # type: ignore[index, union-attr]
        except Exception:
            logger.warning("MediaProcessor: vision description failed", exc_info=True)
            return ""

    @staticmethod
    def should_use_vision(message_text: str, attachment: dict[str, Any]) -> bool:
        """Determine if vision LLM is needed for an image attachment.

        Returns True when message text alone is insufficient to understand
        the attachment content. This saves cost on bot-generated dashboards
        where the message already contains all the data.
        """
        text = (message_text or "").strip()

        # Very short text — likely just "see attached" or emoji
        if len(text) < 50:
            return True

        # Text explicitly references the attachment
        if _ATTACHMENT_REF_PATTERNS.search(text):
            return True

        # Filename suggests visual content worth describing
        name = attachment.get("name") or ""
        if name and _VISUAL_FILENAME_RE.search(name):
            return True

        # Text has substance — skip vision
        return False
