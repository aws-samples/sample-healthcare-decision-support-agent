"""Tests for bounded FHIR query tools."""

from medical_nudging.search.fhir_client import FHIRClientError
from medical_nudging.tools.fhir_query import _summarize_resource, create_fhir_query_tool


def _text(result):
    if isinstance(result, dict):
        return result["content"][0]["text"]
    return result


class FakeFHIRClient:
    def __init__(self):
        self.search_calls = []

    def read(self, resource_type, resource_id):
        return {"resourceType": resource_type, "id": resource_id}

    def search(self, resource_type, params, max_pages=None):
        self.search_calls.append((resource_type, params, max_pages))
        return [
            {
                "resourceType": "Observation",
                "code": {"text": "Creatinine"},
                "effectiveDateTime": "2026-08-01T00:00:00Z",
                "valueQuantity": {"value": 1.4, "unit": "mg/dL"},
            },
            {
                "resourceType": "Observation",
                "code": {"text": "Creatinine"},
                "effectiveDateTime": "2024-01-01T00:00:00Z",
                "valueQuantity": {"value": 0.9, "unit": "mg/dL"},
            },
        ]


def test_fhir_tool_caps_calls_results_and_recent_history():
    client = FakeFHIRClient()
    query = create_fhir_query_tool(
        client,
        max_calls=1,
        max_resource_types_per_call=2,
        max_results_per_resource=2,
        recent_history_days=365,
    )

    first = _text(query(patient_id="p1", resource_types=["Observation"]))
    second = _text(query(patient_id="p1", resource_types=["Observation"]))

    assert "1.4 mg/dL" in first
    assert "0.9 mg/dL" not in first
    assert "History window excluded 1 older Observation" in first
    assert client.search_calls[0][1]["_count"] == "3"  # cap + truncation sentinel
    assert client.search_calls[0][2] == 1
    assert "BUDGET_EXHAUSTED" in second
    assert len(client.search_calls) == 1


def test_fhir_tool_allows_explicit_older_history_escape_hatch():
    client = FakeFHIRClient()
    query = create_fhir_query_tool(client, recent_history_days=365)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["Observation"],
            include_older_history=True,
        )
    )

    assert "1.4 mg/dL" in result
    assert "0.9 mg/dL" in result


def test_fhir_tool_rejects_too_many_resource_types_without_querying():
    client = FakeFHIRClient()
    query = create_fhir_query_tool(client, max_resource_types_per_call=1)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["Observation", "Encounter"],
        )
    )

    assert "BUDGET_EXCEEDED" in result
    assert client.search_calls == []


def test_fhir_tool_normalizes_scalar_params_and_drops_nulls():
    client = FakeFHIRClient()
    query = create_fhir_query_tool(client, max_results_per_resource=10)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["Observation"],
            params={"_count": 15, "status": None, "active": True},
        )
    )

    assert "Validation failed" not in result
    assert client.search_calls[0][1]["_count"] == "11"  # cap + truncation sentinel
    assert client.search_calls[0][1]["active"] == "true"
    assert "status" not in client.search_calls[0][1]


def test_fhir_tool_can_anchor_recent_history_to_latest_shifted_resource():
    client = FakeFHIRClient()
    client.search = lambda resource_type, params, max_pages=None: [
        {
            "resourceType": "Observation",
            "code": {"text": "Creatinine"},
            "effectiveDateTime": "2142-08-01T00:00:00Z",
            "valueQuantity": {"value": 1.4, "unit": "mg/dL"},
        },
        {
            "resourceType": "Observation",
            "code": {"text": "Creatinine"},
            "effectiveDateTime": "2100-01-01T00:00:00Z",
            "valueQuantity": {"value": 0.9, "unit": "mg/dL"},
        },
    ]
    query = create_fhir_query_tool(
        client,
        recent_history_days=365,
        history_anchor="latest_resource",
    )

    result = _text(query(patient_id="p1", resource_types=["Observation"]))

    assert "1.4 mg/dL" in result
    assert "0.9 mg/dL" not in result
    assert "History window excluded 1 older Observation" in result


