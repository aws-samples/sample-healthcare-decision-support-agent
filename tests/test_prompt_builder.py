"""Tests for medical_nudging.agents.prompt_builder module."""

from medical_nudging.agents.prompt_builder import (
    build_system_prompt,
    build_user_prompt,
    build_user_prompt_fhir,
    load_custom_instructions,
)


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    """Tests for build_system_prompt()."""

    def test_contains_base_orchestrator_prompt(self):
        result = build_system_prompt({})
        assert "nudge" in result.lower() or "clinical" in result.lower()

    def test_does_not_contain_tool_guidance(self):
        result = build_system_prompt({})
        assert "You have access to these tools" not in result

    def test_does_not_inject_datetime(self):
        result = build_system_prompt({})
        assert "Current date and time:" not in result

    def test_source_filtering_when_enabled(self):
        result = build_system_prompt({}, enabled_sources=["ADA", "CDC"])
        assert "SOURCE FILTERING" in result
        assert "ADA" in result
        assert "CDC" in result

    def test_no_source_filtering_when_none(self):
        result = build_system_prompt({}, enabled_sources=None)
        assert "SOURCE FILTERING" not in result

    def test_includes_runtime_tool_limits(self):
        result = build_system_prompt(
            {},
            runtime_config={
                "max_nudges": 3,
                "tool_limits": {
                    "guideline_search": {
                        "max_calls": 3,
                        "max_results_per_call": 4,
                    },
                    "fhir_query": {
                        "max_calls": 3,
                        "max_resource_types_per_call": 4,
                        "max_results_per_resource": 15,
                        "recent_history_days": 365,
                        "history_anchor": "latest_resource",
                    },
                },
            },
        )

        assert "Final nudge maximum: 3" in result
        assert "Guideline-search external call maximum: 3" in result
        assert "FHIR query-round maximum: 3" in result
        assert "Default dated-history window: 365 days" in result
        assert "relative to the latest dated resource returned" in result


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    """Tests for build_user_prompt() XML-tagged template."""

    def test_contains_current_datetime(self):
        result = build_user_prompt({}, {"specialty": "general"}, "/tmp/t.xml", "general")
        assert "<current_datetime>" in result
        assert "UTC" in result

    def test_contains_specialty_in_xml(self):
        result = build_user_prompt({}, {"specialty": "cardiology"}, "/tmp/t.xml", "general")
        assert "<specialty>cardiology</specialty>" in result

    def test_contains_visit_type_in_xml(self):
        result = build_user_prompt(
            {}, {"specialty": "general", "visit_type": "inpatient"}, "/tmp/t.xml", "general"
        )
        assert "<visit_type>inpatient</visit_type>" in result

    def test_contains_chief_complaint_in_xml(self):
        result = build_user_prompt(
            {},
            {"specialty": "general", "chief_complaint": "Chest pain"},
            "/tmp/t.xml",
            "general",
        )
        assert "<chief_complaint>Chest pain</chief_complaint>" in result

    def test_contains_patient_data_in_xml(self):
        result = build_user_prompt(
            {"demographics": {"age": 58}}, {"specialty": "general"}, "/tmp/t.xml", "general"
        )
        assert "<patient_data>" in result
        assert '"age": 58' in result

    def test_contains_raw_document_path(self):
        result = build_user_prompt({}, {"specialty": "general"}, "/tmp/my.xml", "general")
        assert "<raw_document_path>/tmp/my.xml</raw_document_path>" in result

    def test_custom_instructions_after_visit_context(self):
        result = build_user_prompt(
            {},
            {"specialty": "general"},
            "/tmp/t.xml",
            "general",
            custom_instructions="Focus on CV risk.",
        )
        assert "<custom_instructions>" in result
        ci_pos = result.index("<custom_instructions>")
        vc_pos = result.index("</visit_context>")
        assert ci_pos > vc_pos

    def test_omits_custom_instructions_when_none(self):
        result = build_user_prompt({}, {"specialty": "general"}, "/tmp/t.xml", "general")
        assert "<custom_instructions>" not in result

    def test_specialty_instructions_included(self):
        result = build_user_prompt(
            {},
            {"specialty": "general"},
            "/tmp/t.xml",
            "general",
            specialty_instructions="Cardiology focus areas...",
        )
        assert "<specialty_instructions>" in result
        assert "Cardiology focus areas..." in result

    def test_omits_specialty_instructions_when_none(self):
        result = build_user_prompt({}, {"specialty": "general"}, "/tmp/t.xml", "general")
        assert "<specialty_instructions>" not in result

    def test_ends_with_begin_analysis(self):
        result = build_user_prompt({}, {"specialty": "general"}, "/tmp/t.xml", "general")
        assert result.strip().endswith("Begin your analysis now.")

    def test_falls_back_to_default_specialty(self):
        result = build_user_prompt({}, {}, "/tmp/t.xml", "endocrinology")
        assert "<specialty>endocrinology</specialty>" in result


