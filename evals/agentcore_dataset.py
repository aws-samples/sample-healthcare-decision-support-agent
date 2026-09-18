"""Publish the reviewed regression dataset as an immutable AgentCore dataset version.

AgentCore Dataset Management keeps one mutable Draft per dataset and numbered,
immutable published versions. The gate pins a published version, so the scenarios it
ran are fixed regardless of later edits to the Draft.

    # Create the dataset from the checked-in file and publish version 1
    uv run python -m evals.agentcore_dataset publish \
        --file config/evals/regression_dataset_v1.json \
        --name medical_nudging_regression --region us-east-1

    # Later: replace the Draft with a revised file and publish the next version
    uv run python -m evals.agentcore_dataset publish \
        --file config/evals/regression_dataset_v2.json --dataset-id <id>

    # Show a dataset and its published versions
    uv run python -m evals.agentcore_dataset describe --dataset-id <id>

Only identifiers, run configuration, and behavioural assertions leave the machine:
the checked-in file contains no patient records. Reviewed production sessions are
never ingested automatically; they enter the Draft only after de-identified human
review, then become the next version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger("evals.agentcore_dataset")

SCHEMA_TYPE = "AGENTCORE_EVALUATION_PREDEFINED_V1"


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    scenarios = payload.get("scenarios") if isinstance(payload, Mapping) else None
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(f"{path}: expected an object with a non-empty 'scenarios' list")
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or not scenario.get("scenario_id"):
            raise ValueError(f"{path}: every scenario needs a scenario_id")
        if not scenario.get("turns"):
            raise ValueError(f"{path}: scenario {scenario['scenario_id']} has no turns")
    return [dict(scenario) for scenario in scenarios]


def inline_examples(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The scenarios as AgentCore inline examples (the predefined schema is the same)."""
    return [dict(scenario) for scenario in scenarios]


def dataset_version_label(scenarios: list[dict[str, Any]]) -> str:
    labels = {(scenario.get("metadata") or {}).get("dataset_version") for scenario in scenarios}
    labels.discard(None)
    if len(labels) != 1:
        raise ValueError("Scenarios must share one metadata.dataset_version label")
    return str(labels.pop())


def publish(args: argparse.Namespace) -> int:
    from bedrock_agentcore.evaluation import DatasetClient

    path: Path = args.file
    scenarios = load_scenarios(path)
    label = dataset_version_label(scenarios)
    file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    client = DatasetClient(region_name=args.region)
    description = args.description or (
        f"Medical nudging regression scenarios {label}; source sha256 {file_sha256[:12]}"
    )

    control = client._cp_client  # boto3 bedrock-agentcore-control client
    source = {"inlineExamples": {"examples": inline_examples(scenarios)}}
    if args.dataset_id:
        dataset_id = args.dataset_id
        existing_ids = draft_example_ids(control, dataset_id)
        if existing_ids:
            log.info("Removing %d Draft example(s) before replacing them", len(existing_ids))
            client.delete_examples_and_wait(datasetId=dataset_id, exampleIds=existing_ids)
        client.add_examples_and_wait(datasetId=dataset_id, source=source)
    else:
        created = client.create_dataset_and_wait(
            datasetName=args.name,
            schemaType=SCHEMA_TYPE,
            description=description[:200],
            source=source,
        )
        dataset_id = created["datasetId"]
        log.info("Created dataset %s (%s)", dataset_id, created.get("status"))

    published = control.create_dataset_version(datasetId=dataset_id)
    version = str(published["datasetVersion"])
    wait_until_active(control, dataset_id)
    pointer = {
        "dataset_id": dataset_id,
        "dataset_arn": published.get("datasetArn"),
        "dataset_version": version,
        "dataset_version_label": label,
        "source_file": str(path),
        "source_sha256": file_sha256,
        "scenario_count": len(scenarios),
        "region": args.region,
        "schema_type": SCHEMA_TYPE,
    }
    print(json.dumps(pointer, indent=2))
    if args.pointer:
        args.pointer.parent.mkdir(parents=True, exist_ok=True)
        args.pointer.write_text(json.dumps(pointer, indent=2) + "\n")
        log.info("Wrote pointer %s", args.pointer)
    return 0


def draft_example_ids(control: Any, dataset_id: str) -> list[str]:
    """Example ids currently in the Draft (paginated ListDatasetExamples)."""
    ids: list[str] = []
    kwargs: dict[str, Any] = {"datasetId": dataset_id}
    while True:
        page = control.list_dataset_examples(**kwargs)
        for example in page.get("examples") or []:
            example_id = example.get("exampleId")
            if example_id:
                ids.append(example_id)
        token = page.get("nextToken")
        if not token:
            return ids
        kwargs["nextToken"] = token


def wait_until_active(
    control: Any, dataset_id: str, *, timeout_seconds: int = 300, poll_seconds: int = 3
) -> dict[str, Any]:
    """Poll GetDataset until the dataset is ACTIVE again after publishing."""
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        dataset = control.get_dataset(datasetId=dataset_id)
        status = dataset.get("status")
        if status == "ACTIVE":
            return dataset
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}:
            raise RuntimeError(
                f"Dataset {dataset_id} entered {status}: {dataset.get('failureReason')}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(f"Dataset {dataset_id} still {status} after {timeout_seconds}s")
        time.sleep(poll_seconds)


def describe(args: argparse.Namespace) -> int:
    from bedrock_agentcore.evaluation import DatasetClient

    client = DatasetClient(region_name=args.region)
    control = client._cp_client
    dataset = control.get_dataset(datasetId=args.dataset_id)
    dataset.pop("ResponseMetadata", None)
    dataset.pop("downloadUrl", None)
    print(json.dumps(dataset, indent=2, default=str))
    try:
        versions = control.list_dataset_versions(datasetId=args.dataset_id)
        versions.pop("ResponseMetadata", None)
        print(json.dumps(versions, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001 - listing versions is optional detail
        log.warning("Could not list dataset versions: %s", exc)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", default=None)
    # The same two flags are accepted after the subcommand (the form the deployment guide
    # shows); SUPPRESS keeps a subcommand-level omission from overriding the top-level value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=argparse.SUPPRESS)
    common.add_argument("--profile", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser(
        "publish",
        parents=[common],
        help="Create/replace the Draft from a file and publish a version",
    )
    pub.add_argument("--file", type=Path, required=True)
    pub.add_argument(
        "--name", default="medical_nudging_regression", help="Dataset name for a new dataset"
    )
    pub.add_argument(
        "--dataset-id",
        default=None,
        help="Existing dataset: replace its Draft, publish next version",
    )
    pub.add_argument("--description", default=None)
    pub.add_argument(
        "--pointer", type=Path, default=None, help="Write the id/version pointer JSON here"
    )
    pub.set_defaults(func=publish)

    desc = sub.add_parser(
        "describe", parents=[common], help="Show a dataset and its published versions"
    )
    desc.add_argument("--dataset-id", required=True)
    desc.set_defaults(func=describe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    try:
        return int(args.func(args))
    except (ValueError, OSError) as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
