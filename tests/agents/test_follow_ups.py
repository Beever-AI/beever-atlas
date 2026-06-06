"""Tests for follow_ups_tool bullet-strip guard and plain-string contract."""

from beever_atlas.agents.query.follow_ups_tool import (
    FollowUpsCollector,
    _clean,
    _current_collector,
    suggest_follow_ups,
)


def _with_collector(fn):
    collector = FollowUpsCollector()
    token = _current_collector.set(collector)
    try:
        fn(collector)
    finally:
        _current_collector.reset(token)


def test_bullets_stripped():
    results = []

    def run(collector):
        suggest_follow_ups(["- foo?", "* bar?", "1. baz?"])
        results.extend(collector.questions)

    _with_collector(run)
    assert results == ["foo?", "bar?", "baz?"]


def test_returns_three_strings():
    results = []

    # Use concrete questions (no X/Y/Z placeholders) so they survive the
    # placeholder filter — this test asserts plain-string passthrough.
    def run(collector):
        suggest_follow_ups(
            [
                "What did the team decide about authentication?",
                "Who owns the Slack integration?",
                "When did the migration ship?",
            ]
        )
        results.extend(collector.questions)

    _with_collector(run)
    assert len(results) == 3
    assert results == [
        "What did the team decide about authentication?",
        "Who owns the Slack integration?",
        "When did the migration ship?",
    ]


def test_empty_after_strip_dropped():
    results = []

    def run(collector):
        suggest_follow_ups(["- ", "valid?"])
        results.extend(collector.questions)

    _with_collector(run)
    assert results == ["valid?"]


def test_placeholder_suggestions_dropped():
    # Templated chips with X/Y placeholders must never reach the renderer.
    assert _clean(["What did we decide about X?", "Who knows about Y?"]) == []


def test_real_question_kept_alongside_placeholder():
    # A concrete question survives while the placeholder chip is dropped.
    assert _clean(["What did we decide about X?", "Who owns the deployment pipeline?"]) == [
        "Who owns the deployment pipeline?"
    ]
