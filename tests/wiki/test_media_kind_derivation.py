"""Test that _build_media_data and _assemble_resources_markdown handle
media items where the upstream persister did not record a specific
type — common for Mattermost/Slack file URLs that are opaque IDs with
no extension on the URL itself.

Without correct kind inference, the planner buckets these as "file"
and the frontend renders them as plain text links instead of
``<img>``/``<video>``/PDF preview cards.
"""

from __future__ import annotations

from beever_atlas.models.domain import AtomicFact
from beever_atlas.wiki.compiler import (
    _assemble_resources_markdown,
    _build_media_data,
    _derive_media_kind,
)


class TestDeriveMediaKind:
    """``_derive_media_kind(url, name, fallback)`` — canonical kind picker."""

    def test_specific_fallback_wins(self) -> None:
        # Persister already gave us "image"; trust it.
        assert _derive_media_kind("https://opaque.example/x", "x", "image") == "image"
        assert _derive_media_kind("https://opaque.example/x", "x", "video") == "video"
        assert _derive_media_kind("https://opaque.example/x", "x", "pdf") == "pdf"

    def test_doc_normalized_to_document(self) -> None:
        assert _derive_media_kind("u", "n", "doc") == "document"

    def test_image_inferred_from_filename(self) -> None:
        # Mattermost-style opaque URL but the filename gives the kind.
        assert (
            _derive_media_kind(
                "https://team.example.com/api/v4/files/abc123",
                "logo.png",
                "file",
            )
            == "image"
        )
        assert _derive_media_kind("u", "screenshot.JPG", "") == "image"

    def test_video_inferred_from_filename(self) -> None:
        assert _derive_media_kind("u", "demo.mp4", "file") == "video"
        assert _derive_media_kind("u", "Recording.MOV", "") == "video"

    def test_pdf_inferred_from_filename(self) -> None:
        assert _derive_media_kind("u", "report.pdf", "file") == "pdf"

    def test_document_inferred_from_filename(self) -> None:
        assert _derive_media_kind("u", "spec.docx", "file") == "document"
        assert _derive_media_kind("u", "data.csv", "") == "document"

    def test_video_inferred_from_url_host(self) -> None:
        assert _derive_media_kind("https://www.youtube.com/watch?v=abc", "", "") == "video"
        assert _derive_media_kind("https://youtu.be/abc", "", "link") == "video"
        assert _derive_media_kind("https://vimeo.com/12345", "", "") == "video"
        assert _derive_media_kind("https://www.loom.com/share/abc", "", "") == "video"

    def test_unknown_falls_back_to_file(self) -> None:
        # Opaque URL, no filename, generic fallback — best we can do.
        assert (
            _derive_media_kind(
                "https://team.example.com/api/v4/files/abc123",
                "",
                "file",
            )
            == "file"
        )


def _make_fact(
    *,
    media_urls: list[str] | None = None,
    media_names: list[str] | None = None,
    media_type: str = "",
    link_urls: list[str] | None = None,
    link_titles: list[str] | None = None,
    text: str = "context",
) -> AtomicFact:
    return AtomicFact(
        memory_text=text,
        author_name="Alan",
        cluster_id="c1",
        source_media_urls=media_urls or [],
        source_media_names=media_names or [],
        source_media_type=media_type,
        source_link_urls=link_urls or [],
        source_link_titles=link_titles or [],
    )


class TestBuildMediaData:
    """``_build_media_data`` populates both ``type`` and ``kind`` so the
    legacy ``_assemble_resources_markdown`` (reads ``type``) and the new
    modules orchestrator (reads ``kind`` first, then ``type``) agree."""

    def test_image_kind_populated_for_mattermost_url(self) -> None:
        fact = _make_fact(
            media_urls=["https://team.example.com/api/v4/files/abc"],
            media_names=["beever_logo.svg"],
            media_type="",  # persister had no MIME
        )
        out = _build_media_data([fact])
        assert len(out) == 1
        assert out[0]["type"] == "image"
        assert out[0]["kind"] == "image"
        assert out[0]["name"] == "beever_logo.svg"

    def test_video_kind_populated_for_mp4(self) -> None:
        fact = _make_fact(
            media_urls=["https://team.example.com/api/v4/files/xyz"],
            media_names=["wiki-fast.mp4"],
            media_type="",
        )
        out = _build_media_data([fact])
        assert out[0]["kind"] == "video"

    def test_video_link_promoted_from_youtube_url(self) -> None:
        # A YouTube URL recorded as a "link" should still be classified
        # as video so the VideoEmbedModule picks it up.
        fact = _make_fact(link_urls=["https://www.youtube.com/watch?v=abc"])
        out = _build_media_data([fact])
        assert out[0]["kind"] == "video"
        assert out[0]["type"] == "video"

    def test_plain_link_stays_link(self) -> None:
        fact = _make_fact(link_urls=["https://github.com/foo/bar"])
        out = _build_media_data([fact])
        assert out[0]["kind"] == "link"

    def test_existing_specific_type_is_preserved(self) -> None:
        fact = _make_fact(
            media_urls=["https://opaque.example/abc"],
            media_names=["x"],
            media_type="image",
        )
        out = _build_media_data([fact])
        assert out[0]["kind"] == "image"


class TestResourcesMarkdownHints:
    """The legacy ``_assemble_resources_markdown`` produces flat markdown
    that React renders. Frontend ``detectMediaType`` infers the media
    kind from URL extension OR alt-text substring — when the URL is
    opaque (Mattermost ``/api/v4/files/<id>``), the alt text is the
    only hint available."""

    def test_pdf_link_text_carries_pdf_marker(self) -> None:
        media = [
            {
                "url": "https://team.example.com/api/v4/files/pdf-id",
                "type": "pdf",
                "kind": "pdf",
                "name": "spec.pdf",
                "author": "Alan",
                "context": "shared the spec",
            }
        ]
        md = _assemble_resources_markdown(media)
        # WikiMarkdown::detectMediaType triggers WikiPdfLink when the
        # link text starts with 📄 OR contains "pdf".
        assert "📄" in md
        assert "PDF" in md
        # The plain ``[Download]`` text we used to emit is gone — that
        # was what caused the renderer to fall back to a generic link.
        assert "[Download]" not in md

    def test_video_link_text_carries_video_marker(self) -> None:
        media = [
            {
                "url": "https://team.example.com/api/v4/files/vid-id",
                "type": "video",
                "kind": "video",
                "name": "wiki-fast.mp4",
                "author": "Alan",
                "context": "demo of the wiki sync",
            }
        ]
        md = _assemble_resources_markdown(media)
        # detectMediaType returns "video" when the alt contains "video"
        # OR starts with 🎥 — both are present here.
        assert "🎥" in md
        assert "video" in md.lower()
        assert "[Watch]" not in md
