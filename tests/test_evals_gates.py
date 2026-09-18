"""Unit tests for the `evals` sanity-gate harness.

Everything here runs offline against hand-built fixtures -- no model calls, no
AWS. The point of these tests is that a gate's verdict is pinned to a specific
saved-artifact shape, so a change in what the pipeline writes shows up as a test
failure rather than as a silently mis-scored baseline.
"""

import json

import pytest
from strands_evals.types.evaluation import EvaluationData

from evals.catalog import GuidelineCatalog, normalize_source
from evals.gates import CitationPresent, ExecutionHealth, OutputFormatSuccess, ReviewerReady

CATALOG_ENTRIES = [
    {"source": "ATS_IDSA_CAP_2019", "title": "ATS/IDSA Community-Acquired Pneumonia 2019"},
    {"source": "KDIGO_AKI_2012", "title": "KDIGO Acute Kidney Injury 2012"},
]


def make_case(output=None, trace=None, sample_id="sample-1"):
    return EvaluationData(
        name=sample_id,
        input={"sample_id": sample_id},
        actual_output=output if output is not None else {},
        metadata={"trace": trace},
    )


def nudge(grounding="guideline", source="ATS_IDSA_CAP_2019", citation=True):
    payload = {
        "title": "Consider de-escalation",
        "description": "Narrow antibiotics after culture review.",
        "rationale": "Cultures identify a susceptible organism.",
        "grounding": grounding,
    }
    if citation:
        payload["guideline_citation"] = {"source": source, "section": "5.2", "page": 12}
    return payload


@pytest.fixture
def catalog():
    return GuidelineCatalog(CATALOG_ENTRIES)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestGuidelineCatalog:
    def test_normalize_folds_punctuation_and_case(self):
        assert normalize_source("ATS/IDSA CAP 2019") == normalize_source("ats_idsa_cap_2019")

    def test_exact_source_match(self, catalog):
        assert catalog.contains("KDIGO_AKI_2012")

    def test_matches_across_separator_styles(self, catalog):
        assert catalog.contains("ATS/IDSA CAP 2019")

    def test_matches_title(self, catalog):
        assert catalog.contains("KDIGO Acute Kidney Injury 2012")

    def test_rejects_unknown_source(self, catalog):
        assert not catalog.contains("Surviving Sepsis Campaign 2021")

    def test_rejects_blank(self, catalog):
        assert not catalog.contains("   ")

    def test_missing_file_yields_empty_catalog(self, tmp_path):
        loaded = GuidelineCatalog.load(tmp_path / "nope.json")
        assert loaded.is_empty

    def test_malformed_file_yields_empty_catalog(self, tmp_path):
        path = tmp_path / "catalog.json"
        path.write_text("{not json")
        assert GuidelineCatalog.load(path).is_empty

    def test_loads_from_file(self, tmp_path):
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps({"guidelines": CATALOG_ENTRIES}))
        loaded = GuidelineCatalog.load(path)
        assert len(loaded) == 2
        assert loaded.contains("ATS_IDSA_CAP_2019")


# ---------------------------------------------------------------------------
# Gate 1 -- output format success
# ---------------------------------------------------------------------------


class TestOutputFormatSuccess:
    def test_success_passes(self):
        outputs = OutputFormatSuccess().evaluate(make_case({"status": "success", "nudges": []}))
        assert [o.test_pass for o in outputs] == [True]
        assert outputs[0].label == "output_format_ok"

    def test_partial_passes(self):
        outputs = OutputFormatSuccess().evaluate(make_case({"status": "partial", "nudges": []}))
        assert outputs[0].test_pass

    def test_structured_output_error_fails(self):
        case = make_case(
            {
                "status": "error",
                "error": "Structured output failed: max tokens reached before tool call",
            }
        )
        outputs = OutputFormatSuccess().evaluate(case)
        assert outputs[0].test_pass is False
        assert outputs[0].label == "output_format_failure"

    def test_output_format_stage_marker_fails(self):
        case = make_case({"status": "error", "error": "boom", "stage": "output_format"})
        assert OutputFormatSuccess().evaluate(case)[0].test_pass is False

    def test_stage_marker_nested_in_metadata_fails(self):
        case = make_case(
            {"status": "error", "error": "boom", "metadata": {"stage": "output_format"}}
        )
        assert OutputFormatSuccess().evaluate(case)[0].test_pass is False

    def test_non_format_error_passes_this_gate(self):
        """A tool blowing up is gate 3's problem, not a format failure."""
        case = make_case({"status": "error", "error": "FHIR search Condition failed: HTTP 403"})
        outputs = OutputFormatSuccess().evaluate(case)
        assert outputs[0].test_pass is True

    def test_missing_output_passes(self):
        assert OutputFormatSuccess().evaluate(make_case(None))[0].test_pass is True


