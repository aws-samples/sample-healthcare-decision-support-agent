"""AgentCore configuration bundles: a versioned, service-side home for the system prompt.

A bundle stores key-value configuration per runtime component and every update is an
immutable version with a commit message and parent ids. The caller selects the version
per request through the W3C ``baggage`` header (the two keys below); the runtime reads
its component's configuration and applies ``system_prompt`` as the base prompt in place
of ``prompts/orchestrator.md``. Nothing here is required: without a bundle reference
the repository prompt is used, and local runs never touch the control plane.

Only ``system_prompt`` is applied. The regression gate pins the model in the frozen
dataset configuration, so a model change is a dataset re-freeze, not a prompt hot-swap.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping
from urllib.parse import unquote

logger = logging.getLogger(__name__)

BAGGAGE_HEADER = "baggage"
BAGGAGE_ARN_KEY = "aws.agentcore.configbundle_arn"
BAGGAGE_VERSION_KEY = "aws.agentcore.configbundle_version"
APPLIED_KEYS = ("system_prompt",)
PROMPT_SOURCE_BUNDLE = "config_bundle"
PROMPT_SOURCE_REPOSITORY = "repository"
RUNTIME_ARN_ENV = "AGENTCORE_RUNTIME_ARN"


class ConfigBundleError(RuntimeError):
    """A bundle reference was present but could not be applied."""


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- reference -----------------------------------------------------------------


@dataclass(frozen=True)
class BundleRef:
    """One immutable bundle version, as named in request baggage."""

    bundle_arn: str
    bundle_version: str

    def __post_init__(self) -> None:
        if not self.bundle_arn or "/" not in self.bundle_arn or self.bundle_arn.endswith("/"):
            raise ValueError(f"bundle_arn is not a configuration-bundle ARN: {self.bundle_arn!r}")
        if not self.bundle_version:
            raise ValueError("bundle_version must not be empty")

    @property
    def bundle_id(self) -> str:
        return self.bundle_arn.rsplit("/", 1)[-1]

    def baggage(self) -> str:
        """The ``baggage`` header value that selects this version."""
        return f"{BAGGAGE_ARN_KEY}={self.bundle_arn},{BAGGAGE_VERSION_KEY}={self.bundle_version}"

    def as_dict(self) -> dict[str, str]:
        return {
            "bundle_arn": self.bundle_arn,
            "bundle_id": self.bundle_id,
            "bundle_version": self.bundle_version,
        }


def parse_baggage(header: str | None) -> dict[str, str]:
    """First value per key from a W3C baggage header; properties and encoding stripped."""
    values: dict[str, str] = {}
    for item in (header or "").split(","):
        key, sep, value = item.strip().partition("=")
        if not sep or not key.strip():
            continue
        decoded = unquote(value.split(";")[0].strip())
        if decoded:
            values.setdefault(key.strip(), decoded)
    return values


def bundle_ref_from_baggage(header: str | None) -> BundleRef | None:
    """The bundle version a request asks for, or ``None`` when the header names none."""
    values = parse_baggage(header)
    arn = values.get(BAGGAGE_ARN_KEY)
    version = values.get(BAGGAGE_VERSION_KEY)
    if not arn or not version:
        return None
    try:
        return BundleRef(bundle_arn=arn, bundle_version=version)
    except ValueError as error:
        logger.warning("Ignoring invalid configuration bundle reference: %s", error)
        return None


def runtime_arn_from_environment() -> str | None:
    """This process's runtime ARN: an explicit env var, else OTEL's ``cloud.resource_id``.

    AgentCore sets ``OTEL_RESOURCE_ATTRIBUTES`` with a ``cloud.resource_id`` that is
    either the runtime ARN or a runtime-endpoint ARN; the endpoint suffix is dropped so
    the value matches the component key a bundle was created with.
    """
    explicit = os.environ.get(RUNTIME_ARN_ENV)
    if explicit:
        return explicit
    for attribute in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, sep, value = attribute.strip().partition("=")
        if sep and key.strip() == "cloud.resource_id" and value.strip():
            return value.strip().split("/runtime-endpoint/", 1)[0]
    return None


# --- resolved configuration ----------------------------------------------------


@dataclass(frozen=True)
class BundleConfig:
    """This runtime's configuration from one bundle version."""

    ref: BundleRef
    component_arn: str
    values: Mapping[str, Any]
    commit_message: str | None = None
    branch_name: str | None = None

    @property
    def system_prompt(self) -> str | None:
        prompt = self.values.get("system_prompt")
        return prompt if isinstance(prompt, str) and prompt.strip() else None

    def provenance(self) -> dict[str, Any]:
        """What to record next to every result produced under this bundle."""
        prompt = self.system_prompt
        return {
            **self.ref.as_dict(),
            "component_arn": self.component_arn,
            "branch_name": self.branch_name,
            "commit_message": self.commit_message,
            "applied_keys": [key for key in APPLIED_KEYS if self.values.get(key) is not None],
            "ignored_keys": sorted(key for key in self.values if key not in APPLIED_KEYS),
            "system_prompt_sha256": prompt_sha256(prompt) if prompt else None,
        }


