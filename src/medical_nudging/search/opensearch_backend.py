"""OpenSearch Serverless search backend."""

import logging
import os
import re
import time
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from medical_nudging.config import aws_region_from_endpoint, get_aws_config, get_opensearch_config
from medical_nudging.search.backend import SearchBackend, SearchResult

logger = logging.getLogger(__name__)

T = TypeVar("T")

REFERENCE_SECTION_EXCLUSIONS = [
    {"wildcard": {"hierarchy": {"value": "reference*", "case_insensitive": True}}},
    {"wildcard": {"hierarchy": {"value": "bibliograph*", "case_insensitive": True}}},
]


def _retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 0.5,
    retryable_errors: tuple[str, ...] = ("timeout", "429", "503", "connection", "timed out"),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for retry with exponential backoff on transient errors.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Base delay in seconds (doubles each retry).
        retryable_errors: Substrings in error messages that indicate transient failures.

    Returns:
        Decorated function with retry logic.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Exception | None = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    error_str = str(e).lower()
                    # Only retry on transient errors
                    if any(err in error_str for err in retryable_errors):
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            f"OpenSearch retry {attempt + 1}/{max_retries}, waiting {delay:.1f}s: {e}"
                        )
                        time.sleep(delay)
                    else:
                        raise  # Non-transient error, don't retry
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry loop exit")  # Should never reach

        return wrapper

    return decorator


_opensearch_client = None


def _get_client():
    """Get or create OpenSearch client with AWS SigV4 auth."""
    global _opensearch_client
    if _opensearch_client is not None:
        return _opensearch_client

    opensearch_config = get_opensearch_config()
    endpoint = opensearch_config.get("opensearch_endpoint")
    if not endpoint:
        return None

    try:
        aws_config = get_aws_config()
        # The collection's region is in its hostname; AWS_REGION is only a fallback.
        region = aws_region_from_endpoint(endpoint) or aws_config.get("aws_region", "us-east-1")
        profile = aws_config.get("aws_profile")

        # If explicit credentials are in the environment, skip profile_name
        # so boto3's default credential chain picks them up
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            profile = None

        credentials = boto3.Session(profile_name=profile).get_credentials()
    except Exception as e:
        logger.warning(f"Failed to get AWS credentials: {e}")
        return None

    auth = AWSV4SignerAuth(credentials, region, "aoss")

    # Remove https:// prefix if present for host
    host = endpoint.replace("https://", "").replace("http://", "")

    _opensearch_client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )
    return _opensearch_client


class OpenSearchBackend(SearchBackend):
    """OpenSearch Serverless backend using BM25 search."""

    def __init__(self, index_name: str = "guidelines"):
        opensearch_config = get_opensearch_config()
        self._index_name = opensearch_config.get("opensearch_index", index_name)

    @property
    def name(self) -> str:
        return "opensearch"

    def is_available(self) -> bool:
        client = _get_client()
        if client is None:
            return False
        try:
            client.indices.exists(index=self._index_name)
            return True
        except Exception as e:
            logger.warning(f"OpenSearch not available: {e}")
            return False

    def health_check(self) -> dict[str, Any]:
        """Perform comprehensive health check on OpenSearch connection.

        Returns:
            Dict with keys:
                - healthy (bool): Whether the cluster is healthy
                - latency_ms (float): Round-trip latency in milliseconds
                - index_doc_count (int): Number of documents in the index
                - index_exists (bool): Whether the index exists
                - error (str, optional): Error message if check failed
        """
        client = _get_client()
        if client is None:
            return {
                "healthy": False,
                "error": "Client not initialized - check opensearch_endpoint in settings.yaml",
            }

        try:
            start = time.perf_counter()

            # Check index exists and get doc count (Serverless doesn't support info())
            doc_count = 0
            index_exists = client.indices.exists(index=self._index_name)
            latency_ms = (time.perf_counter() - start) * 1000

            if index_exists:
                count_response = client.count(index=self._index_name)
                doc_count = count_response.get("count", 0)

            return {
                "healthy": True,
                "latency_ms": round(latency_ms, 2),
                "index_doc_count": doc_count,
                "index_exists": index_exists,
                "index_name": self._index_name,
                "cluster_name": "serverless",
                "version": "serverless",
            }
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    @staticmethod
    def _slugify(source: str) -> str:
        """Convert source name to index slug.

        Examples:
            "ADA 2026" -> "ada"
            "AHA_ACC 2025" -> "aha-acc"
            "CDC 2024" -> "cdc"
        """
        # Take the org prefix (first token), lowercase, replace underscores with hyphens
        org = source.split()[0].lower()
        return re.sub(r"[^a-z0-9]+", "-", org).strip("-")

    def _resolve_index_pattern(self, sources: Optional[list[str]] = None) -> str:
        """Determine the index pattern to query.

        Prefer the exact configured index. This keeps experiment corpora isolated
        even when multiple ``guidelines-*`` aggregate indices coexist.
        """
        client = _get_client()
        if client is not None:
            try:
                if client.indices.exists(index=self._index_name):
                    return self._index_name
            except Exception:
                pass

        if sources:
            indices = [f"guidelines-{self._slugify(s)}" for s in sources]
            return ",".join(indices)

        if client is not None:
            try:
                existing = client.indices.get("guidelines-*")
                if existing:
                    return "guidelines-*"
            except Exception:
                pass

        return self._index_name

    @_retry_with_backoff(max_retries=3, base_delay=0.5)
    def search(
        self,
        query: str,
        sources: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        client = _get_client()
        if client is None:
            return []

        index_pattern = self._resolve_index_pattern(sources)

        # Build query
        must_clauses = [{"match": {"content": {"query": query, "fuzziness": "AUTO"}}}]

        if sources:
            must_clauses.append({"terms": {"source": sources}})

        body = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "must_not": REFERENCE_SECTION_EXCLUSIONS,
                }
            },
            "size": limit,
        }

        try:
            response = client.search(index=index_pattern, body=body)
            results = []
            max_score = response["hits"].get("max_score", 1.0) or 1.0

            for hit in response["hits"]["hits"]:
                src = hit["_source"]
                content = src.get("content", "")

                results.append(
                    SearchResult(
                        source=src.get("source", ""),
                        section=src.get("section"),
                        section_number=src.get("section_number"),
                        page_number=src.get("page_number"),
                        content=content,
                        relevance=hit["_score"] / max_score,
                    )
                )
            return results
        except Exception as e:
            logger.error(f"OpenSearch search failed: {e}")
            return []

    def list_sources(self) -> list[dict]:
        """List unique sources with document counts across all guideline indices."""
        client = _get_client()
        if client is None:
            return []

        index_pattern = self._resolve_index_pattern()

        body = {
            "size": 0,
            "aggs": {"sources": {"terms": {"field": "source", "size": 100}}},
        }

        try:
            response = client.search(index=index_pattern, body=body)
            return [
                {"source": bucket["key"], "doc_count": bucket["doc_count"]}
                for bucket in response["aggregations"]["sources"]["buckets"]
            ]
        except Exception as e:
            logger.error(f"OpenSearch list_sources failed: {e}")
            return []

    def list_indices(self) -> list[str]:
        """List all guideline indices."""
        client = _get_client()
        if client is None:
            return []

        try:
            existing = client.indices.get("guidelines-*")
            return sorted(existing.keys())
        except Exception as e:
            logger.debug(f"No per-source indices found: {e}")
            # Fall back to checking the single configured index
            try:
                if client.indices.exists(index=self._index_name):
                    return [self._index_name]
            except Exception:
                pass
            return []
