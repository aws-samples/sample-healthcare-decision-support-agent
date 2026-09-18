"""Where benchmark artifacts may be written.

Persisted benchmark artifacts carry raw patient evidence — FHIR resources as evidence
spans, and the retrieved third-party guideline passages the nudge cites.  Neither may
enter the repository working tree, which is published as a public sample.  Every runner
that writes such artifacts routes its output directory through
:func:`ensure_output_dir_outside_repo` so the guard is one implementation rather than one
per script.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

#: Repository root, derived from this module's own location.
REPO_ROOT = Path(__file__).resolve().parents[3]


class ArtifactPathError(ValueError):
    """Raised when an output directory would write patient evidence into the repository."""


def ensure_output_dir_outside_repo(
    output_dir: Path | str,
    *,
    repo_root: Path | None = None,
    argument: str = "--output-dir",
) -> Path:
    """Resolve ``output_dir`` and refuse any path inside the repository working tree.

    Args:
        output_dir: Requested output directory (or artifact file path).
        repo_root: Repository root to guard; defaults to this repository.
        argument: CLI argument name to name in the error message.

    Returns:
        The resolved, absolute output directory.

    Raises:
        ArtifactPathError: When the resolved path is the repository root or beneath it.
    """
    root = (repo_root or REPO_ROOT).resolve()
    resolved = Path(output_dir).expanduser().resolve()
    roots = {root}
    if repo_root is None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
                capture_output=True,
                check=False,
            )
        except OSError:
            # Runtime images may omit Git; the source-root guard still applies.
            result = None
        if result is not None and result.returncode == 0:
            roots.update(
                Path(field.removeprefix("worktree ")).resolve()
                for field in result.stdout.decode().split("\0")
                if field.startswith("worktree ")
            )
    if any(resolved == candidate or candidate in resolved.parents for candidate in roots):
        raise ArtifactPathError(
            f"{argument} {resolved} is inside the repository at {root}. Persisted "
            "artifacts carry raw patient evidence and retrieved guideline text, and must "
            "be written outside the working tree."
        )
    return resolved
