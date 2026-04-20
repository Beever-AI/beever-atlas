"""Integration test: adaptive batcher on realistic message samples."""

from beever_atlas.services.adaptive_batcher import estimate_message_tokens, token_aware_batches


def _make_realistic_messages(count: int) -> list[dict]:
    """Generate realistic message samples with varying sizes."""
    messages = []
    for i in range(count):
        # Mix of short messages, medium messages, and messages with media
        if i % 5 == 0:
            # Short greeting
            msg = {
                "text": f"Hey team, quick update #{i}",
                "ts": str(1000 + i),
                "user": f"user_{i % 3}",
            }
        elif i % 5 == 1:
            # Medium technical message
            msg = {
                "text": "I've been looking into the performance issue with the API endpoint. The p99 latency jumped from 50ms to 300ms after the last deploy. I think it's related to the new middleware we added for request validation. "
                * 3,
                "ts": str(1000 + i),
                "user": f"user_{i % 3}",
            }
        elif i % 5 == 2:
            # Message with thread context
            msg = {
                "text": "Agreed, let's rollback the middleware and add proper caching. " * 2,
                "ts": str(1000 + i),
                "user": f"user_{i % 3}",
                "thread_context": "[Reply to user_1: We should investigate the latency spike in the API]",
            }
        elif i % 5 == 3:
            # Message with document digest
            msg = {
                "text": "Here's the incident report [Attachment: report.pdf (PDF, 24 kB, 3 pages)]\n[Document Digest]:\n- Root cause: unindexed MongoDB query in auth middleware\n- Impact: 6x latency increase for 2 hours\n- Resolution: Added compound index on user_id + session_id\n- Action items: Set up query performance monitoring, add index audit to deploy checklist",
                "ts": str(1000 + i),
                "user": f"user_{i % 3}",
            }
        else:
            # Message with links
            msg = {
                "text": f"Check out this PR: https://github.com/org/repo/pull/{i}",
                "ts": str(1000 + i),
                "user": f"user_{i % 3}",
                "source_link_titles": [f"Fix latency issue #{i}"],
                "source_link_descriptions": ["Adds compound index and query optimization"],
            }
        messages.append(msg)
    return messages


class TestAdaptiveBatcherIntegration:
    def test_50_messages_within_budget(self):
        msgs = _make_realistic_messages(50)
        batches = token_aware_batches(msgs, max_tokens=12000)
        # All messages should be distributed across batches
        total = sum(len(b) for b in batches)
        assert total == 50
        # Each batch should be within budget
        for batch in batches:
            tokens = sum(estimate_message_tokens(m) for m in batch)
            # Allow single oversized messages
            if len(batch) > 1:
                assert tokens <= 12000, (
                    f"Batch of {len(batch)} msgs exceeded budget: {tokens} tokens"
                )

    def test_100_messages_no_data_loss(self):
        msgs = _make_realistic_messages(100)
        batches = token_aware_batches(msgs, max_tokens=8000)
        total = sum(len(b) for b in batches)
        assert total == 100

    def test_budget_controls_batch_count(self):
        msgs = _make_realistic_messages(50)
        small_batches = token_aware_batches(msgs, max_tokens=2000)
        large_batches = token_aware_batches(msgs, max_tokens=20000)
        assert len(small_batches) > len(large_batches)
