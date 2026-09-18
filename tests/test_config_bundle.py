"""Configuration bundles: reference parsing, resolution, request scope, gate pinning."""

from __future__ import annotations

import json
from typing import Any

import pytest

from medical_nudging import config_bundle as cb
from medical_nudging.agents.prompt_builder import build_system_prompt

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abc123"
BUNDLE_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:configuration-bundle/prompt-a1b2c3d4e5"
)
V1 = "11111111-1111-1111-1111-111111111111"
V2 = "22222222-2222-2222-2222-222222222222"


class FakeControlPlane:
    """Just enough of bedrock-agentcore-control to exercise the module."""

    def __init__(self) -> None:
        self.versions: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.latest = V1
        self.add_version(V1, "You are a careful clinical assistant.", "Initial prompt", [])

    def add_version(self, version_id: str, prompt: str, message: str, parents: list[str]) -> None:
        self.versions[version_id] = {
            "bundleArn": BUNDLE_ARN,
            "bundleId": "prompt-a1b2c3d4e5",
            "versionId": version_id,
            "versionCreatedAt": f"2026-09-14T00:00:0{len(self.versions)}+00:00",
            "components": {RUNTIME_ARN: {"configuration": {"system_prompt": prompt}}},
            "lineageMetadata": {
                "branchName": "mainline",
                "commitMessage": message,
                "parentVersionIds": parents,
            },
        }
        self.latest = version_id

    def get_configuration_bundle_version(self, bundleId: str, versionId: str) -> dict[str, Any]:
        self.calls.append(f"get:{versionId}")
        if versionId not in self.versions:
            raise RuntimeError("ResourceNotFoundException")
        return self.versions[versionId]

    def get_configuration_bundle(
        self, bundleId: str, branchName: str | None = None
    ) -> dict[str, Any]:
        return self.versions[self.latest]

    def create_configuration_bundle(self, **request: Any) -> dict[str, Any]:
        self.calls.append("create")
        self.created = request
        return {"bundleArn": BUNDLE_ARN, "bundleId": "prompt-a1b2c3d4e5", "versionId": V1}

    def update_configuration_bundle(self, **request: Any) -> dict[str, Any]:
        self.calls.append("update")
        self.updated = request
        prompt = request["components"][RUNTIME_ARN]["configuration"]["system_prompt"]
        self.add_version(V2, prompt, request["commitMessage"], request["parentVersionIds"])
        return {"bundleArn": BUNDLE_ARN, "bundleId": "prompt-a1b2c3d4e5", "versionId": V2}

    def list_configuration_bundle_versions(
        self, bundleId: str, nextToken: str | None = None
    ) -> dict[str, Any]:
        return {"versions": list(self.versions.values())}


# --- reference and baggage -----------------------------------------------------


def test_bundle_ref_round_trips_through_baggage():
    ref = cb.BundleRef(BUNDLE_ARN, V1)
    assert ref.bundle_id == "prompt-a1b2c3d4e5"
    assert cb.bundle_ref_from_baggage(ref.baggage()) == ref


def test_baggage_parsing_tolerates_properties_encoding_and_other_keys():
    header = (
        f"trace=abc;prop=1, {cb.BAGGAGE_ARN_KEY}={BUNDLE_ARN}%20;x=y ,"
        f"{cb.BAGGAGE_VERSION_KEY}={V1}, broken, =empty"
    )
    assert cb.parse_baggage(header)[cb.BAGGAGE_ARN_KEY] == BUNDLE_ARN + " "
    assert cb.bundle_ref_from_baggage(header) is not None


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "trace=abc",
        f"{cb.BAGGAGE_ARN_KEY}={BUNDLE_ARN}",
        f"{cb.BAGGAGE_ARN_KEY}=arn/,{cb.BAGGAGE_VERSION_KEY}=v",
    ],
)
def test_missing_or_invalid_reference_means_repository_prompt(header):
    assert cb.bundle_ref_from_baggage(header) is None


