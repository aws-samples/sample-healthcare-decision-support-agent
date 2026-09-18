"""Search backend factory for config-driven backend selection."""

import logging
from pathlib import Path
from typing import Optional

from medical_nudging.config import get_search_backend_type
from medical_nudging.search.backend import SearchBackend

logger = logging.getLogger(__name__)

# Singleton backends
_backends: dict[str, SearchBackend] = {}


class SearchBackendFactory:
    """Factory for creating and caching search backends."""

    @staticmethod
    def create_ripgrep_backend(guidelines_dir: Path) -> SearchBackend:
        """Create a ripgrep backend with a custom directory.

        Args:
            guidelines_dir: Directory to search

        Returns:
            RipgrepBackend configured with the specified directory
        """
        from medical_nudging.search.ripgrep_backend import RipgrepBackend

        return RipgrepBackend(guidelines_dir=guidelines_dir)

    @staticmethod
    def get_backend(backend_type: Optional[str] = None) -> SearchBackend:
        """Get search backend by type.

        Args:
            backend_type: One of 'opensearch', 'ripgrep', 'auto'.
                         If None, uses agent.search_backend from settings.yaml (default: 'auto').

        Returns:
            SearchBackend instance
        """
        if backend_type is None:
            backend_type = get_search_backend_type()

        backend_type = backend_type.lower()

        if backend_type == "auto":
            return SearchBackendFactory._get_auto_backend()

        if backend_type in _backends:
            return _backends[backend_type]

        backend = SearchBackendFactory._create_backend(backend_type)
        _backends[backend_type] = backend
        return backend

    @staticmethod
    def _create_backend(backend_type: str) -> SearchBackend:
        """Create a new backend instance."""
        if backend_type == "opensearch":
            from medical_nudging.search.opensearch_backend import OpenSearchBackend

            return OpenSearchBackend()
        elif backend_type == "ripgrep":
            from medical_nudging.search.ripgrep_backend import RipgrepBackend

            return RipgrepBackend()
        else:
            raise ValueError(f"Unknown backend type: {backend_type}")

    @staticmethod
    def _get_auto_backend() -> SearchBackend:
        """Auto-select best available backend."""
        # Try in order: opensearch, ripgrep
        for backend_type in ["opensearch", "ripgrep"]:
            try:
                backend = SearchBackendFactory.get_backend(backend_type)
                if backend.is_available():
                    logger.info(f"Auto-selected search backend: {backend.name}")
                    return backend
            except Exception as e:
                logger.debug(f"Backend {backend_type} not available: {e}")

        # Fallback to ripgrep even if not available (will return empty results)
        logger.warning("No search backend available, using ripgrep fallback")
        return SearchBackendFactory.get_backend("ripgrep")


def get_search_backend(backend_type: Optional[str] = None) -> SearchBackend:
    """Convenience function to get search backend."""
    return SearchBackendFactory.get_backend(backend_type)
