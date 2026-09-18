"""Load and format guideline summaries for context injection.

This module loads pre-made markdown summaries from guidelines/summaries/
for use in the 'summaries_only' context condition during experiments.
"""

import logging
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Default summaries directory relative to project root
DEFAULT_SUMMARIES_DIR = Path(__file__).parent.parent.parent.parent / "guidelines" / "summaries"


class GuidelineSummary(NamedTuple):
    """A guideline summary with metadata."""

    source: str  # Organization (ADA, AHA_ACC, CDC, MISC)
    filename: str  # Original filename
    title: str  # Extracted title or filename stem
    content: str  # Full markdown content


def extract_title_from_markdown(content: str, filename: str) -> str:
    """Extract title from markdown content.

    Looks for first H1 header (# Title) or uses filename stem.

    Args:
        content: Markdown content
        filename: Filename to use as fallback

    Returns:
        Extracted title
    """
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()


def load_guideline_summaries(
    summaries_dir: Path | None = None,
    sources: list[str] | None = None,
) -> list[GuidelineSummary]:
    """Load all guideline summaries from the summaries directory.

    Args:
        summaries_dir: Path to summaries directory (default: guidelines/summaries/)
        sources: Optional list of sources to include (e.g., ["ADA", "AHA_ACC"])
                 If None, loads all sources.

    Returns:
        List of GuidelineSummary objects
    """
    if summaries_dir is None:
        summaries_dir = DEFAULT_SUMMARIES_DIR

    if not summaries_dir.exists():
        logger.warning(f"Summaries directory not found: {summaries_dir}")
        return []

    summaries: list[GuidelineSummary] = []

    # Find all markdown files in the summaries directory
    # Structure: guidelines/summaries/{source}/*.md or guidelines/summaries/*.md
    for md_file in summaries_dir.rglob("*.md"):
        # Determine source from parent directory or use "MISC"
        rel_path = md_file.relative_to(summaries_dir)
        if len(rel_path.parts) > 1:
            source = rel_path.parts[0]
        else:
            source = "MISC"

        # Filter by source if specified
        if sources is not None and source not in sources:
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
            title = extract_title_from_markdown(content, md_file.name)

            summaries.append(
                GuidelineSummary(
                    source=source,
                    filename=md_file.name,
                    title=title,
                    content=content,
                )
            )
        except Exception as e:
            logger.warning(f"Failed to load summary {md_file}: {e}")

    logger.info(f"Loaded {len(summaries)} guideline summaries from {summaries_dir}")
    return summaries


def format_summaries_for_prompt(
    summaries: list[GuidelineSummary],
    max_length: int | None = None,
) -> str:
    """Format guideline summaries for injection into system prompt.

    Args:
        summaries: List of GuidelineSummary objects
        max_length: Optional maximum total length in characters

    Returns:
        Formatted string for system prompt injection
    """
    if not summaries:
        return ""

    # Group summaries by source
    by_source: dict[str, list[GuidelineSummary]] = {}
    for s in summaries:
        if s.source not in by_source:
            by_source[s.source] = []
        by_source[s.source].append(s)

    # Build formatted output
    sections = []
    sections.append("# Clinical Guidelines Reference")
    sections.append("")
    sections.append(
        "The following clinical guideline summaries are provided for reference. "
        "Use these to inform your clinical recommendations."
    )
    sections.append("")

    for source in sorted(by_source.keys()):
        source_summaries = by_source[source]
        sections.append(f"## {source} Guidelines")
        sections.append("")

        for summary in sorted(source_summaries, key=lambda x: x.title):
            sections.append(f"### {summary.title}")
            sections.append("")
            # Include content (potentially truncated per summary)
            content = summary.content
            # Remove the title line if it exists to avoid duplication
            lines = content.split("\n")
            if lines and lines[0].strip().startswith("# "):
                content = "\n".join(lines[1:]).strip()
            sections.append(content)
            sections.append("")

    result = "\n".join(sections)

    # Truncate if needed
    if max_length is not None and len(result) > max_length:
        result = result[:max_length]
        # Try to end at a complete line
        last_newline = result.rfind("\n")
        if last_newline > max_length * 0.8:
            result = result[:last_newline]
        result += "\n\n[Guidelines truncated due to length]"

    return result


def get_summaries_token_estimate(summaries: list[GuidelineSummary]) -> int:
    """Estimate token count for summaries.

    Uses rough estimate of 4 characters per token.

    Args:
        summaries: List of GuidelineSummary objects

    Returns:
        Estimated token count
    """
    total_chars = sum(len(s.content) for s in summaries)
    return total_chars // 4
