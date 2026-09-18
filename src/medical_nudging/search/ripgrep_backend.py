"""Ripgrep/Python fallback search backend."""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from medical_nudging.config import get_guidelines_path
from medical_nudging.search.backend import SearchBackend, SearchResult

logger = logging.getLogger(__name__)


class RipgrepBackend(SearchBackend):
    """Ripgrep-based search with Python fallback."""

    def __init__(self, guidelines_dir: Path | None = None):
        """Initialize ripgrep backend.

        Args:
            guidelines_dir: Custom directory to search. If None, uses default.
        """
        self._custom_dir = guidelines_dir

    def _get_dir(self) -> Path:
        """Get the guidelines directory to search."""
        if self._custom_dir:
            return self._custom_dir
        return get_guidelines_path()

    @property
    def name(self) -> str:
        return "ripgrep"

    def is_available(self) -> bool:
        return self._get_dir().exists()

    def list_sources(self) -> list[dict]:
        """Not supported for ripgrep backend."""
        return []

    def list_indices(self) -> list[str]:
        """Not supported for ripgrep backend."""
        return []

    def search(
        self,
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        guidelines_dir = self._get_dir()
        if not guidelines_dir.exists():
            return []

        if shutil.which("rg"):
            return self._search_ripgrep(query, guidelines_dir, sources, limit)
        return self._search_python(query, guidelines_dir, sources, limit)

    def _search_ripgrep(
        self,
        query: str,
        guidelines_dir: Path,
        sources: list[str] | None,
        limit: int,
    ) -> list[SearchResult]:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color=never",
            "--ignore-case",
            "--context=1",
            "--max-count",
            str(limit * 2),  # Get more matches, dedupe later
            query,
        ]

        if sources:
            for name in sources:
                cmd.extend(["--glob", f"*{name}*"])

        cmd.append(str(guidelines_dir))

        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )  # rc 1 = no matches
        if result.returncode not in (0, 1):  # 1 = no matches
            return []

        return self._parse_ripgrep_output(result.stdout, limit)

    def _parse_ripgrep_output(self, output: str, limit: int) -> list[SearchResult]:
        if not output.strip():
            return []

        results = []
        current_file = None
        current_lines: list[str] = []

        for line in output.split("\n"):
            if not line.strip():
                continue

            # Parse filepath:line:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue

            filepath, line_num, content = parts[0], parts[1], parts[2]
            filename = Path(filepath).stem

            if filename != current_file:
                if current_file and current_lines:
                    results.append(
                        SearchResult(
                            source=current_file,
                            section=None,
                            section_number=None,
                            page_number=None,
                            content="\n".join(current_lines),
                            relevance=1.0 - (len(results) * 0.1),
                        )
                    )
                current_file = filename
                current_lines = []

            current_lines.append(f"Line {line_num}: {content}")

        # Add last file
        if current_file and current_lines:
            results.append(
                SearchResult(
                    source=current_file,
                    section=None,
                    section_number=None,
                    page_number=None,
                    content="\n".join(current_lines),
                    relevance=1.0 - (len(results) * 0.1),
                )
            )

        return results[:limit]

    def _search_python(
        self,
        query: str,
        guidelines_dir: Path,
        sources: list[str] | None,
        limit: int,
    ) -> list[SearchResult]:
        # Summaries are markdown (guidelines/summaries/<SOURCE>/*.md); plain-text
        # corpora are also accepted. Mirrors what ripgrep searches when installed.
        files = sorted(
            f for pattern in ("**/*.md", "**/*.txt") for f in guidelines_dir.glob(pattern)
        )

        if sources:
            files = [f for f in files if any(s.lower() in f.name.lower() for s in sources)]

        results = []
        query_lower = query.lower()

        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                continue

            matches = []
            for i, line in enumerate(lines, start=1):
                if query_lower in line.lower():
                    matches.append(f"Line {i}: {line.strip()}")

            if matches:
                results.append(
                    SearchResult(
                        source=file_path.stem,
                        section=None,
                        section_number=None,
                        page_number=None,
                        content="\n".join(matches[:5]),
                        relevance=1.0 - (len(results) * 0.1),
                    )
                )

            if len(results) >= limit:
                break

        return results