def test_fhir_tool_retries_resource_after_incompatible_shared_params():
    client = FakeFHIRClient()

    def search(resource_type, params, max_pages=None):
        client.search_calls.append((resource_type, params, max_pages))
        if "date" in params:
            raise FHIRClientError("FHIR search MedicationRequest failed: HTTP 400", status_code=400)
        return []

    client.search = search
    query = create_fhir_query_tool(client, max_results_per_resource=15)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["MedicationRequest"],
            params={"date": "ge2142-05-10", "_sort": "date"},
        )
    )

    assert len(client.search_calls) == 2
    # v4: no default status filter — MIMIC MedicationRequests never match status=active,
    # so the filter silently hid every order (root cause found in the cohort audit).
    assert client.search_calls[1][1] == {
        "patient": "p1",
        "_count": "16",  # cap + truncation sentinel
    }
    assert "server rejected shared search parameters" in result


def test_fhir_tool_reports_truncation_when_matches_exceed_cap():
    client = FakeFHIRClient()
    client.search = lambda resource_type, params, max_pages=None: [
        {
            "resourceType": "Observation",
            "code": {"text": "Hematocrit"},
            "effectiveDateTime": f"2026-08-0{i}T00:00:00Z",
            "valueQuantity": {"value": 30 - i, "unit": "%"},
        }
        for i in range(1, 4)
    ]
    query = create_fhir_query_tool(client, max_results_per_resource=2)

    result = _text(query(patient_id="p1", resource_types=["Observation"]))

    assert "(2 results)" in result
    assert "MORE MATCHES EXIST" in result


def test_fhir_tool_filters_display_name_codes_client_side():
    client = FakeFHIRClient()
    client.search = lambda resource_type, params, max_pages=None: [
        {
            "resourceType": "Observation",
            "code": {"coding": [{"code": "51221", "display": "Hematocrit"}]},
            "effectiveDateTime": "2026-08-01T00:00:00Z",
            "valueQuantity": {"value": 24.2, "unit": "%"},
        },
        {
            "resourceType": "Observation",
            "code": {"coding": [{"code": "50912", "display": "Creatinine"}]},
            "effectiveDateTime": "2026-08-01T00:00:00Z",
            "valueQuantity": {"value": 0.8, "unit": "mg/dL"},
        },
    ] * (client.search_calls.append((resource_type, params, max_pages)) or 1)
    query = create_fhir_query_tool(client, max_results_per_resource=15)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["Observation"],
            params={"code": "Hematocrit,INR(PT)"},
        )
    )

    sent_params = client.search_calls[0][1]
    assert "code" not in sent_params
    assert sent_params["_count"] == "100"
    assert client.search_calls[0][2] == 3
    assert "Hematocrit: 24.2 %" in result
    assert "Creatinine" not in result
    assert "matched by name client-side" in result


def test_fhir_tool_name_filter_matches_medication_codeable_concept():
    client = FakeFHIRClient()
    client.search = lambda resource_type, params, max_pages=None: [
        {
            "resourceType": "MedicationAdministration",
            "medicationCodeableConcept": {"coding": [{"code": "WARF5", "display": "Warfarin 5mg"}]},
            "effectiveDateTime": "2026-08-01T00:00:00Z",
        },
        {
            "resourceType": "MedicationAdministration",
            "medicationCodeableConcept": {
                "coding": [{"code": "NACLFLUSH", "display": "NaCl Flush"}]
            },
            "effectiveDateTime": "2026-08-01T00:00:00Z",
        },
    ]
    query = create_fhir_query_tool(client, max_results_per_resource=15)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["MedicationAdministration"],
            params={"code": "Warfarin"},
        )
    )

    assert "Warfarin 5mg" in result
    assert "NaCl Flush" not in result


