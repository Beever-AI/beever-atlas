"""Tests for v9 review follow-ups.

1. Glossary placeholder scrubber removes (Implicit)/(Inferred)/(Unknown) markers.
2. Recent-Activity clusters path includes a Summary aggregate block.
"""

from types import SimpleNamespace

from beever_atlas.wiki.compiler import _activity_fallback, _scrub_glossary_placeholders


def test_scrub_common_placeholders():
    src = (
        "| Term | Definition | First Mentioned By |\n"
        "| AI | ... | (Implicit) |\n"
        "| KG | ... | (Inferred) |\n"
        "| LLM | ... | (Unknown) |\n"
        "| DB | ... | (N/A) |\n"
        "| X | ... | (not specified) |\n"
        "| Y | ... | (TBD) |\n"
    )
    out = _scrub_glossary_placeholders(src)
    assert "(Implicit)" not in out
    assert "(Inferred)" not in out
    assert "(Unknown)" not in out
    assert "(N/A)" not in out
    assert "(not specified)" not in out
    assert "(TBD)" not in out
    assert out.count("—") >= 6


def test_scrub_preserves_other_parentheses():
    src = "Beever Atlas (an omni-modal memory system) is used by (Thomas Chong)."
    out = _scrub_glossary_placeholders(src)
    assert out == src


def test_activity_clusters_path_has_summary():
    clusters = [
        SimpleNamespace(
            title="Alpha",
            member_count=12,
            date_range_end="2026-04-10",
            date_range_start="2026-03-01",
        ),
        SimpleNamespace(
            title="Beta",
            member_count=5,
            date_range_end="2026-03-20",
            date_range_start="2026-02-15",
        ),
    ]
    content, _ = _activity_fallback([], {}, clusters)
    assert "## Summary" in content
    assert "| Topics tracked | 2 |" in content
    assert "| Total memories | 17 |" in content
    assert "2026-02-15 – 2026-04-10" in content
    # Topics table still present
    assert "## Topics with Recent Activity" in content