# ---------------------------------------------------------------------------
# build_user_prompt_fhir
# ---------------------------------------------------------------------------


class TestBuildUserPromptFhir:
    """Tests for build_user_prompt_fhir() XML-tagged template."""

    def test_contains_fhir_patient_id(self):
        result = build_user_prompt_fhir("patient-123", {"specialty": "general"}, "general")
        assert "<fhir_patient_id>patient-123</fhir_patient_id>" in result

    def test_no_patient_data_tag(self):
        result = build_user_prompt_fhir("p-1", {"specialty": "general"}, "general")
        assert "<patient_data>" not in result

    def test_contains_fhir_query_tips(self):
        result = build_user_prompt_fhir("p-1", {"specialty": "general"}, "general")
        assert "FHIR" in result

    def test_contains_specialty_instructions(self):
        result = build_user_prompt_fhir(
            "p-1",
            {"specialty": "general"},
            "general",
            specialty_instructions="Endo guidance...",
        )
        assert "<specialty_instructions>" in result

    def test_contains_current_datetime(self):
        result = build_user_prompt_fhir("p-1", {"specialty": "general"}, "general")
        assert "<current_datetime>" in result

    def test_shifted_dates_use_latest_chart_event_instead_of_wall_clock(self):
        result = build_user_prompt_fhir(
            "p-1",
            {
                "specialty": "acute_care",
                "clinical_date_mode": "deidentified_shifted",
            },
            "acute_care",
        )

        assert "<current_datetime>" not in result
        assert 'mode="deidentified_shifted"' in result
        assert "latest retrieved encounter or clinical event" in result
        assert "Do not compare chart years to the wall clock" in result

    def test_ends_with_begin_analysis(self):
        result = build_user_prompt_fhir("p-1", {"specialty": "general"}, "general")
        assert result.strip().endswith("Begin your analysis now.")


# ---------------------------------------------------------------------------
# load_custom_instructions
# ---------------------------------------------------------------------------


class TestLoadCustomInstructions:
    """Tests for load_custom_instructions()."""

    def test_direct_text_returned(self):
        result = load_custom_instructions({"custom_instructions": "Be concise."})
        assert result == "Be concise."

    def test_direct_text_takes_precedence_over_preset(self):
        result = load_custom_instructions(
            {"custom_instructions": "Direct", "custom_instructions_preset": "test_focus"}
        )
        assert result == "Direct"

    def test_preset_loads_from_yaml(self):
        """Load instructions from config/custom_instructions/<name>.yaml."""
        result = load_custom_instructions({"custom_instructions_preset": "test_focus"})
        # test_focus.yaml should exist and have instructions
        assert result is not None
        assert len(result) > 0

    def test_missing_preset_returns_none(self):
        result = load_custom_instructions({"custom_instructions_preset": "nonexistent_preset_xyz"})
        assert result is None

    def test_no_custom_instructions_returns_none(self):
        result = load_custom_instructions({})
        assert result is None