# ---------------------------------------------------------------------------
# Gate 2 -- citation present
# ---------------------------------------------------------------------------


class TestCitationPresent:
    def test_guideline_nudge_with_known_source_passes(self, catalog):
        case = make_case({"status": "success", "nudges": [nudge()]})
        outputs = CitationPresent(catalog=catalog).evaluate(case)
        assert [o.label for o in outputs] == ["citation_present"]

    def test_guideline_nudge_without_citation_fails(self, catalog):
        case = make_case({"status": "success", "nudges": [nudge(citation=False)]})
        outputs = CitationPresent(catalog=catalog).evaluate(case)
        assert outputs[0].test_pass is False
        assert outputs[0].label == "citation_missing"

    def test_guideline_nudge_with_blank_source_fails(self, catalog):
        case = make_case({"status": "success", "nudges": [nudge(source="  ")]})
        assert CitationPresent(catalog=catalog).evaluate(case)[0].label == "citation_missing"

    def test_unknown_source_fails(self, catalog):
        case = make_case({"status": "success", "nudges": [nudge(source="Made_Up_Guideline_2030")]})
        outputs = CitationPresent(catalog=catalog).evaluate(case)
        assert outputs[0].test_pass is False
        assert outputs[0].label == "citation_unknown_source"

    def test_clinical_reasoning_nudge_is_exempt(self, catalog):
        case = make_case(
            {"status": "success", "nudges": [nudge(grounding="clinical_reasoning", citation=False)]}
        )
        outputs = CitationPresent(catalog=catalog).evaluate(case)
        assert outputs[0].test_pass is True
        assert outputs[0].label == "not_guideline_grounded"

    def test_one_output_per_nudge(self, catalog):
        case = make_case(
            {
                "status": "success",
                "nudges": [nudge(), nudge(citation=False), nudge(grounding="clinical_reasoning")],
            }
        )
        gate = CitationPresent(catalog=catalog)
        outputs = gate.evaluate(case)
        assert len(outputs) == 3
        score, test_pass, _ = gate.aggregator(outputs)
        assert test_pass is False
        assert score == pytest.approx(2 / 3)

    def test_no_nudges_has_no_citations_to_check(self, catalog):
        outputs = CitationPresent(catalog=catalog).evaluate(make_case({"status": "success"}))
        assert outputs[0].label == "no_guideline_citations"
        assert outputs[0].test_pass is True

    def test_empty_catalog_reports_unverified_rather_than_failing(self):
        case = make_case({"status": "success", "nudges": [nudge(source="Anything_At_All")]})
        outputs = CitationPresent(catalog=GuidelineCatalog([])).evaluate(case)
        assert outputs[0].test_pass is True
        assert outputs[0].label == "citation_present_unverified"


# ---------------------------------------------------------------------------
# Gate 3 -- execution health
# ---------------------------------------------------------------------------


