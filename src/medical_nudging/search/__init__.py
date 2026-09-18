"""Search backend abstraction for clinical guidelines."""

from medical_nudging.search.backend import SearchBackend, SearchResult
from medical_nudging.search.factory import SearchBackendFactory, get_search_backend

__all__ = [
    "SearchBackend",
    "SearchResult",
    "SearchBackendFactory",
    "get_search_backend",
]
