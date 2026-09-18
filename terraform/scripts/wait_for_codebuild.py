#!/usr/bin/env python
"""
Cross-platform script to start a CodeBuild build and wait for completion.

This script provides Windows/Mac/Linux compatibility for Terraform local-exec.

Usage:
    python wait_for_codebuild.py --project-name <name> --region <region> [--profile <profile>]
        [--timeout <seconds>] [--env KEY=VALUE ...]

``--env`` overrides project environment variables for this build only (CodeBuild
``environmentVariablesOverride``); the candidate gate uses it to run the known-bad
fixture through the same project.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def run_aws_command(args: list[str], profile: str | None = None) -> dict:
    """Run an AWS CLI command and return JSON output."""
    cmd = ["aws"] + args + ["--output", "json"]
    if profile:
        cmd.extend(["--profile", profile])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    return json.loads(result.stdout) if result.stdout.strip() else {}


def parse_env_overrides(pairs: list[str] | None) -> list[dict[str, str]]:
    """Turn KEY=VALUE pairs into CodeBuild environment variable overrides."""
    overrides = []
    for pair in pairs or []:
        if "=" not in pair:
            print(f"Error: --env expects KEY=VALUE, got {pair!r}", file=sys.stderr)
            sys.exit(2)
        name, value = pair.split("=", 1)
        overrides.append({"name": name, "value": value, "type": "PLAINTEXT"})
    return overrides


def start_build(
    project_name: str,
    region: str,
    profile: str | None = None,
    env_overrides: list[dict[str, str]] | None = None,
) -> str:
    """Start a CodeBuild build and return the build ID."""
    print(f"Starting CodeBuild project: {project_name}")

    args = ["codebuild", "start-build", "--project-name", project_name, "--region", region]
    if env_overrides:
        args.extend(["--environment-variables-override", json.dumps(env_overrides)])
    result = run_aws_command(args, profile)

    build_id = result["build"]["id"]
    print(f"Build started: {build_id}")
    return build_id


# A build rejected before it ran, because CodeBuild could not yet use the freshly
# created service role. IAM changes take a few seconds to propagate; the same build
# succeeds when started again.
RETRYABLE_QUEUE_ERROR = "retryable"
ROLE_PROPAGATION_ATTEMPTS = 3
ROLE_PROPAGATION_DELAY_SECONDS = 20


def _failed_before_running(build: dict) -> bool:
    """True when the build never left the queue because its service role was rejected."""
    for phase in build.get("phases", []):
        if phase.get("phaseType") in ("SUBMITTED", "QUEUED") and phase.get("phaseStatus") in (
            "CLIENT_ERROR",
            "FAULT",
        ):
            messages = " ".join(c.get("message", "") for c in phase.get("contexts", []))
            if "role" in messages.lower():
                return True
    return False


def wait_for_build(
    build_id: str, region: str, profile: str | None = None, timeout: int = 900
) -> bool | str:
    """Wait for a CodeBuild build to complete.

    Returns True on success, False on failure, and RETRYABLE_QUEUE_ERROR when the
    build was rejected in the queue because its service role had not propagated yet.
    """
    print(f"Waiting for build to complete (timeout: {timeout}s)...")

    start_time = time.time()
    last_phase = ""

    while time.time() - start_time < timeout:
        result = run_aws_command(
            ["codebuild", "batch-get-builds", "--ids", build_id, "--region", region], profile
        )

        build = result["builds"][0]
        status = build["buildStatus"]
        phase = build.get("currentPhase", "")

        # Print phase changes
        if phase != last_phase:
            print(f"  Phase: {phase}")
            last_phase = phase

        if status == "SUCCEEDED":
            print("Build completed successfully!")
            return True
        elif status in ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"]:
            if _failed_before_running(build):
                print(
                    "Build was rejected before running (service role not yet usable by "
                    "CodeBuild); retrying after IAM propagation delay",
                    file=sys.stderr,
                )
                return RETRYABLE_QUEUE_ERROR
            print(f"Build failed with status: {status}", file=sys.stderr)
            # Print logs URL
            logs = build.get("logs", {})
            if "deepLink" in logs:
                print(f"  Logs: {logs['deepLink']}", file=sys.stderr)
            return False

        time.sleep(10)

    print(f"Timeout waiting for build after {timeout}s", file=sys.stderr)
    return False


def main():
    parser = argparse.ArgumentParser(description="Start CodeBuild and wait for completion")
    parser.add_argument("--project-name", required=True, help="CodeBuild project name")
    parser.add_argument("--region", required=True, help="AWS region")
    parser.add_argument("--profile", help="AWS profile (optional)")
    parser.add_argument(
        "--timeout", type=int, default=900, help="Timeout in seconds (default: 900)"
    )
    parser.add_argument(
        "--env",
        action="append",
        metavar="KEY=VALUE",
        help="Override a project environment variable for this build (repeatable)",
    )

    args = parser.parse_args()

    env_overrides = parse_env_overrides(args.env)
    success: bool | str = False
    for attempt in range(1, ROLE_PROPAGATION_ATTEMPTS + 1):
        build_id = start_build(args.project_name, args.region, args.profile, env_overrides)
        success = wait_for_build(build_id, args.region, args.profile, args.timeout)
        if success != RETRYABLE_QUEUE_ERROR:
            break
        if attempt < ROLE_PROPAGATION_ATTEMPTS:
            time.sleep(ROLE_PROPAGATION_DELAY_SECONDS)
    sys.exit(0 if success is True else 1)


if __name__ == "__main__":
    main()