def test_runtime_arn_from_otel_resource_attributes_strips_endpoint(monkeypatch):
    monkeypatch.delenv(cb.RUNTIME_ARN_ENV, raising=False)
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        f"service.name=x,cloud.resource_id={RUNTIME_ARN}/runtime-endpoint/DEFAULT",
    )
    assert cb.runtime_arn_from_environment() == RUNTIME_ARN
    monkeypatch.setenv(cb.RUNTIME_ARN_ENV, "arn:explicit/runtime/x")
    assert cb.runtime_arn_from_environment() == "arn:explicit/runtime/x"


# --- resolution ----------------------------------------------------------------


def test_resolver_selects_this_runtime_and_caches_per_version():
    plane = FakeControlPlane()
    resolver = cb.ConfigBundleResolver(runtime_arn=RUNTIME_ARN, client=plane)
    ref = cb.BundleRef(BUNDLE_ARN, V1)
    first = resolver.resolve(ref)
    second = resolver.resolve(ref)
    assert first is second
    assert plane.calls == [f"get:{V1}"]
    assert first.system_prompt == "You are a careful clinical assistant."
    provenance = first.provenance()
    assert provenance["bundle_version"] == V1
    assert provenance["applied_keys"] == ["system_prompt"]
    assert provenance["commit_message"] == "Initial prompt"
    assert provenance["system_prompt_sha256"] == cb.prompt_sha256(first.system_prompt)


def test_resolver_rejects_missing_component_and_unknown_version():
    plane = FakeControlPlane()
    other = cb.ConfigBundleResolver(runtime_arn="arn:other/runtime/zzz", client=plane)
    with pytest.raises(cb.ConfigBundleError, match="no configuration for"):
        other.resolve(cb.BundleRef(BUNDLE_ARN, V1))
    resolver = cb.ConfigBundleResolver(runtime_arn=RUNTIME_ARN, client=plane)
    with pytest.raises(cb.ConfigBundleError, match="could not fetch"):
        resolver.resolve(cb.BundleRef(BUNDLE_ARN, V2))