def component_configuration(version: Mapping[str, Any], component_arn: str) -> BundleConfig:
    """Select ``component_arn``'s configuration from a ``GetConfigurationBundleVersion`` response."""
    components = version.get("components") or {}
    component = components.get(component_arn)
    if component is None:
        raise ConfigBundleError(
            f"bundle {version.get('bundleId')!r} version {version.get('versionId')!r} has no "
            f"configuration for {component_arn!r}; components: {sorted(components)}"
        )
    lineage = version.get("lineageMetadata") or {}
    return BundleConfig(
        ref=BundleRef(bundle_arn=version["bundleArn"], bundle_version=version["versionId"]),
        component_arn=component_arn,
        values=dict(component.get("configuration") or {}),
        commit_message=lineage.get("commitMessage"),
        branch_name=lineage.get("branchName"),
    )


class ConfigBundleResolver:
    """Fetch bundle versions for this runtime, once per version.

    Versions are immutable, so a resolved version is cached for the process lifetime.
    The boto3 client is created on first use so runtimes that never receive a bundle
    reference never build one.
    """

    def __init__(
        self,
        *,
        runtime_arn: str | None = None,
        client: Any | None = None,
        region: str | None = None,
    ) -> None:
        self._runtime_arn = runtime_arn
        self._client = client
        self._region = region
        self._cache: dict[BundleRef, BundleConfig] = {}

    @property
    def runtime_arn(self) -> str:
        arn = self._runtime_arn or runtime_arn_from_environment()
        if not arn:
            raise ConfigBundleError(
                "Cannot select a bundle component: neither AGENTCORE_RUNTIME_ARN nor "
                "OTEL_RESOURCE_ATTRIBUTES cloud.resource_id names this runtime"
            )
        return arn

    def _control_plane(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-agentcore-control", region_name=self._region)
        return self._client

    def resolve(self, ref: BundleRef) -> BundleConfig:
        cached = self._cache.get(ref)
        if cached is not None:
            return cached
        try:
            version = self._control_plane().get_configuration_bundle_version(
                bundleId=ref.bundle_id, versionId=ref.bundle_version
            )
        except Exception as error:  # boto3 raises client-specific classes
            raise ConfigBundleError(
                f"could not fetch bundle {ref.bundle_id!r} version {ref.bundle_version!r}: {error}"
            ) from error
        config = component_configuration(version, self.runtime_arn)
        self._cache[ref] = config
        logger.info(
            "Configuration bundle %s version %s applied (%s)",
            config.ref.bundle_id,
            config.ref.bundle_version,
            ", ".join(config.provenance()["applied_keys"]) or "no applied keys",
        )
        return config


# --- request scope -------------------------------------------------------------

_active: ContextVar[BundleConfig | None] = ContextVar("active_config_bundle", default=None)


def active_bundle() -> BundleConfig | None:
    """The bundle configuration the current request runs under, if any."""
    return _active.get()


@contextmanager
def bundle_scope(config: BundleConfig | None) -> Iterator[None]:
    """Make ``config`` the active bundle for the enclosed request."""
    token = _active.set(config)
    try:
        yield
    finally:
        _active.reset(token)


def repository_base_prompt() -> str:
    """The checked-in base prompt, ``prompts/orchestrator.md``."""
    from .config import get_prompts_path

    return (get_prompts_path() / "orchestrator.md").read_text()


def effective_base_prompt() -> str:
    """The active bundle's ``system_prompt`` when one is applied, else the repository prompt."""
    bundle = active_bundle()
    if bundle is not None and bundle.system_prompt is not None:
        return bundle.system_prompt
    return repository_base_prompt()


def prompt_provenance() -> dict[str, Any]:
    """Where the base system prompt came from and its hash, for run artifacts and gates."""
    bundle = active_bundle()
    applied = bundle is not None and bundle.system_prompt is not None
    return {
        "prompt_source": PROMPT_SOURCE_BUNDLE if applied else PROMPT_SOURCE_REPOSITORY,
        "base_prompt_sha256": prompt_sha256(effective_base_prompt()),
        "config_bundle": bundle.provenance() if bundle is not None else None,
    }


# --- control-plane operations shared by the CLI and Terraform -----------------


def create_bundle(
    client: Any,
    *,
    name: str,
    runtime_arn: str,
    system_prompt: str,
    commit_message: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a bundle whose first version carries ``system_prompt`` for ``runtime_arn``.

    Bundle names are unique per account and components are keyed by runtime ARN, so a
    runtime that is re-created (every image rebuild does this when the runtime is named
    after the image tag) must not start a new bundle: the existing bundle gets a new
    version carrying the prompt for the new runtime ARN, and the history stays in one place.
    """
    existing = find_bundle_by_name(client, name)
    if existing is None:
        request: dict[str, Any] = {
            "bundleName": name,
            "components": {runtime_arn: {"configuration": {"system_prompt": system_prompt}}},
            "commitMessage": commit_message[:500],
            "clientToken": str(uuid.uuid4()),
        }
        if description:
            request["description"] = description[:500]
        try:
            response = client.create_configuration_bundle(**request)
        except Exception as error:  # ConflictException: created between the lookup and now
            if type(error).__name__ != "ConflictException":
                raise
            existing = find_bundle_by_name(client, name)
            if existing is None:
                raise
        else:
            return {
                "bundle_id": response["bundleId"],
                "bundle_arn": response["bundleArn"],
                "version_id": response["versionId"],
                "system_prompt_sha256": prompt_sha256(system_prompt),
                "created": True,
            }
    result = update_system_prompt(
        client,
        bundle_id=existing["bundleId"],
        runtime_arn=runtime_arn,
        system_prompt=system_prompt,
        commit_message=f"{commit_message} [component added for re-created runtime]",
    )
    result["created"] = False
    return result


def find_bundle_by_name(client: Any, name: str) -> dict[str, Any] | None:
    """The bundle summary whose ``bundleName`` is ``name``, or None."""
    lister = getattr(client, "list_configuration_bundles", None)
    if lister is None:
        return None
    token: str | None = None
    while True:
        request: dict[str, Any] = {"maxResults": 100}
        if token:
            request["nextToken"] = token
        response = lister(**request)
        for summary in response.get("bundles", []):
            if summary.get("bundleName") == name:
                return dict(summary)
        token = response.get("nextToken")
        if not token:
            return None


def latest_version(client: Any, bundle_id: str, branch_name: str | None = None) -> dict[str, Any]:
    """The bundle's current version on ``branch_name`` (mainline by default)."""
    request: dict[str, Any] = {"bundleId": bundle_id}
    if branch_name:
        request["branchName"] = branch_name
    response: dict[str, Any] = client.get_configuration_bundle(**request)
    return response


def update_system_prompt(
    client: Any,
    *,
    bundle_id: str,
    runtime_arn: str,
    system_prompt: str,
    commit_message: str,
    created_by: str | None = None,
    branch_name: str | None = None,
) -> dict[str, Any]:
    """Publish a new immutable version whose parent is the branch's current version."""
    parent = latest_version(client, bundle_id, branch_name)
    request: dict[str, Any] = {
        "bundleId": bundle_id,
        "components": {runtime_arn: {"configuration": {"system_prompt": system_prompt}}},
        "parentVersionIds": [parent["versionId"]],
        "commitMessage": commit_message[:500],
        "clientToken": str(uuid.uuid4()),
    }
    if branch_name:
        request["branchName"] = branch_name
    if created_by:
        request["createdBy"] = {"name": created_by}
    response = client.update_configuration_bundle(**request)
    return {
        "bundle_id": bundle_id,
        "bundle_arn": response["bundleArn"],
        "version_id": response["versionId"],
        "parent_version_id": parent["versionId"],
        "system_prompt_sha256": prompt_sha256(system_prompt),
    }


def list_versions(client: Any, bundle_id: str) -> list[dict[str, Any]]:
    """Every version, newest first, as content-free rows."""
    versions: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        request: dict[str, Any] = {"bundleId": bundle_id}
        if token:
            request["nextToken"] = token
        page = client.list_configuration_bundle_versions(**request)
        for item in page.get("versions") or []:
            lineage = item.get("lineageMetadata") or {}
            versions.append(
                {
                    "version_id": item.get("versionId"),
                    "created_at": str(item.get("versionCreatedAt") or ""),
                    "branch_name": lineage.get("branchName"),
                    "commit_message": lineage.get("commitMessage"),
                    "parent_version_ids": list(lineage.get("parentVersionIds") or []),
                }
            )
        token = page.get("nextToken")
        if not token:
            break
    versions.sort(key=lambda row: row["created_at"], reverse=True)
    return versions


def version_prompt(client: Any, bundle_id: str, version_id: str, runtime_arn: str) -> str:
    version = client.get_configuration_bundle_version(bundleId=bundle_id, versionId=version_id)
    return component_configuration(version, runtime_arn).system_prompt or ""


def diff_prompts(
    client: Any, bundle_id: str, runtime_arn: str, from_version: str, to_version: str
) -> str:
    """Unified diff of the system prompt between two versions."""
    before = version_prompt(client, bundle_id, from_version, runtime_arn)
    after = version_prompt(client, bundle_id, to_version, runtime_arn)
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{bundle_id}@{from_version}",
            tofile=f"{bundle_id}@{to_version}",
        )
    )