def test_fhir_tool_passes_token_codes_to_server():
    client = FakeFHIRClient()
    query = create_fhir_query_tool(client, max_results_per_resource=15)

    query(
        patient_id="p1",
        resource_types=["Observation"],
        params={"code": "http://loinc.org|718-7"},
    )

    assert client.search_calls[0][1]["code"] == "http://loinc.org|718-7"


def test_fhir_tool_preserves_repeated_search_params():
    client = FakeFHIRClient()
    query = create_fhir_query_tool(client)

    result = _text(
        query(
            patient_id="p1",
            resource_types=["Observation"],
            params={"date": ["ge2142-05-10", "le2142-05-16"]},
        )
    )

    assert "Validation failed" not in result
    assert client.search_calls[0][1]["date"] == ["ge2142-05-10", "le2142-05-16"]


# ---------------------------------------------------------------------------
# Composition from settings (the FHIR data source has one home)
# ---------------------------------------------------------------------------


def test_from_config_is_none_when_data_source_disabled():
    from medical_nudging import config
    from medical_nudging.tools.fhir_query import create_fhir_query_tool_from_config

    config.override({"fhir_api": {"enabled": False, "datastore_endpoint": "https://x/r4/"}})

    assert create_fhir_query_tool_from_config() is None


def test_from_config_is_none_without_endpoint(caplog):
    from medical_nudging import config
    from medical_nudging.tools.fhir_query import create_fhir_query_tool_from_config

    config.override({"fhir_api": {"enabled": True, "datastore_endpoint": ""}})

    assert create_fhir_query_tool_from_config() is None
    assert "no HealthLake datastore_endpoint" in caplog.text


def test_from_config_degrades_to_none_when_client_cannot_be_built(caplog):
    from unittest.mock import patch

    from medical_nudging import config
    from medical_nudging.search.fhir_auth import FHIRAuthError
    from medical_nudging.tools.fhir_query import create_fhir_query_tool_from_config

    config.override({"fhir_api": {"enabled": True, "datastore_endpoint": "https://x/r4/"}})

    with patch(
        "medical_nudging.tools.fhir_query.create_healthlake_client",
        side_effect=FHIRAuthError("no credentials"),
    ):
        assert create_fhir_query_tool_from_config() is None
    assert "FHIR tool creation failed: no credentials" in caplog.text


def test_from_config_builds_tool_with_request_limits(monkeypatch):
    from unittest.mock import patch

    from medical_nudging import config
    from medical_nudging.tools.fhir_query import create_fhir_query_tool_from_config

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    config.override(
        {"fhir_api": {"enabled": True, "datastore_endpoint": "https://x/r4/", "max_pages": 2}}
    )
    fake = FakeFHIRClient()

    with patch(
        "medical_nudging.tools.fhir_query.create_healthlake_client", return_value=fake
    ) as factory:
        query = create_fhir_query_tool_from_config(
            {"fhir_query": {"max_calls": 1, "max_results_per_resource": 1}}
        )

    factory.assert_called_once_with(
        datastore_endpoint="https://x/r4/", region="us-east-1", max_pages=2
    )
    assert query is not None
    first = _text(query(patient_id="p1", resource_types=["Observation"]))
    second = _text(query(patient_id="p1", resource_types=["Observation"]))
    assert "Result cap: showing 1 Observation" in first
    assert "BUDGET_EXHAUSTED" in second


# ---------------------------------------------------------------------------
# Per-resource summaries, through the tool interface
# ---------------------------------------------------------------------------


