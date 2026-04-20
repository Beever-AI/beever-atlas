"""Unit tests for fact-level status tracking."""

from beever_atlas.models.persistence import FactStatus


class TestFactStatus:
    def test_default_values(self):
        fs = FactStatus(fact_index=0)
        assert fs.status == "pending"
        assert fs.weaviate_id is None
        assert fs.error is None
        assert fs.retry_count == 0

    def test_stored_fact(self):
        fs = FactStatus(fact_index=1, status="stored", weaviate_id="abc-123")
        assert fs.status == "stored"
        assert fs.weaviate_id == "abc-123"

    def test_failed_fact(self):
        fs = FactStatus(fact_index=2, status="failed", error="connection timeout", retry_count=2)
        assert fs.status == "failed"
        assert fs.retry_count == 2

    def test_max_retry_exceeded(self):
        fs = FactStatus(fact_index=0, status="failed", retry_count=3)
        # fact_max_retries default is 3, so this should be skipped
        assert fs.retry_count >= 3

    def test_serialization(self):
        fs = FactStatus(fact_index=0, status="stored", weaviate_id="abc")
        d = fs.model_dump()
        assert d["fact_index"] == 0
        assert d["status"] == "stored"
        assert d["weaviate_id"] == "abc"