def test_resolver_needs_a_runtime_identity(monkeypatch):
    monkeypatch.delenv(cb.RUNTIME_ARN_ENV, raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    resolver = cb.ConfigBundleResolver(client=FakeControlPlane())
    with pytest.raises(cb.ConfigBundleError, match="Cannot select a bundle component"):
        resolver.resolve(cb.BundleRef(BUNDLE_ARN, V1))


def test_ignored_keys_are_reported_not_applied():
    version = {
        "bundleArn": BUNDLE_ARN,
        "versionId": V1,
        "components": {
            RUNTIME_ARN: {
                "configuration": {"system_prompt": "p", "model_id": "m", "temperature": 0.2}
            }
        },
    }
    config = cb.component_configuration(version, RUNTIME_ARN)
    assert config.provenance()["ignored_keys"] == ["model_id", "temperature"]


# --- request scope and prompt building ---------------------------------------


def test_prompt_builder_uses_bundle_prompt_inside_scope_and_repository_outside():
    plane = FakeControlPlane()
    config = cb.ConfigBundleResolver(runtime_arn=RUNTIME_ARN, client=plane).resolve(
        cb.BundleRef(BUNDLE_ARN, V1)
    )
    repository = cb.repository_base_prompt()
    assert cb.prompt_provenance() == {
        "prompt_source": "repository",
        "base_prompt_sha256": cb.prompt_sha256(repository),
        "config_bundle": None,
    }
    with cb.bundle_scope(config):
        prompt = build_system_prompt({}, runtime_config={"max_nudges": 3})
        assert prompt.startswith("You are a careful clinical assistant.")
        assert "Final nudge maximum: 3" in prompt
        provenance = cb.prompt_provenance()
        assert provenance["prompt_source"] == "config_bundle"
        assert provenance["config_bundle"]["bundle_version"] == V1
        assert (
            provenance["base_prompt_sha256"] == provenance["config_bundle"]["system_prompt_sha256"]
        )
    assert build_system_prompt({}).startswith(repository[:40])
    assert cb.active_bundle() is None


def test_bundle_without_prompt_falls_back_to_repository_but_is_still_recorded():
    config = cb.BundleConfig(ref=cb.BundleRef(BUNDLE_ARN, V1), component_arn=RUNTIME_ARN, values={})
    with cb.bundle_scope(config):
        provenance = cb.prompt_provenance()
    assert provenance["prompt_source"] == "repository"
    assert provenance["config_bundle"]["applied_keys"] == []


# --- control-plane helpers used by the CLI and Terraform ---------------------


def test_create_update_versions_and_diff():
    plane = FakeControlPlane()
    created = cb.create_bundle(
        plane,
        name="medical_nudging_prompt",
        runtime_arn=RUNTIME_ARN,
        system_prompt="You are a careful clinical assistant.",
        commit_message="Initial prompt",
    )
    assert created["version_id"] == V1
    assert plane.created["components"][RUNTIME_ARN]["configuration"] == {
        "system_prompt": "You are a careful clinical assistant."
    }
    updated = cb.update_system_prompt(
        plane,
        bundle_id="prompt-a1b2c3d4e5",
        runtime_arn=RUNTIME_ARN,
        system_prompt="You are a careful clinical assistant.\nCite the passage you relied on.",
        commit_message="Ask for the supporting passage",
        created_by="reviewer",
    )
    assert updated["parent_version_id"] == V1 and updated["version_id"] == V2
    assert plane.updated["parentVersionIds"] == [V1]
    assert plane.updated["createdBy"] == {"name": "reviewer"}
    rows = cb.list_versions(plane, "prompt-a1b2c3d4e5")
    assert [row["version_id"] for row in rows] == [V2, V1]
    assert rows[0]["parent_version_ids"] == [V1]
    diff = cb.diff_prompts(plane, "prompt-a1b2c3d4e5", RUNTIME_ARN, V1, V2)
    assert "+Cite the passage you relied on." in diff
    assert f"@{V1}" in diff and f"@{V2}" in diff


def test_cli_create_and_diff_print_json_and_diff(monkeypatch, capsys, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("config_bundle_cli", "scripts/config_bundle.py")
    cli = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(cli)
    plane = FakeControlPlane()
    monkeypatch.setattr(cli, "_client", lambda args: plane)
    out = tmp_path / "bundle.json"
    assert (
        cli.main(
            [
                "create",
                "--runtime-arn",
                RUNTIME_ARN,
                "--name",
                "medical_nudging_prompt",
                "--message",
                "Initial",
                "--output",
                str(out),
            ]
        )
        == 0
    )
    written = json.loads(out.read_text())
    assert written["bundle_id"] == "prompt-a1b2c3d4e5"
    assert written["baggage"].startswith(cb.BAGGAGE_ARN_KEY)
    prompt_file = tmp_path / "edited.md"
    prompt_file.write_text("You are a careful clinical assistant.\nNew line.")
    assert (
        cli.main(
            [
                "update",
                "--bundle-id",
                "prompt-a1b2c3d4e5",
                "--runtime-arn",
                RUNTIME_ARN,
                "--prompt-file",
                str(prompt_file),
                "--message",
                "Add a line",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "diff",
                "--bundle-id",
                "prompt-a1b2c3d4e5",
                "--runtime-arn",
                RUNTIME_ARN,
                "--from",
                V1,
                "--to",
                V2,
            ]
        )
        == 0
    )
    assert "+New line." in capsys.readouterr().out


def test_create_reuses_existing_bundle_for_recreated_runtime():
    """A second create with the same name publishes a version instead of failing."""
    client = FakeControlPlane()
    client.list_configuration_bundles = lambda **request: {  # type: ignore[attr-defined]
        "bundles": [
            {"bundleId": "prompt-a1b2c3d4e5", "bundleArn": BUNDLE_ARN, "bundleName": "taken"}
        ]
    }
    result = cb.create_bundle(
        client,
        name="taken",
        runtime_arn=RUNTIME_ARN,
        system_prompt="You are a careful clinical assistant.",
        commit_message="Initial system prompt (terraform apply)",
    )
    assert result["created"] is False
    assert result["bundle_id"] == "prompt-a1b2c3d4e5"
    assert result["parent_version_id"] == V1
    assert "create" not in client.calls
    assert "re-created runtime" in client.updated["commitMessage"]
    assert list(client.updated["components"]) == [RUNTIME_ARN]