class TestExecutionHealth:
    def test_clean_run_passes(self):
        trace = {
            "tool_calls": [
                {"tool_name": "search_guidelines", "success": True, "error": None},
                {"tool_name": "query_patient_fhir", "success": True, "error": None},
            ],
            "messages": [],
        }
        gate = ExecutionHealth()
        outputs = gate.evaluate(make_case({"status": "success"}, trace))
        assert len(outputs) == 3
        score, test_pass, _ = gate.aggregator(outputs)
        assert test_pass is True
        assert score == pytest.approx(1.0)

    def test_run_error_fails(self):
        outputs = ExecutionHealth().evaluate(make_case({"status": "error", "error": "boom"}))
        assert outputs[0].label == "run_error"
        assert outputs[0].test_pass is False

    def test_success_status_with_error_string_still_fails(self):
        outputs = ExecutionHealth().evaluate(make_case({"status": "success", "error": "boom"}))
        assert outputs[0].test_pass is False

    def test_failed_tool_call_fails(self):
        trace = {
            "tool_calls": [{"tool_name": "search_guidelines", "success": False, "error": "429"}]
        }
        gate = ExecutionHealth()
        outputs = gate.evaluate(make_case({"status": "success"}, trace))
        _, test_pass, reason = gate.aggregator(outputs)
        assert test_pass is False
        assert "search_guidelines" in reason

    def test_tool_error_string_without_success_false_fails(self):
        trace = {"tool_calls": [{"tool_name": "t", "success": True, "error": "timeout"}]}
        outputs = ExecutionHealth().evaluate(make_case({"status": "success"}, trace))
        assert any(o.label == "tool_error" for o in outputs)

    def test_error_tool_result_in_messages_fails(self):
        """A tool can hand the model an error payload while the callback says success."""
        trace = {
            "tool_calls": [{"tool_name": "query_patient_fhir", "success": True, "error": None}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "tu-1",
                                "status": "error",
                                "content": [{"text": "HTTP 403"}],
                            }
                        }
                    ],
                }
            ],
        }
        gate = ExecutionHealth()
        outputs = gate.evaluate(make_case({"status": "success"}, trace))
        _, test_pass, _ = gate.aggregator(outputs)
        assert test_pass is False
        assert any(o.label == "tool_result_error" for o in outputs)

    def test_missing_trace_scores_run_only(self):
        outputs = ExecutionHealth().evaluate(make_case({"status": "success"}, None))
        assert len(outputs) == 1
        assert outputs[0].test_pass is True

    def test_malformed_trace_does_not_raise(self):
        trace = {"tool_calls": "not-a-list", "messages": [None, {"content": "nope"}]}
        outputs = ExecutionHealth().evaluate(make_case({"status": "success"}, trace))
        assert len(outputs) == 1

    def test_incomplete_tool_call_fails(self):
        trace = {
            "tool_calls": [
                {
                    "tool_name": "search_guidelines",
                    "completed": False,
                    "success": True,
                    "error": None,
                }
            ]
        }
        outputs = ExecutionHealth().evaluate(make_case({"status": "success"}, trace))
        assert any(o.label == "tool_incomplete" for o in outputs)


# ---------------------------------------------------------------------------
# Gate 4 -- ready for clinician review
# ---------------------------------------------------------------------------


def review_ready_output():
    return {
        "format": "fhir_api",
        "status": "success",
        "key_findings": [
            "Acute kidney injury complicates current antibiotic dosing",
            "Creatinine increased from 1.0 to 2.1 mg/dL",
            "Culture results permit narrower antimicrobial therapy",
        ],
        "patient_summary": (
            "Hospitalized adult with pneumonia, acute kidney injury, and improving "
            "hemodynamics. Creatinine increased from 1.0 to 2.1 mg/dL while receiving "
            "renally cleared antibiotics. Final cultures identify a susceptible organism, "
            "which permits treatment narrowing and renal dose review during this admission."
            " today."
        ),
        "nudges": [nudge()],
    }


