"""Pre-flight test: decides whether the <<KEY_FACTS_TABLE>> marker survives
`_postprocess_content` so we know whether to splice before or after it.

Per plan lines 267, 282: if the marker is preserved verbatim, splice after
postprocess; otherwise splice before.
"""

from __future__ import annotations

from beever_atlas.wiki.compiler import WikiCompiler


def test_marker_survives_postprocess() -> None:
    raw = "foo\n\n## Overview\n\nsome text\n\n<<KEY_FACTS_TABLE>>\n\n## See Also\n\nbar"
    out = WikiCompiler._postprocess_content(raw)
    assert "<<KEY_FACTS_TABLE>>" in out, (
        f"Marker stripped by _postprocess_content; splice must happen BEFORE postprocess. "
        f"Output: {out!r}"
    )


def test_marker_preserved_inline() -> None:
    raw = "blah <<KEY_FACTS_TABLE>> blah"
    out = WikiCompiler._postprocess_content(raw)
    assert "<<KEY_FACTS_TABLE>>" in out