class ResourceFakeClient:
    """Returns one canned resource per requested type; the tool summarizes it."""

    RESOURCES = {
        "Condition": {
            "resourceType": "Condition",
            "code": {"coding": [{"code": "E11", "display": "Type 2 diabetes"}]},
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "onsetDateTime": "2020-01-01",
        },
        "MedicationRequest": {
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": {"text": "Metformin 500 mg"},
            "status": "active",
        },
        "MedicationStatement": {
            "resourceType": "MedicationStatement",
            "medicationReference": {"display": "Lisinopril 10 mg"},
            "status": "completed",
        },
        "MedicationDispense": {
            "resourceType": "MedicationDispense",
            "medicationCodeableConcept": {"coding": [{"code": "RX1"}]},
            "whenHandedOver": "2026-01-05",
        },
        "Encounter": {
            "resourceType": "Encounter",
            "type": [{"text": "Emergency visit"}],
            "period": {"start": "2026-02-01T08:00:00Z"},
        },
        "Procedure": {
            "resourceType": "Procedure",
            "code": {"text": "Appendectomy"},
            "performedPeriod": {"start": "2025-12-24"},
        },
        "AllergyIntolerance": {
            "resourceType": "AllergyIntolerance",
            "code": {"text": "Penicillin"},
            "clinicalStatus": {"text": "confirmed"},
        },
        "Immunization": {
            "resourceType": "Immunization",
            "vaccineCode": {"text": "Influenza"},
            "occurrenceDateTime": "2025-10-01",
        },
    }

    def read(self, resource_type, resource_id):
        return {
            "resourceType": "Patient",
            "id": resource_id,
            "name": [{"given": ["Ada"], "family": "Lovelace"}],
            "gender": "female",
            "birthDate": "1815-12-10",
        }

    def search(self, resource_type, params, max_pages=None):
        if resource_type == "Observation":
            return [{"resourceType": "OperationOutcome", "issue": [{"severity": "warning"}]}]
        return [self.RESOURCES[resource_type]]


def test_summaries_cover_every_supported_resource_type():
    query = create_fhir_query_tool(ResourceFakeClient())

    text = _text(
        query(
            patient_id="p1",
            resource_types=[
                "Patient",
                "Condition",
                "MedicationRequest",
                "MedicationStatement",
                "MedicationDispense",
                "Encounter",
                "Procedure",
                "AllergyIntolerance",
                "Immunization",
            ],
        )
    )

    assert "- Ada Lovelace, female, DOB: 1815-12-10" in text
    assert "- Type 2 diabetes (status: active) onset: 2020-01-01" in text
    assert "- Metformin 500 mg (status: active)" in text
    assert "- Lisinopril 10 mg (status: completed)" in text
    assert "- RX1 (2026-01-05)" in text
    assert "- Emergency visit (2026-02-01T08:00:00Z)" in text
    assert "- Appendectomy (2025-12-24)" in text
    assert "- Penicillin (status: confirmed)" in text
    assert "- Influenza (2025-10-01)" in text


def test_unexpected_resource_type_falls_back_to_compact_json():
    query = create_fhir_query_tool(ResourceFakeClient())

    text = _text(query(patient_id="p1", resource_types=["Observation"]))

    assert '- {"resourceType": "OperationOutcome"' in text


def test_per_resource_failures_become_error_sections():
    class FailingClient(ResourceFakeClient):
        def search(self, resource_type, params, max_pages=None):
            if resource_type == "Condition":
                raise RuntimeError("datastore unavailable")
            return super().search(resource_type, params, max_pages)

    query = create_fhir_query_tool(FailingClient())

    text = _text(query(patient_id="p1", resource_types=["Condition", "Immunization"]))

    assert "### Condition\nError: datastore unavailable" in text
    assert "- Influenza (2025-10-01)" in text