def review_ready_trace():
    return {
        "tool_calls": [
            {
                "tool_name": "query_patient_fhir",
                "tool_use_id": "fhir-1",
                "completed": True,
                "success": True,
            },
            {
                "tool_name": "search_guidelines",
                "tool_use_id": "search-1",
                "completed": True,
                "success": True,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "search-1",
                            "status": "success",
                            "content": [
                                {
                                    "text": (
                                        "=== ATS_IDSA_CAP_2019 ===\n"
                                        "Section: Antibiotic selection\n"
                                        "Page: 12\n\nUse culture results."
                                    )
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }


def review_ready_config():
    return {
        "context": {"data_source": "fhir_api"},
        "agent": {
            "max_nudges": 3,
            "tool_limits": {
                "guideline_search": {"max_calls": 3},
                "fhir_query": {"max_calls": 3},
            },
        },
    }


class TestReviewerReady:
    def test_compact_traced_output_passes(self):
        case = make_case(review_ready_output(), review_ready_trace())
        case.metadata["run_config"] = review_ready_config()

        gate = ReviewerReady()
        outputs = gate.evaluate(case)
        score, test_pass, _ = gate.aggregator(outputs)

        assert test_pass is True
        assert score == pytest.approx(1.0)
        assert any(o.label == "citation_retrieved" for o in outputs)

    def test_missing_trace_fails(self):
        case = make_case(review_ready_output(), None)
        outputs = ReviewerReady().evaluate(case)
        assert any(o.label == "trace_missing" for o in outputs)

    def test_output_length_failures_are_reported(self):
        output = review_ready_output()
        output["key_findings"] = ["Too few findings"]
        output["patient_summary"] = "Too short."
        output["nudges"][0]["title"] = "This title contains far too many words for review"

        outputs = ReviewerReady().evaluate(make_case(output, review_ready_trace()))
        labels = {o.label for o in outputs if not o.test_pass}

        assert "key_finding_count_failed" in labels
        assert "patient_summary_words_failed" in labels
        assert "nudge_0_title_words_failed" in labels

    def test_configured_tool_budget_overrun_fails(self):
        trace = review_ready_trace()
        trace["tool_calls"].extend(
            [
                {"tool_name": "search_guidelines", "tool_use_id": "search-2"},
                {"tool_name": "search_guidelines", "tool_use_id": "search-3"},
                {"tool_name": "search_guidelines", "tool_use_id": "search-4"},
            ]
        )
        case = make_case(review_ready_output(), trace)
        case.metadata["run_config"] = review_ready_config()

        outputs = ReviewerReady().evaluate(case)

        assert any(o.label == "guideline_search_calls_failed" for o in outputs)

    def test_budget_blocked_attempt_does_not_count_as_external_call(self):
        trace = review_ready_trace()
        trace["tool_calls"].extend(
            [
                {
                    "tool_name": "query_patient_fhir",
                    "tool_use_id": "fhir-2",
                    "completed": True,
                    "success": True,
                },
                {
                    "tool_name": "query_patient_fhir",
                    "tool_use_id": "fhir-3",
                    "completed": True,
                    "success": True,
                },
                {
                    "tool_name": "query_patient_fhir",
                    "tool_use_id": "fhir-4",
                    "completed": True,
                    "success": True,
                },
            ]
        )
        trace["messages"].append(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "fhir-4",
                            "status": "success",
                            "content": [
                                {
                                    "text": (
                                        "BUDGET_EXHAUSTED: query_patient_fhir allows at most "
                                        "3 external calls per patient."
                                    )
                                }
                            ],
                        }
                    }
                ],
            }
        )
        case = make_case(review_ready_output(), trace)
        case.metadata["run_config"] = review_ready_config()

        outputs = ReviewerReady().evaluate(case)

        assert not any(o.label == "fhir_query_calls_failed" for o in outputs)

    def test_fhir_run_without_patient_query_fails(self):
        trace = review_ready_trace()
        trace["tool_calls"] = [
            call for call in trace["tool_calls"] if call["tool_name"] != "query_patient_fhir"
        ]
        case = make_case(review_ready_output(), trace)
        case.metadata["run_config"] = {"agent": {"max_nudges": 3}}

        outputs = ReviewerReady().evaluate(case)

        assert any(o.label == "fhir_query_calls_failed" for o in outputs)

    def test_non_fhir_run_does_not_require_patient_query(self):
        output = review_ready_output()
        output["format"] = "ccda"
        trace = review_ready_trace()
        trace["tool_calls"] = [
            call for call in trace["tool_calls"] if call["tool_name"] != "query_patient_fhir"
        ]
        case = make_case(output, trace)
        case.metadata["run_config"] = {
            **review_ready_config(),
            "context": {"data_source": "file"},
        }

        outputs = ReviewerReady().evaluate(case)

        assert not any(o.label == "fhir_query_calls_failed" for o in outputs)

    def test_fhir_run_with_zero_query_budget_has_clear_failure(self):
        config = review_ready_config()
        config["agent"]["tool_limits"]["fhir_query"]["max_calls"] = 0
        case = make_case(review_ready_output(), review_ready_trace())
        case.metadata["run_config"] = config

        outputs = ReviewerReady().evaluate(case)
        failure = next(o for o in outputs if o.label == "fhir_query_budget_disabled")

        assert failure.test_pass is False
        assert "requires at least one patient query" in failure.reason
        assert "1-0" not in failure.reason

    def test_citation_source_must_appear_in_search_results(self):
        trace = review_ready_trace()
        trace["messages"][0]["content"][0]["toolResult"]["content"][0][
            "text"
        ] = "=== KDIGO_AKI_2012 ===\nSection: AKI\nPage: 5\n\nRenal guidance."

        outputs = ReviewerReady().evaluate(make_case(review_ready_output(), trace))

        assert any(o.label == "citation_not_retrieved" for o in outputs)

    def test_reference_section_citation_fails(self):
        output = review_ready_output()
        output["nudges"][0]["guideline_citation"]["section"] = "References"

        outputs = ReviewerReady().evaluate(make_case(output, review_ready_trace()))

        assert any(o.label == "citation_reference_section" for o in outputs)

    def test_exact_repeated_nudge_fails(self):
        output = review_ready_output()
        output["nudges"].append(dict(output["nudges"][0]))

        outputs = ReviewerReady().evaluate(make_case(output, review_ready_trace()))

        assert any(o.label == "exact_duplicate_nudges" for o in outputs)
