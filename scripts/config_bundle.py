#!/usr/bin/env python
"""Version the deployed agent's system prompt with AgentCore configuration bundles.

A bundle holds the base system prompt for one runtime. Every update is an immutable
version with a commit message and a parent, so the version history is the audit trail
of prompt changes. The runtime applies a version only when a request names it in the
``baggage`` header; the candidate-acceptance gate pins one version per run and records
it next to the runtime version.

Usage:
    # First version from the repository prompt (run once per runtime)
    uv run scripts/config_bundle.py create --runtime-arn arn:aws:bedrock-agentcore:... \
        --name medical_nudging_prompt --message "Initial prompt from prompts/orchestrator.md"

    # Publish an edited prompt as the next version
    uv run scripts/config_bundle.py update --bundle-id <id> --runtime-arn arn:... \
        --prompt-file /path/to/edited-orchestrator.md --message "Soften urgency wording"

    # History, one version's prompt, and the diff between two versions
    uv run scripts/config_bundle.py versions --bundle-id <id>
    uv run scripts/config_bundle.py show --bundle-id <id> --version <version> --runtime-arn arn:...
    uv run scripts/config_bundle.py diff --bundle-id <id> --runtime-arn arn:... --from <v1> --to <v2>

    # Gate a version, then promote by pinning it wherever the runtime is invoked
    uv run python -m evals.agentcore_gate ... --bundle-id <id> --bundle-version <version>

Every command prints JSON (or the diff text) so a pipeline step can capture the ids.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from medical_nudging.config_bundle import (
    BundleRef,
    create_bundle,
    diff_prompts,
    latest_version,
    list_versions,
    repository_base_prompt,
    update_system_prompt,
    version_prompt,
)


def _client(args: argparse.Namespace) -> Any:
    import boto3

    return boto3.Session(profile_name=args.profile, region_name=args.region).client(
        "bedrock-agentcore-control"
    )


def _prompt_text(path: Path | None) -> str:
    return path.read_text() if path else repository_base_prompt()


def _emit(payload: Any, output: Path | None) -> None:
    text = json.dumps(payload, indent=2, default=str)
    if output:
        output.write_text(text + "\n")
    print(text)


def cmd_create(args: argparse.Namespace) -> int:
    result = create_bundle(
        _client(args),
        name=args.name,
        runtime_arn=args.runtime_arn,
        system_prompt=_prompt_text(args.prompt_file),
        commit_message=args.message,
        description=args.description,
    )
    result["baggage"] = BundleRef(result["bundle_arn"], result["version_id"]).baggage()
    _emit(result, args.output)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    result = update_system_prompt(
        _client(args),
        bundle_id=args.bundle_id,
        runtime_arn=args.runtime_arn,
        system_prompt=_prompt_text(args.prompt_file),
        commit_message=args.message,
        created_by=args.created_by,
        branch_name=args.branch,
    )
    result["baggage"] = BundleRef(result["bundle_arn"], result["version_id"]).baggage()
    _emit(result, args.output)
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    _emit(list_versions(_client(args), args.bundle_id), args.output)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    client = _client(args)
    version_id = args.version or latest_version(client, args.bundle_id)["versionId"]
    print(version_prompt(client, args.bundle_id, version_id, args.runtime_arn), end="")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    text = diff_prompts(
        _client(args), args.bundle_id, args.runtime_arn, args.from_version, args.to_version
    )
    print(
        text
        if text
        else f"No system_prompt difference between {args.from_version} and {args.to_version}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile", default=None)
    # Also accepted after the subcommand; SUPPRESS keeps the top-level value when omitted.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=argparse.SUPPRESS)
    common.add_argument("--profile", default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create", parents=[common], help="Create a bundle with the first prompt version"
    )
    create.add_argument("--runtime-arn", required=True, help="Runtime the prompt applies to")
    create.add_argument("--name", required=True, help="Bundle name (letters, digits, underscores)")
    create.add_argument("--prompt-file", type=Path, help="Default: prompts/orchestrator.md")
    create.add_argument("--message", required=True, help="Commit message for version 1")
    create.add_argument("--description", default=None)
    create.add_argument("--output", type=Path, help="Also write the JSON result here")
    create.set_defaults(func=cmd_create)

    update = commands.add_parser(
        "update", parents=[common], help="Publish a new immutable prompt version"
    )
    update.add_argument("--bundle-id", required=True)
    update.add_argument("--runtime-arn", required=True)
    update.add_argument("--prompt-file", type=Path, help="Default: prompts/orchestrator.md")
    update.add_argument("--message", required=True, help="What changed and why")
    update.add_argument("--created-by", default=None, help="Person or job recorded as author")
    update.add_argument("--branch", default=None, help="Branch name (default: mainline)")
    update.add_argument("--output", type=Path, help="Also write the JSON result here")
    update.set_defaults(func=cmd_update)

    versions = commands.add_parser("versions", parents=[common], help="List versions, newest first")
    versions.add_argument("--bundle-id", required=True)
    versions.add_argument("--output", type=Path)
    versions.set_defaults(func=cmd_versions)

    show = commands.add_parser("show", parents=[common], help="Print one version's system prompt")
    show.add_argument("--bundle-id", required=True)
    show.add_argument("--runtime-arn", required=True)
    show.add_argument("--version", default=None, help="Default: the latest mainline version")
    show.set_defaults(func=cmd_show)

    diff = commands.add_parser(
        "diff", parents=[common], help="Unified diff of the prompt between two versions"
    )
    diff.add_argument("--bundle-id", required=True)
    diff.add_argument("--runtime-arn", required=True)
    diff.add_argument("--from", dest="from_version", required=True)
    diff.add_argument("--to", dest="to_version", required=True)
    diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
