"""Search backend protocol and result dataclass."""

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class SearchResult:
    """Unified search result across all backends."""

    source: str
    section: Optional[str]
    section_number: Optional[str]
    page_number: Optional[int]
    content: str
    relevance: float


class SearchBackend(Protocol):
    """Protocol for search backend implementations."""

    def search(
        self,
        query: str,
        sources: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search guidelines and return results."""
        ...

    def is_available(self) -> bool:
        """Check if this backend is available."""
        ...

    def list_sources(self) -> list[dict]:
        """List unique sources with document counts.

        Returns:
            List of dicts with 'source' and 'doc_count' keys.
        """
        ...

    def list_indices(self) -> list[str]:
        """List available guideline indices.

        Returns:
            List of index names.
        """
        ...

    @property
    def name(self) -> str:
        """Backend name for logging."""
        ...
