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
from typing import Any

import httpx

from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)

# Patterns that suggest the user is referencing an attachment
_ATTACHMENT_REF_PATTERNS = re.compile(
    r"see attached|check this|look at this|attached|screenshot|see above|here'?s the",
    re.IGNORECASE,
)

# Maximum characters of extracted PDF text to include
_MAX_PDF_TEXT_CHARS = 2000


class MediaProcessor:
    """Download, extract, and describe media attachments from Slack messages."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._sem = asyncio.Semaphore(3)
        self._supported_images = set(
            self._settings.media_supported_image_types.split(",")
        )
        self._supported_docs = set(
            self._settings.media_supported_doc_types.split(",")
        )
        self._max_bytes = self._settings.media_max_file_size_mb * 1024 * 1024

    # ── Public API ──────────────────────────────────────────────────────

    async def process_message_media(
        self, msg: dict[str, Any]
    ) -> dict[str, Any]:
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
        descriptions: list[str] = []
        media_urls: list[str] = []
        media_type = ""

        timeout = self._settings.media_vision_timeout_seconds

        for att in all_media:
            try:
                result = await asyncio.wait_for(
                    self._process_attachment(att, message_text),
                    timeout=timeout,
                )
                if result["description"]:
                    descriptions.append(result["description"])
                if result["media_url"]:
                    media_urls.append(result["media_url"])
                if result["media_type"] and not media_type:
                    media_type = result["media_type"]
            except asyncio.TimeoutError:
                name = att.get("name", "unknown")
                logger.warning(
                    "MediaProcessor: timeout processing attachment %s (limit=%ds)",
                    name,
                    timeout,
                )
            except Exception:
                name = att.get("name", "unknown")
                logger.warning(
                    "MediaProcessor: failed to process attachment %s",
                    name,
                    exc_info=True,
                )

        return {
            "description": "\n\n".join(descriptions),
            "media_urls": media_urls,
            "media_type": media_type,
        }

    # ── Internal methods ────────────────────────────────────────────────

    async def _process_attachment(
        self, att: dict[str, Any], message_text: str
    ) -> dict[str, str]:
        """Route a single attachment to the appropriate handler."""
        url = att.get("url") or att.get("url_private") or ""
        name = att.get("name") or "file"
        att_type = att.get("type") or ""
        mimetype = att.get("mimetype") or ""

        # Determine file extension
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        is_image = (
            att_type == "image"
            or ext in self._supported_images
            or mimetype.startswith("image/")
        )
        is_pdf = ext in self._supported_docs or mimetype == "application/pdf"

        if not url:
            return {"description": "", "media_url": "", "media_type": ""}

        if is_pdf:
            return await self._handle_pdf(url, name)
        elif is_image:
            return await self._handle_image(url, name, message_text)
        else:
            # Unsupported type — return metadata only
            return {
                "description": f"[Attachment: {name} ({att_type or ext})]",
                "media_url": url,
                "media_type": att_type or ext,
            }

    async def _handle_pdf(
        self, url: str, name: str
    ) -> dict[str, str]:
        """Download and extract text from a PDF."""
        async with self._sem:
            data = await self._download_file(url)

        if not data:
            return {"description": "", "media_url": url, "media_type": "pdf"}

        text = self._extract_pdf_text(data)
        size_kb = len(data) // 1024

        if text.strip():
            desc = (
                f"[Attachment: {name} (PDF, {size_kb} kB)]\n"
                f"[Document text: {text[:_MAX_PDF_TEXT_CHARS]}]"
            )
        else:
            desc = f"[Attachment: {name} (PDF, {size_kb} kB, no extractable text)]"

        return {"description": desc, "media_url": url, "media_type": "pdf"}

    async def _handle_image(
        self, url: str, name: str, message_text: str
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

        description = await self._describe_image(data, message_text)
        size_kb = len(data) // 1024

        if description:
            desc = (
                f"[Attachment: {name} (image, {size_kb} kB)]\n"
                f"[Image description: {description}]"
            )
        else:
            desc = f"[Attachment: {name} (image, {size_kb} kB)]"

        return {"description": desc, "media_url": url, "media_type": "image"}

    async def _download_file(self, url: str) -> bytes | None:
        """Download a file via the bridge file proxy."""
        settings = self._settings
        proxy_url = f"{settings.bridge_url}/bridge/files?url={url}"
        headers: dict[str, str] = {}
        if settings.bridge_api_key:
            headers["Authorization"] = f"Bearer {settings.bridge_api_key}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(proxy_url, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        "MediaProcessor: download failed status=%d url=%s",
                        resp.status_code,
                        url[:80],
                    )
                    return None

                if len(resp.content) > self._max_bytes:
                    logger.info(
                        "MediaProcessor: skipping file >%dMB: %s",
                        settings.media_max_file_size_mb,
                        url[:80],
                    )
                    return None

                return resp.content
        except Exception:
            logger.warning(
                "MediaProcessor: download error url=%s", url[:80], exc_info=True
            )
            return None

    def _extract_pdf_text(self, data: bytes) -> str:
        """Extract text from PDF bytes using pypdf."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages: list[str] = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text.strip())
            return "\n\n".join(pages)
        except Exception:
            logger.warning("MediaProcessor: PDF text extraction failed", exc_info=True)
            return ""

    async def _describe_image(
        self, data: bytes, message_context: str
    ) -> str:
        """Describe an image using Gemini vision API."""
        try:
            from google import genai
            from google.genai import types as genai_types

            client = genai.Client(api_key=self._settings.google_api_key)

            prompt = (
                "Describe this image concisely for a knowledge extraction system. "
                "Focus on: key data points, text visible in the image, chart/graph values, "
                "names, dates, and any actionable information. "
                "Keep the description under 200 words."
            )
            if message_context:
                prompt += f"\n\nMessage context: {message_context[:200]}"

            response = await client.aio.models.generate_content(
                model=self._settings.media_vision_model,
                contents=[
                    genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part.from_bytes(
                                data=data,
                                mime_type="image/png",
                            ),
                            genai_types.Part.from_text(text=prompt),
                        ],
                    )
                ],
            )

            return response.text or ""
        except Exception:
            logger.warning(
                "MediaProcessor: vision description failed", exc_info=True
            )
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
        if len(text) < 20:
            return True

        # Text explicitly references the attachment
        if _ATTACHMENT_REF_PATTERNS.search(text):
            return True

        # Text has substance — skip vision
        return False
