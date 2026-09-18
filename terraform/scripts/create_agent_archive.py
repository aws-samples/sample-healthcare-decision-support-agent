#!/usr/bin/env python
"""
Cross-platform script to create agent source archive for CodeBuild.

This script replaces the bash-based prepare_agent_code null_resource,
enabling Terraform to work on Windows without WSL or Git Bash.

Usage:
    python create_agent_archive.py <project_root> <terraform_dir> <output_path>

Output (JSON):
    {"md5": "<md5_hash>", "path": "<output_path>"}
"""

import hashlib
import json
import sys
import zipfile
from pathlib import Path


def add_file_to_zip(zf: zipfile.ZipFile, source_path: Path, archive_name: str) -> None:
    """Add a single file to the zip archive."""
    if source_path.exists() and source_path.is_file():
        zf.write(source_path, archive_name)


def add_directory_to_zip(zf: zipfile.ZipFile, source_dir: Path, archive_prefix: str) -> None:
    """Recursively add a directory to the zip archive."""
    if not source_dir.exists():
        return
    for item in source_dir.rglob("*"):
        if item.is_file():
            # Skip __pycache__ and other unwanted files
            if "__pycache__" in str(item) or item.suffix == ".pyc":
                continue
            relative_path = item.relative_to(source_dir)
            archive_path = f"{archive_prefix}/{relative_path}"
            zf.write(item, archive_path)


def calculate_md5(file_path: Path) -> str:
    """Calculate MD5 hash of a file."""
    hash_md5 = hashlib.md5(usedforsecurity=False)  # archive checksum, not a security control
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def create_agent_archive(
    project_root: Path, terraform_dir: Path, output_path: Path, profile: str = "agent"
) -> str:
    """Create a source archive for CodeBuild and return its MD5 hash.

    ``profile="agent"`` packages the container build inputs. ``profile="gate"``
    packages the candidate-acceptance gate: the same package plus ``evals/``, the
    frozen ``config/evals`` dataset and thresholds, the known-bad fixture, and the
    gate buildspec. The two archives hash independently, so editing the evaluation
    code never rebuilds the agent image.
    """
    if profile not in {"agent", "gate"}:
        raise ValueError(f"Unknown archive profile {profile!r}; expected 'agent' or 'gate'")

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Individual files from project root
        files_from_root = [
            "agent.py",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "Dockerfile",
        ]
        for filename in files_from_root:
            source = project_root / filename
            if source.exists():
                add_file_to_zip(zf, source, filename)

        # Directories from project root
        add_directory_to_zip(zf, project_root / "src", "src")
        add_directory_to_zip(zf, project_root / "prompts", "prompts")

        # Guidelines catalog and summaries (used by list_guidelines tool in container)
        add_file_to_zip(zf, project_root / "guidelines" / "catalog.json", "guidelines/catalog.json")
        add_directory_to_zip(zf, project_root / "guidelines" / "summaries", "guidelines/summaries")

        if profile == "agent":
            # Build files from terraform directory
            add_file_to_zip(zf, terraform_dir / "buildspec.yml", "buildspec.yml")
            add_file_to_zip(zf, terraform_dir / "scripts" / "build-docker.sh", "build-docker.sh")
        else:
            add_directory_to_zip(zf, project_root / "evals", "evals")
            add_directory_to_zip(zf, project_root / "config" / "evals", "config/evals")
            add_directory_to_zip(
                zf, project_root / "tests" / "fixtures" / "evals", "tests/fixtures/evals"
            )
            add_file_to_zip(zf, terraform_dir / "buildspec-gate.yml", "buildspec.yml")

    return calculate_md5(output_path)


def main():
    """Main entry point - reads from stdin for Terraform external data source."""
    # When called by Terraform external data source, input is JSON on stdin
    if len(sys.argv) == 1:
        # Called by Terraform - read JSON from stdin
        try:
            input_data = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
            sys.exit(1)

        # Validate required keys
        required_keys = ["project_root", "terraform_dir", "output_path"]
        missing_keys = [k for k in required_keys if k not in input_data]
        if missing_keys:
            print(f"Error: Missing required keys: {', '.join(missing_keys)}", file=sys.stderr)
            sys.exit(1)

        project_root = Path(input_data["project_root"])
        terraform_dir = Path(input_data["terraform_dir"])
        output_path = Path(input_data["output_path"])
        profile = input_data.get("profile", "agent")
    elif len(sys.argv) in (4, 5):
        # Called directly from command line
        project_root = Path(sys.argv[1])
        terraform_dir = Path(sys.argv[2])
        output_path = Path(sys.argv[3])
        profile = sys.argv[4] if len(sys.argv) == 5 else "agent"
    else:
        print(
            f"Usage: {sys.argv[0]} <project_root> <terraform_dir> <output_path> [agent|gate]",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        md5_hash = create_agent_archive(project_root, terraform_dir, output_path, profile)
        # Output JSON for Terraform external data source
        result = {"md5": md5_hash, "path": str(output_path.absolute())}
        print(json.dumps(result))
    except Exception as e:
        print(f"Error creating archive: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