# One-line summaries pinned per resource type and per optional-field branch so the
# agent-visible tool text stays byte-identical across refactors.
SUMMARY_CASES = [
    (
        {
            "resourceType": "Patient",
            "name": [{"given": ["Ada", "K"], "family": "Lovelace"}],
            "birthDate": "1815-12-10",
            "gender": "female",
        },
        "Ada K Lovelace, female, DOB: 1815-12-10",
    ),
    ({"resourceType": "Patient"}, "Unknown, unknown, DOB: unknown DOB"),
    (
        {
            "resourceType": "Condition",
            "code": {"text": "Type 2 diabetes"},
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "onsetDateTime": "2020-01-01",
        },
        "Type 2 diabetes (status: active) onset: 2020-01-01",
    ),
    (
        {"resourceType": "Condition", "code": {"coding": [{"display": "Sepsis"}]}},
        "Sepsis (status: unknown)",
    ),
    (
        {
            "resourceType": "Observation",
            "code": {"text": "Creatinine"},
            "valueQuantity": {"value": 1.4, "unit": "mg/dL"},
            "effectiveDateTime": "2026-01-02",
        },
        "Creatinine: 1.4 mg/dL (2026-01-02)",
    ),
    ({"resourceType": "Observation", "code": {"text": "Note"}}, "Note: no value"),
    (
        {
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": {"text": "Metformin 500 mg"},
            "status": "active",
        },
        "Metformin 500 mg (status: active)",
    ),
    (
        {
            "resourceType": "MedicationRequest",
            "medicationReference": {"reference": "Medication/1"},
            "status": "stopped",
        },
        "Medication/1 (status: stopped)",
    ),
    (
        {
            "resourceType": "MedicationStatement",
            "medicationReference": {"display": "Lisinopril", "reference": "Medication/2"},
        },
        "Lisinopril (status: )",
    ),
    (
        {
            "resourceType": "MedicationAdministration",
            "medicationCodeableConcept": {"text": "Heparin"},
            "effectiveDateTime": "2026-01-03",
        },
        "Heparin (2026-01-03)",
    ),
    (
        {
            "resourceType": "MedicationDispense",
            "medicationReference": {"reference": "Medication/3"},
            "whenHandedOver": "2026-01-05",
        },
        "Medication/3 (2026-01-05)",
    ),
    ({"resourceType": "MedicationDispense"}, "Unknown"),
    (
        {
            "resourceType": "Encounter",
            "type": [{"text": "Emergency visit"}],
            "period": {"start": "2026-02-01T08:00:00Z"},
        },
        "Emergency visit (2026-02-01T08:00:00Z)",
    ),
    ({"resourceType": "Encounter"}, "encounter"),
    (
        {
            "resourceType": "Procedure",
            "code": {"text": "Appendectomy"},
            "performedDateTime": "2025-12-24",
        },
        "Appendectomy (2025-12-24)",
    ),
    (
        {
            "resourceType": "Procedure",
            "code": {"text": "Dialysis"},
            "performedPeriod": {"start": "2025-12-25", "end": "2025-12-26"},
        },
        "Dialysis (2025-12-25)",
    ),
    ({"resourceType": "Procedure"}, "Unknown"),
    (
        {
            "resourceType": "AllergyIntolerance",
            "code": {"text": "Penicillin"},
            "clinicalStatus": {"coding": [{"code": "confirmed"}]},
        },
        "Penicillin (status: confirmed)",
    ),
    ({"resourceType": "AllergyIntolerance"}, "Unknown (status: unknown)"),
    (
        {
            "resourceType": "Immunization",
            "vaccineCode": {"text": "Influenza"},
            "occurrenceDateTime": "2025-10-01",
        },
        "Influenza (2025-10-01)",
    ),
    ({"resourceType": "Immunization"}, "Unknown"),
    (
        {"resourceType": "OperationOutcome", "issue": [{"severity": "warning"}]},
        '{"resourceType": "OperationOutcome", "issue": [{"severity": "warning"}]}',
    ),
    ({"id": "x"}, '{"id": "x"}'),
]


def test_summarize_resource_matches_pinned_one_liners():
    for resource, expected in SUMMARY_CASES:
        assert _summarize_resource(resource) == expected, resource
