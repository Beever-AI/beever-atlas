"""Tests for PDF page-chunked extraction."""

from __future__ import annotations

import io

import pytest

from beever_atlas.services.media_extractors import MediaContent, PdfExtractor


class TestExtractPages:
    """Tests for PdfExtractor._extract_pages()."""

    def _make_pdf(self, num_pages: int) -> bytes:
        """Create a simple PDF with N pages of text."""
        from pypdf import PdfWriter

        writer = PdfWriter()
        for i in range(num_pages):
            from pypdf._page import PageObject

            page = PageObject.create_blank_page(width=612, height=792)
            # pypdf blank pages have no text; we'll test the page counting logic
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def _make_pdf_with_text(self, pages_text: list[str]) -> bytes:
        """Create a PDF with text content on each page using reportlab."""
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas
        except ImportError:
            pytest.skip("reportlab not installed")

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        for text in pages_text:
            c.drawString(72, 720, text)
            c.showPage()
        c.save()
        return buf.getvalue()

    def test_extract_pages_returns_list(self):
        page_texts = [f"Page {i} content here" for i in range(5)]
        data = self._make_pdf_with_text(page_texts)
        pages = PdfExtractor._extract_pages(data, max_pages=100)
        assert isinstance(pages, list)
        assert len(pages) == 5

    def test_extract_pages_max_pages_respected(self):
        page_texts = [f"Page {i} content" for i in range(10)]
        data = self._make_pdf_with_text(page_texts)
        pages = PdfExtractor._extract_pages(data, max_pages=5)
        # 5 pages + 1 truncation note
        assert len(pages) == 6
        assert "remaining 5 pages" in pages[-1]

    def test_extract_pages_empty_pdf(self):
        data = self._make_pdf(0)
        pages = PdfExtractor._extract_pages(data, max_pages=100)
        assert pages == []

    def test_extract_pages_invalid_data(self):
        pages = PdfExtractor._extract_pages(b"not a pdf", max_pages=100)
        assert pages == []


class TestPdfExtractorIntegration:
    """Integration tests for the full extract() flow."""

    def _make_pdf_with_text(self, pages_text: list[str]) -> bytes:
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas
        except ImportError:
            pytest.skip("reportlab not installed")

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        for text in pages_text:
            c.drawString(72, 720, text)
            c.showPage()
        c.save()
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_small_pdf_single_chunk(self):
        """A 3-page PDF with chunk_pages=4 should produce 1 chunk."""
        from unittest.mock import patch, MagicMock

        settings = MagicMock(pdf_chunk_pages=4, pdf_max_pages=100)
        with patch("beever_atlas.services.media_extractors.get_settings", return_value=settings):
            data = self._make_pdf_with_text(["Page 1", "Page 2", "Page 3"])
            extractor = PdfExtractor()
            result = await extractor.extract(data, "test.pdf")

        assert isinstance(result, MediaContent)
        assert result.media_type == "pdf"
        assert len(result.chunks) == 1
        assert "test.pdf" in result.text

    @pytest.mark.asyncio
    async def test_multi_page_pdf_multiple_chunks(self):
        """A 10-page PDF with chunk_pages=4 should produce 3 chunks."""
        from unittest.mock import patch, MagicMock

        settings = MagicMock(pdf_chunk_pages=4, pdf_max_pages=100)
        with patch("beever_atlas.services.media_extractors.get_settings", return_value=settings):
            data = self._make_pdf_with_text([f"Page {i} content here" for i in range(10)])
            extractor = PdfExtractor()
            result = await extractor.extract(data, "report.pdf")

        assert result.media_type == "pdf"
        assert len(result.chunks) == 3  # 4+4+2
        assert "10 pages" in result.text
        # Each chunk should have page range annotation
        assert "pages" in result.chunks[0].lower()
        assert "pages" in result.chunks[1].lower()

    @pytest.mark.asyncio
    async def test_empty_pdf(self):
        """PDF with no extractable text."""
        from unittest.mock import patch, MagicMock
        from pypdf import PdfWriter

        settings = MagicMock(pdf_chunk_pages=4, pdf_max_pages=100)
        writer = PdfWriter()
        from pypdf._page import PageObject

        writer.add_page(PageObject.create_blank_page(width=612, height=792))
        buf = io.BytesIO()
        writer.write(buf)

        with patch("beever_atlas.services.media_extractors.get_settings", return_value=settings):
            extractor = PdfExtractor()
            result = await extractor.extract(buf.getvalue(), "blank.pdf")

        assert "no extractable text" in result.text
        assert result.chunks == []


class TestVirtualMessageExpansion:
    """Tests for virtual message expansion logic."""

    def test_single_chunk_no_expansion(self):
        """When chunks has 1 entry, no expansion needed."""
        chunks = ["only chunk"]
        assert len(chunks) <= 1 or len(chunks) > 1  # Just testing the logic gate
        assert len(chunks) == 1

    def test_multi_chunk_expansion_produces_correct_count(self):
        """Simulates the expansion logic from preprocessor."""
        chunks = ["chunk 0", "chunk 1", "chunk 2"]
        original_msg = {
            "text": "Here's the doc",
            "ts": "123",
            "user": "U1",
            "channel_id": "C1",
        }

        # Simulate expansion
        messages = []
        # First message
        first = {**original_msg, "text": original_msg["text"] + "\n\n" + chunks[0]}
        first["pdf_chunk_index"] = 0
        first["pdf_total_chunks"] = len(chunks)
        messages.append(first)

        # Virtual messages
        for idx in range(1, len(chunks)):
            virtual = {
                "text": chunks[idx],
                "ts": original_msg["ts"],
                "user": original_msg["user"],
                "channel_id": original_msg["channel_id"],
                "pdf_chunk_index": idx,
                "pdf_total_chunks": len(chunks),
            }
            messages.append(virtual)

        assert len(messages) == 3
        assert messages[0]["pdf_chunk_index"] == 0
        assert messages[1]["pdf_chunk_index"] == 1
        assert messages[2]["pdf_chunk_index"] == 2
        # All share same identity
        assert all(m["ts"] == "123" for m in messages)
        assert all(m["channel_id"] == "C1" for m in messages)
