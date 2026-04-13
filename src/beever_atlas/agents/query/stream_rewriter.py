"""Rewrites LLM citation tags to user-visible [N] markers at stream time.

The LLM emits opaque tags from tool-result `_cite` fields. The backend
rewrites them before the chunk is emitted as a `response_delta` SSE event.

Supported tag shapes inside a single pair of brackets:
  - Single:    `[src:src_<10hex>]`
  - Inline:    `[src:src_<10hex> inline]`
  - Combined:  `[src:src_aaa, src:src_bbb]` → `[1] [2]`  (each can be inline)

Chunk-safe: if a chunk ends inside `[src:...`, the rewriter buffers until it
can complete the rewrite. A safety cap prevents runaway buffering from a
malformed stream.

Safety net: `flush()` runs a final regex-strip over any leftover
`[src:...]`-looking literals that slipped through (defensive — should be
rare once the combined-tag matcher is in place).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from beever_atlas.agents.citations.registry import SourceRegistry

logger = logging.getLogger(__name__)

# Matches ANY bracket pair `[...]` containing a `src:src_<10hex>` token.
# The content may contain multiple comma-separated tags and arbitrary
# whitespace. We extract individual tags with `_INNER_TAG_RE`.
_SRC_BRACKET_RE = re.compile(r"\[([^\[\]]*?src:src_[a-f0-9]{10}[^\[\]]*?)\]")

# Individual tag within a bracket's content.
_INNER_TAG_RE = re.compile(r"src:(src_[a-f0-9]{10})(\s+inline)?", re.IGNORECASE)

# Safety-net: any leftover `[src:...]` or `[External: ...]` literal that the
# main passes didn't consume. Stripped at flush time.
_LEFTOVER_TAG_RE = re.compile(
    r"\[\s*(?:src:[^\[\]]*?|External:[^\[\]]*?)\]", re.IGNORECASE
)

# Truncated tag openers at the tail of a buffer that must be flushed
# (e.g. cap tripped). These are strictly unclosed; stripping them
# prevents partial `[src:src_abc` or `[12` fragments reaching the client.
_TRUNCATED_SRC_OPENER_RE = re.compile(r"\[\s*src:[^\[\]]*$", re.IGNORECASE)
_TRUNCATED_NUM_OPENER_RE = re.compile(r"\[\d+$")

# Maximum buffered bytes we'll hold waiting for a tag to close. Larger
# than any realistic tag even in combined form (~150 chars for 5 tags).
_MAX_BUFFER = 1024


@dataclass
class StreamRewriter:
    """Stateful text filter that rewrites tags as chunks arrive.

    Usage:
        rw = StreamRewriter(registry)
        for chunk in upstream:
            out = rw.feed(chunk)
            if out: emit(out)
        tail = rw.flush()
        if tail: emit(tail)
    """

    registry: SourceRegistry
    _buffer: str = ""
    _next_marker: int = 1
    _assigned: dict[str, int] = field(default_factory=dict)
    # Observability
    _unknown_tags: int = 0
    _leftover_stripped: int = 0
    _orphan_markers: int = 0

    # ---- public API ----------------------------------------------------

    def feed(self, chunk: str) -> str:
        """Accept a new chunk; return whatever is safe to emit now."""
        if not chunk:
            return ""
        self._buffer += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        """Called once at stream end; emits any remaining buffered text.

        Applies a final strip pass over any leftover `[src:...]` literals
        that slipped through — belt-and-suspenders defense against LLM
        formatting drift.
        """
        out = self._drain(final=True)
        tail = self._buffer
        self._buffer = ""
        combined = out + tail
        cleaned = self._strip_leftovers(combined)
        # Warn once per stream if any observability counters tripped.
        if self._unknown_tags or self._orphan_markers:
            logger.warning(
                "stream_rewriter: flush stats unknown_tags=%d orphan_markers=%d",
                self._unknown_tags,
                self._orphan_markers,
            )
        return cleaned

    def get_stats(self) -> dict[str, int]:
        """Return observability counters for this rewriter instance."""
        return {
            "unknown_tags": self._unknown_tags,
            "orphan_markers": self._orphan_markers,
        }

    # ---- introspection -------------------------------------------------

    @property
    def unknown_tag_count(self) -> int:
        return self._unknown_tags

    @property
    def leftover_stripped_count(self) -> int:
        return self._leftover_stripped

    # ---- internals -----------------------------------------------------

    def _drain(self, *, final: bool) -> str:
        out: list[str] = []
        while self._buffer:
            match = self._find_next_src_bracket()
            if match is not None:
                out.append(self._buffer[: match.start()])
                out.append(self._process_bracket(match.group(1)))
                self._buffer = self._buffer[match.end():]
                continue

            if not final:
                open_idx = self._find_open_tag(self._buffer)
                if open_idx is not None:
                    if len(self._buffer) - open_idx > _MAX_BUFFER:
                        logger.warning(
                            "stream_rewriter: open [src:... exceeded buffer cap; flushing literal"
                        )
                        # Safety: strip any truncated `[src:...` or `[N`
                        # opener at the tail so partial tag openers never
                        # reach the client.
                        out.append(self._sanitize_cap_flush(self._buffer))
                        self._buffer = ""
                        break
                    out.append(self._buffer[:open_idx])
                    self._buffer = self._buffer[open_idx:]
                    break

            out.append(self._buffer)
            self._buffer = ""
            break
        return "".join(out)

    def _find_next_src_bracket(self) -> re.Match[str] | None:
        """Return the next `[...]` in the buffer whose content carries a src tag."""
        return _SRC_BRACKET_RE.search(self._buffer)

    def _process_bracket(self, content: str) -> str:
        """Rewrite every `src:src_<hex>` token inside one bracket to `[N]`.

        A bracket containing three tags (`[src:a, src:b, src:c]`) becomes
        `[1] [2] [3]`. Unknown source_ids are stripped silently and logged.
        """
        parts: list[str] = []
        for match in _INNER_TAG_RE.finditer(content):
            src_id = match.group(1)
            tag_inline = bool(match.group(2))
            rewritten = self._rewrite(src_id, tag_inline)
            if rewritten:
                parts.append(rewritten)
        if not parts:
            # Every inner tag was unknown → strip the whole bracket.
            return ""
        return " ".join(parts)

    def _rewrite(self, source_id: str, inline: bool) -> str:
        marker = self._assigned.get(source_id)
        if marker is None:
            if not self._registry_has(source_id):
                self._unknown_tags += 1
                logger.warning(
                    "stream_rewriter: unknown source tag stripped (source_id=%s)",
                    source_id,
                )
                return ""
            marker = self._next_marker
            self._next_marker += 1
            self._assigned[source_id] = marker
            self.registry.mark_referenced(source_id, marker, inline=inline)
        else:
            # Subsequent reference — propagate inline=True if this one is.
            if inline:
                self.registry.mark_referenced(source_id, marker, inline=True)
        return f"[{marker}]"

    def _registry_has(self, source_id: str) -> bool:
        # Prefer the public API when available; fall back to the internal
        # dict for backwards compatibility.
        has_source = getattr(self.registry, "has_source", None)
        if callable(has_source):
            return bool(has_source(source_id))
        return source_id in self.registry._sources  # noqa: SLF001

    @staticmethod
    def _find_open_tag(buf: str) -> int | None:
        """Return index from which the buffer may contain a partial/unclosed src tag.

        Cases handled:
        1. Unclosed `[` that already has `src:` or a prefix of `src:` after it.
        2. Buffer ends with a prefix of the literal `[src:` (e.g. `[`, `[s`).
        """
        # 1. Unclosed `[...`
        lb = buf.rfind("[")
        while lb != -1:
            rb = buf.find("]", lb)
            if rb == -1:
                after = buf[lb + 1:]
                if after.startswith("src:") or _is_src_prefix(after):
                    return lb
                # Unclosed bracket that's definitely not a src tag; look earlier.
                lb = buf.rfind("[", 0, lb)
                continue
            break  # Found a closed bracket — done scanning open ones.

        # 2. Trailing prefix of `[src:`
        prefix = "[src:"
        for n in range(min(len(buf), len(prefix) - 1), 0, -1):
            if buf.endswith(prefix[:n]):
                return len(buf) - n
        return None

    def _sanitize_cap_flush(self, chunk: str) -> str:
        """Strip any truncated `[src:...` or `[N` opener at the tail.

        Called when `_MAX_BUFFER` is exceeded and we must emit whatever
        is buffered. Without this, partial tag openers like `[src:src_12`
        would leak to the client verbatim.
        """
        cleaned = _TRUNCATED_SRC_OPENER_RE.sub("", chunk)
        cleaned, n = _TRUNCATED_NUM_OPENER_RE.subn("", cleaned)
        if n:
            self._orphan_markers += n
        return cleaned

    def _strip_leftovers(self, text: str) -> str:
        """Defensive: remove any `[src:...]`-looking literal the main passes missed."""
        def _repl(_match: re.Match[str]) -> str:
            self._leftover_stripped += 1
            return ""

        cleaned = _LEFTOVER_TAG_RE.sub(_repl, text)
        if self._leftover_stripped:
            logger.warning(
                "stream_rewriter: stripped %d leftover [src:...] literal(s) at flush",
                self._leftover_stripped,
            )
        return cleaned


def _is_src_prefix(s: str) -> bool:
    """True when `s` is a non-empty prefix of `src:` (e.g. `s`, `sr`, `src`)."""
    target = "src:"
    if not s:
        return False
    for n in range(1, len(target)):
        if s == target[:n]:
            return True
    return False
