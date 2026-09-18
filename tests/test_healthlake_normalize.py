"""Tests for the HealthLake NDJSON normalization stage."""

import gzip
import json
from pathlib import Path

import pytest

from medical_nudging.healthlake.normalize import (
    MIMIC_HIGH_VOLUME_FILES,
    mimic_patient_ids,
    mimic_source_files,
    normalize_mimic,
    normalize_synthea,
    patient_id_of,
)


def _write_gz_ndjson(path: Path, resources: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for resource in resources:
            handle.write(json.dumps(resource) + "\n")


@pytest.fixture
def mimic_dir(tmp_path: Path) -> Path:
    source = tmp_path / "mimic"
    _write_gz_ndjson(
        source / "MimicPatient.ndjson.gz",
        [
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Patient", "id": "p2"},
        ],
    )
    _write_gz_ndjson(
        source / "MimicCondition.ndjson.gz",
        [
            {
                "resourceType": "Condition",
                "id": "c1",
                "subject": {"reference": "Patient/p1"},
            },
            {
                "resourceType": "Condition",
                "id": "c2",
                "subject": {"reference": "Patient/p2"},
            },
            {"resourceType": "Condition", "subject": {"reference": "Patient/p1"}},
        ],
    )
    _write_gz_ndjson(
        source / "MimicObservationChartevents.ndjson.gz",
        [
            {
                "resourceType": "Observation",
                "id": "o1",
                "subject": {"reference": "Patient/p1"},
            }
        ],
    )
    _write_gz_ndjson(
        source / "MimicOrganization.ndjson.gz",
        [{"resourceType": "Organization", "id": "org1"}],
    )
    return source


class TestNormalizeMimic:
    def test_gunzips_every_file(self, mimic_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        stats = normalize_mimic(mimic_dir, out)

        assert stats.files_written == 4
        assert {p.name for p in out.glob("*.ndjson")} == {
            "MimicPatient.ndjson",
            "MimicCondition.ndjson",
            "MimicObservationChartevents.ndjson",
            "MimicOrganization.ndjson",
        }

    def test_resources_without_an_id_are_skipped(self, mimic_dir: Path, tmp_path: Path):
        """Bulk import is upsert-by-id, so an id-less resource cannot be imported."""
        stats = normalize_mimic(mimic_dir, tmp_path / "ndjson")
        assert stats.resources_skipped_no_id == 1
        assert stats.resources_written == 6

    def test_core_subset_drops_the_high_volume_file(self, mimic_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        normalize_mimic(mimic_dir, out, subset="core")
        names = {p.name for p in out.glob("*.ndjson")}
        assert "MimicObservationChartevents.ndjson" not in names
        assert "MimicObservationChartevents" in MIMIC_HIGH_VOLUME_FILES

    def test_patient_filter_pre_filters_before_import(self, mimic_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        normalize_mimic(mimic_dir, out, patient_ids={"p1"})

        conditions = [
            json.loads(line)
            for line in (out / "MimicCondition.ndjson").read_text().splitlines()
            if line
        ]
        assert [c["id"] for c in conditions] == ["c1"]

        patients = [
            json.loads(line)
            for line in (out / "MimicPatient.ndjson").read_text().splitlines()
            if line
        ]
        assert [p["id"] for p in patients] == ["p1"]

    def test_patient_filter_keeps_shared_resources(self, mimic_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        normalize_mimic(mimic_dir, out, patient_ids={"p1"})
        assert (out / "MimicOrganization.ndjson").exists()

    def test_output_lines_are_single_resources(self, mimic_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        normalize_mimic(mimic_dir, out)
        for line in (out / "MimicCondition.ndjson").read_text().splitlines():
            resource = json.loads(line)
            assert resource["resourceType"] == "Condition"
            assert "entry" not in resource

    def test_missing_input_directory_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            normalize_mimic(tmp_path / "nope", tmp_path / "out")

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path: Path):
        source = tmp_path / "mimic"
        source.mkdir()
        with gzip.open(source / "MimicPatient.ndjson.gz", "wt", encoding="utf-8") as handle:
            handle.write('{"resourceType": "Patient", "id": "p1"}\n')
            handle.write("{not json}\n")
            handle.write('{"resourceType": "Patient", "id": "p2"}\n')

        stats = normalize_mimic(source, tmp_path / "out")
        assert stats.resources_written == 2

    def test_patient_ids_are_read_from_the_patient_file(self, mimic_dir: Path):
        assert mimic_patient_ids(mimic_dir) == {"p1", "p2"}

    def test_source_files_are_listed_in_stable_order(self, mimic_dir: Path):
        first = mimic_source_files(mimic_dir)
        assert first == mimic_source_files(mimic_dir)
        assert all(p.name.endswith(".ndjson.gz") for p in first)


# ---------------------------------------------------------------------------
# Synthea
# ---------------------------------------------------------------------------

PRACTITIONER_SYSTEM = "http://hl7.org/fhir/sid/us-npi"


@pytest.fixture
def synthea_dir(tmp_path: Path) -> Path:
    source = tmp_path / "synthea"
    source.mkdir()

    (source / "practitionerInformation1637345232350.json").write_text(
        json.dumps(
            {
                "resourceType": "Bundle",
                "type": "batch",
                "entry": [
                    {
                        "fullUrl": "urn:uuid:prac-1",
                        "resource": {
                            "resourceType": "Practitioner",
                            "id": "prac-1",
                            "identifier": [{"system": PRACTITIONER_SYSTEM, "value": "9999989959"}],
                        },
                    }
                ],
            }
        )
    )
    (source / "Alice123_Smith456_uuid.json").write_text(
        json.dumps(
            {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "fullUrl": "urn:uuid:pat-1",
                        "resource": {"resourceType": "Patient", "id": "pat-1"},
                        "request": {"method": "POST", "url": "Patient"},
                    },
                    {
                        "fullUrl": "urn:uuid:enc-1",
                        "resource": {
                            "resourceType": "Encounter",
                            "id": "enc-1",
                            "subject": {"reference": "urn:uuid:pat-1"},
                            "participant": [
                                {
                                    "individual": {
                                        "reference": (
                                            f"Practitioner?identifier={PRACTITIONER_SYSTEM}"
                                            "|9999989959"
                                        )
                                    }
                                }
                            ],
                        },
                        "request": {"method": "POST", "url": "Encounter"},
                    },
                    {
                        "fullUrl": "urn:uuid:cond-1",
                        "resource": {
                            "resourceType": "Condition",
                            "id": "cond-1",
                            "subject": {"reference": "urn:uuid:pat-1"},
                            "encounter": {"reference": "urn:uuid:missing"},
                        },
                        "request": {"method": "POST", "url": "Condition"},
                    },
                ],
            }
        )
    )
    return source


class TestNormalizeSynthea:
    def test_bundles_are_unwrapped_into_per_type_ndjson(self, synthea_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        stats = normalize_synthea(synthea_dir, out)

        assert {p.name for p in out.glob("*.ndjson")} == {
            "Patient.ndjson",
            "Encounter.ndjson",
            "Condition.ndjson",
            "Practitioner.ndjson",
        }
        assert stats.resources_written == 4
        for line in (out / "Encounter.ndjson").read_text().splitlines():
            assert "request" not in json.loads(line)

    def test_urn_uuid_references_are_rewritten(self, synthea_dir: Path, tmp_path: Path):
        out = tmp_path / "ndjson"
        stats = normalize_synthea(synthea_dir, out)

        encounter = json.loads((out / "Encounter.ndjson").read_text().strip())
        assert encounter["subject"]["reference"] == "Patient/pat-1"
        assert stats.urn_references_rewritten >= 2

    def test_conditional_references_are_resolved_from_the_companion_bundle(
        self, synthea_dir: Path, tmp_path: Path
    ):
        out = tmp_path / "ndjson"
        stats = normalize_synthea(synthea_dir, out)

        encounter = json.loads((out / "Encounter.ndjson").read_text().strip())
        reference = encounter["participant"][0]["individual"]["reference"]
        assert reference == "Practitioner/prac-1"
        assert stats.conditional_references_resolved == 1
        assert stats.residual_conditional_references == 0

    def test_unresolvable_references_are_left_alone_and_counted(
        self, synthea_dir: Path, tmp_path: Path
    ):
        """Dangling references import fine; they are reported, not repaired or dropped."""
        out = tmp_path / "ndjson"
        stats = normalize_synthea(synthea_dir, out)

        condition = json.loads((out / "Condition.ndjson").read_text().strip())
        assert condition["encounter"]["reference"] == "urn:uuid:missing"
        assert stats.residual_urn_references == 1

    def test_companion_bundles_are_processed_even_when_limited(
        self, synthea_dir: Path, tmp_path: Path
    ):
        out = tmp_path / "ndjson"
        normalize_synthea(synthea_dir, out, limit=1)
        assert (out / "Practitioner.ndjson").exists()

    def test_missing_input_directory_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            normalize_synthea(tmp_path / "nope", tmp_path / "out")


class TestPatientIdOf:
    def test_patient_resource_returns_its_own_id(self):
        assert patient_id_of({"resourceType": "Patient", "id": "p1"}) == "p1"

    def test_subject_reference(self):
        resource = {"resourceType": "Condition", "subject": {"reference": "Patient/p9"}}
        assert patient_id_of(resource) == "p9"

    def test_patient_reference(self):
        resource = {"resourceType": "Observation", "patient": {"reference": "Patient/p8"}}
        assert patient_id_of(resource) == "p8"

    def test_non_patient_reference_returns_none(self):
        resource = {"resourceType": "Observation", "subject": {"reference": "Group/g1"}}
        assert patient_id_of(resource) is None
