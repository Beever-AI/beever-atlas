"""Tests for WikiCompiler post-processing and resource filtering."""

from beever_atlas.wiki.compiler import WikiCompiler


class TestPostprocessContent:
    """Tests for WikiCompiler._postprocess_content."""

    def test_strips_terminal_sources_h3(self):
        content = "## Key Facts\nSome content\n\n### Sources\n\n- [1] @Author · ts — text [link](link)\n- [2] @Author2"
        result = WikiCompiler._postprocess_content(content)
        assert "### Sources" not in result
        assert "## Key Facts" in result
        assert "Some content" in result

    def test_strips_terminal_sources_h2(self):
        content = "## Overview\nGood stuff\n\n## Sources\n\n- [1] citation"
        result = WikiCompiler._postprocess_content(content)
        assert "## Sources" not in result
        assert "## Overview" in result

    def test_preserves_data_sources_heading(self):
        content = "## Data Sources\nThese are our data sources.\n\n## Overview\nMore content."
        result = WikiCompiler._postprocess_content(content)
        assert "## Data Sources" in result
        assert "## Overview" in result

    def test_cleans_mermaid_edge_labels(self):
        content = "```mermaid\ngraph TD\n    A[Foo] -- explores --> B[Bar]\n    C[Baz] --> D[Qux]\n```"
        result = WikiCompiler._postprocess_content(content)
        assert "-- explores -->" not in result
        assert "A[Foo] -->|explores| B[Bar]" in result
        assert "C[Baz] --> D[Qux]" in result

    def test_removes_subgraph_and_style_lines(self):
        content = "```mermaid\ngraph TD\n    subgraph cluster1\n    A[X] --> B[Y]\n    end\n    style A fill:#f00\n    classDef default fill:#fff\n    class A default\n```"
        result = WikiCompiler._postprocess_content(content)
        assert "subgraph" not in result
        assert "end" not in result
        assert "style A" not in result
        assert "classDef" not in result
        assert "class A" not in result
        assert "A[X] --> B[Y]" in result

    def test_collapses_blank_lines(self):
        content = "Section 1\n\n\n\n\n\nSection 2"
        result = WikiCompiler._postprocess_content(content)
        assert "\n\n\n\n" not in result
        assert "Section 1" in result
        assert "Section 2" in result

    def test_passthrough_clean_content(self):
        content = "## Overview\n\nClean content with no issues.\n\n## Details\n\nMore clean content."
        result = WikiCompiler._postprocess_content(content)
        assert "## Overview" in result
        assert "## Details" in result
        assert "Clean content" in result

    def test_empty_content(self):
        assert WikiCompiler._postprocess_content("") == ""


class TestFilterMediaForResources:
    """Tests for WikiCompiler._filter_media_for_resources."""

    def test_filters_shortener_urls(self):
        media = [
            {"url": "https://t.co/abc123", "name": "Shortened Link", "type": "link", "author": "A", "context": "x"},
            {"url": "https://bit.ly/xyz", "name": "Short", "type": "link", "author": "A", "context": "x"},
            {"url": "https://github.com/foo/bar", "name": "Repo", "type": "link", "author": "A", "context": "x"},
        ]
        result = WikiCompiler._filter_media_for_resources(media)
        urls = [m["url"] for m in result]
        assert "https://t.co/abc123" not in urls
        assert "https://bit.ly/xyz" not in urls
        assert "https://github.com/foo/bar" in urls

    def test_filters_generic_names(self):
        media = [
            {"url": "https://files.slack.com/image1", "name": "image.png", "type": "image", "author": "A", "context": "x"},
            {"url": "https://files.slack.com/image2", "name": "download", "type": "file", "author": "A", "context": "x"},
            {"url": "https://files.slack.com/image3", "name": "Architecture Diagram", "type": "image", "author": "A", "context": "x"},
        ]
        result = WikiCompiler._filter_media_for_resources(media)
        names = [m["name"] for m in result]
        assert "image.png" not in names
        assert "download" not in names
        assert "Architecture Diagram" in names

    def test_domain_cap(self):
        media = [
            {"url": f"https://linkedin.com/post/{i}", "name": f"Post {i}", "type": "link", "author": "A", "context": "x"}
            for i in range(12)
        ]
        result = WikiCompiler._filter_media_for_resources(media)
        assert len(result) == 5  # Default cap of 5 per domain

    def test_github_higher_cap(self):
        media = [
            {"url": f"https://github.com/repo/{i}", "name": f"Repo {i}", "type": "link", "author": "A", "context": "x"}
            for i in range(12)
        ]
        result = WikiCompiler._filter_media_for_resources(media)
        assert len(result) == 10  # GitHub cap of 10

    def test_total_cap(self):
        media = []
        for domain_i in range(10):
            for item_i in range(4):
                media.append({
                    "url": f"https://domain{domain_i}.com/{item_i}",
                    "name": f"Item {domain_i}-{item_i}",
                    "type": "link", "author": "A", "context": "x",
                })
        result = WikiCompiler._filter_media_for_resources(media)
        assert len(result) <= 30

    def test_empty_input(self):
        assert WikiCompiler._filter_media_for_resources([]) == []


class TestCleanMermaid:
    """Tests for mermaid sanitization in WikiCompiler._postprocess_content."""

    def _wrap(self, body: str) -> str:
        return f"```mermaid\n{body}\n```"

    def test_strips_parens_in_node_label(self):
        content = self._wrap("graph TD\n    A[Foo (Bar)] --> B[Baz]")
        result = WikiCompiler._postprocess_content(content)
        assert "(" not in result
        assert ")" not in result
        # Label content preserved (without parens)
        assert "Foo" in result
        assert "Bar" in result

    def test_strips_quotes_in_node_label(self):
        content = self._wrap('graph TD\n    A["Quoted Label"] --> B[Normal]')
        result = WikiCompiler._postprocess_content(content)
        assert '"' not in result.split("```mermaid")[1].split("```")[0]

    def test_empty_brackets_dropped(self):
        content = self._wrap("graph TD\n    A[] --> B[Valid]")
        result = WikiCompiler._postprocess_content(content)
        # The empty-bracket node line should be removed
        assert "A[]" not in result

    def test_empty_label_falls_back_to_node_id(self):
        # A node whose label becomes empty after stripping forbidden chars
        content = self._wrap('graph TD\n    A["()"] --> B[Target]')
        result = WikiCompiler._postprocess_content(content)
        # Should not have empty brackets
        assert "A[]" not in result

    def test_colon_label_stripped(self):
        content = self._wrap("graph TD\n    A[Source] --> B[Dest]: some free text")
        result = WikiCompiler._postprocess_content(content)
        # Colon label removed
        assert ": some free text" not in result
        # Nodes preserved
        assert "A[Source]" in result or "Source" in result

    def test_colon_label_not_strip_pipe_labels(self):
        content = self._wrap("graph TD\n    A[Source] -->|valid label| B[Dest]")
        result = WikiCompiler._postprocess_content(content)
        assert "|valid label|" in result

    def test_no_empty_brackets_in_output(self):
        content = self._wrap("graph TD\n    A[()] --> B[Hello]")
        result = WikiCompiler._postprocess_content(content)
        mermaid_body = result.split("```mermaid")[1].split("```")[0]
        import re
        # No node should have empty brackets after sanitization
        assert not re.search(r"\w+\[\s*\]", mermaid_body)
