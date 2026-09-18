"""Integration tests for OpenSearch backend.

These tests require:
- OPENSEARCH_ENDPOINT environment variable set
- AWS credentials configured
- Guidelines indexed in OpenSearch

Run with:
    uv run pytest tests/test_opensearch.py -v
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from medical_nudging.search.opensearch_backend import OpenSearchBackend, _retry_with_backoff


# Module-level skip marker for OpenSearch integration tests
opensearch_skip = pytest.mark.skipif(
    not os.environ.get("OPENSEARCH_ENDPOINT"),
    reason="OPENSEARCH_ENDPOINT not set - skipping OpenSearch integration tests",
)


@pytest.fixture
def backend():
    """Create OpenSearch backend instance."""
    return OpenSearchBackend()


@opensearch_skip
class TestOpenSearchBackend:
    """Integration tests for OpenSearch backend."""

    def test_is_available(self, backend):
        """Test that backend reports availability when endpoint is configured."""
        # If we got here, OPENSEARCH_ENDPOINT is set
        available = backend.is_available()
        assert isinstance(available, bool)
        # We expect it to be available if tests are running
        assert available is True

    def test_health_check_returns_dict(self, backend):
        """Test health check returns a valid response dict."""
        health = backend.health_check()

        assert isinstance(health, dict)
        assert "healthy" in health

        if health["healthy"]:
            assert "latency_ms" in health
            assert "index_doc_count" in health
            assert "index_exists" in health
            assert "index_name" in health
            assert health["latency_ms"] >= 0
            assert health["index_doc_count"] >= 0
        else:
            assert "error" in health

    def test_health_check_latency_reasonable(self, backend):
        """Test health check completes in reasonable time."""
        health = backend.health_check()

        if health["healthy"]:
            # Health check should complete within 5 seconds
            assert health["latency_ms"] < 5000

    def test_search_returns_list(self, backend):
        """Test basic search returns a list of results."""
        results = backend.search("diabetes", limit=5)

        assert isinstance(results, list)
        # Results may be empty if no guidelines indexed

    def test_search_with_source_filter(self, backend):
        """Test search with source filter."""
        # Search with a source filter
        filtered_results = backend.search("target", sources=["ADA"], limit=10)

        assert isinstance(filtered_results, list)
        # Filtered results should be subset (or empty if no ADA docs)
        for result in filtered_results:
            # Source should contain ADA if any results returned
            if filtered_results:
                # At least verify results have source attribute
                assert hasattr(result, "source")

    def test_search_empty_query(self, backend):
        """Test search handles empty query gracefully."""
        results = backend.search("", limit=5)
        assert isinstance(results, list)

    def test_search_result_structure(self, backend):
        """Test search results have expected structure."""
        results = backend.search("diabetes HbA1c", limit=3)

        for result in results:
            assert hasattr(result, "source")
            assert hasattr(result, "content")
            assert hasattr(result, "relevance")
            # Relevance should be normalized 0-1
            assert 0 <= result.relevance <= 1

    def test_search_limit_respected(self, backend):
        """Test search respects limit parameter."""
        results_3 = backend.search("treatment", limit=3)
        results_10 = backend.search("treatment", limit=10)

        assert len(results_3) <= 3
        assert len(results_10) <= 10


class TestRetryDecorator:
    """Unit tests for the retry decorator (no OpenSearch needed)."""

    def test_retry_on_transient_error(self):
        """Test that decorator retries on transient errors."""
        call_count = 0

        @_retry_with_backoff(max_retries=3, base_delay=0.01)
        def flaky_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Connection timed out")
            return "success"

        result = flaky_function()
        assert result == "success"
        assert call_count == 3  # Should have retried twice

    def test_no_retry_on_non_transient_error(self):
        """Test that decorator doesn't retry on non-transient errors."""
        call_count = 0

        @_retry_with_backoff(max_retries=3, base_delay=0.01)
        def error_function():
            nonlocal call_count
            call_count += 1
            raise ValueError("Invalid argument")

        with pytest.raises(ValueError):
            error_function()

        # Should not retry - only one call
        assert call_count == 1

    def test_max_retries_exceeded(self):
        """Test that decorator raises after max retries."""
        call_count = 0

        @_retry_with_backoff(max_retries=3, base_delay=0.01)
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Connection refused")

        with pytest.raises(ConnectionError):
            always_fails()

        # Should have tried 3 times
        assert call_count == 3

    def test_exponential_backoff_timing(self):
        """Test that backoff delays increase exponentially."""
        call_times = []

        @_retry_with_backoff(max_retries=3, base_delay=0.1)
        def timing_function():
            call_times.append(time.time())
            if len(call_times) < 3:
                raise ConnectionError("timeout")
            return "done"

        timing_function()

        assert len(call_times) == 3
        # First delay should be ~0.1s, second ~0.2s
        delay1 = call_times[1] - call_times[0]
        delay2 = call_times[2] - call_times[1]
        # Allow some tolerance for timing
        assert 0.05 < delay1 < 0.3  # ~0.1s
        assert 0.1 < delay2 < 0.5  # ~0.2s


class TestIndexIsolation:
    """Unit tests for deterministic aggregate-index selection."""

    @patch("medical_nudging.search.opensearch_backend._get_client")
    @patch("medical_nudging.search.opensearch_backend.get_opensearch_config")
    def test_exact_configured_index_precedes_guidelines_wildcard(
        self,
        mock_config,
        mock_get_client,
    ):
        mock_config.return_value = {"opensearch_index": "guidelines-icu-baseline-v2"}
        client = MagicMock()
        client.indices.exists.return_value = True
        mock_get_client.return_value = client

        backend = OpenSearchBackend()

        assert backend._resolve_index_pattern() == "guidelines-icu-baseline-v2"
        assert backend._resolve_index_pattern(["SCCM_PADIS_2018"]) == ("guidelines-icu-baseline-v2")
        client.indices.get.assert_not_called()

    @patch("medical_nudging.search.opensearch_backend._get_client")
    @patch("medical_nudging.search.opensearch_backend.get_opensearch_config")
    def test_search_excludes_reference_headings(self, mock_config, mock_get_client):
        mock_config.return_value = {"opensearch_index": "guidelines-icu-baseline-v2"}
        client = MagicMock()
        client.indices.exists.return_value = True
        client.search.return_value = {"hits": {"max_score": 0.0, "hits": []}}
        mock_get_client.return_value = client

        OpenSearchBackend().search("sepsis", limit=3)

        body = client.search.call_args.kwargs["body"]
        assert body["query"]["bool"]["must_not"] == [
            {"wildcard": {"hierarchy": {"value": "reference*", "case_insensitive": True}}},
            {"wildcard": {"hierarchy": {"value": "bibliograph*", "case_insensitive": True}}},
        ]
