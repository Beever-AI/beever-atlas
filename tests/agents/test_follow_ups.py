"""Tests for follow_ups_tool bullet-strip guard and plain-string contract."""

from beever_atlas.agents.query.follow_ups_tool import (
    FollowUpsCollector,
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

    def run(collector):
        suggest_follow_ups(["What is X?", "Who owns Y?", "When did Z happen?"])
        results.extend(collector.questions)

    _with_collector(run)
    assert len(results) == 3
    assert results == ["What is X?", "Who owns Y?", "When did Z happen?"]


def test_empty_after_strip_dropped():
    results = []

    def run(collector):
        suggest_follow_ups(["- ", "valid?"])
        results.extend(collector.questions)

    _with_collector(run)
    assert results == ["valid?"]
