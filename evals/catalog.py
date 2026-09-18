"""Guideline catalog lookup used by the citation-present gate.

The agent can only legitimately cite documents that are in `guidelines/catalog.json`
(the `list_guidelines` tool reads that file). The catalog path honours the same
`GUIDELINES_PATH` override the runtime uses, so an evaluation run can point at a
locally populated catalog without touching the copy that ships empty in git.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_source(value: str) -> str:
    """Fold a citation source string to a comparable key.

    "ATS/IDSA CAP 2019", "ats_idsa_cap_2019" and "ATS IDSA CAP 2019" all collapse
    to "atsidsacap2019", which is what makes matching model-authored citation
    strings against catalog keys tractable.
    """
    return _NORMALIZE_RE.sub("", value.strip().casefold())


def guidelines_dir() -> Path:
    """Resolve the guidelines directory, honouring the GUIDELINES_PATH override."""
    return Path(os.environ.get("GUIDELINES_PATH", "guidelines/"))


class GuidelineCatalog:
    """Normalized view of catalog.json for membership tests."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self._keys: set[str] = set()
        for entry in entries:
            for field in ("source", "title"):
                value = entry.get(field)
                if isinstance(value, str) and value.strip():
                    self._keys.add(normalize_source(value))

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def contains(self, source: str) -> bool:
        """True if `source` resolves to a catalog entry.

        Exact normalized match first, then a containment test in either direction
        so that "ATS/IDSA CAP 2019 (Section 5)" still resolves to the
        "ATS_IDSA_CAP_2019" entry.
        """
        if not source or not source.strip():
            return False
        needle = normalize_source(source)
        if needle in self._keys:
            return True
        return any(needle in key or key in needle for key in self._keys if len(key) >= 6)

    @classmethod
    def load(cls, path: Path | None = None) -> GuidelineCatalog:
        """Load the catalog. A missing or malformed file yields an empty catalog."""
        catalog_path = path or (guidelines_dir() / "catalog.json")
        try:
            payload = json.loads(Path(catalog_path).read_text())
        except (OSError, json.JSONDecodeError):
            return cls([])
        guidelines = payload.get("guidelines")
        if not isinstance(guidelines, list):
            return cls([])
        return cls([g for g in guidelines if isinstance(g, dict)])
