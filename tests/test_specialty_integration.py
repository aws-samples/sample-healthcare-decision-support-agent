"""Integration tests for specialty instructions with orchestrator.

Specialty instructions are pre-loaded into the user prompt at build time
(not loaded dynamically via a tool call).
"""

from unittest.mock import MagicMock, patch

from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator


def _extract_user_prompt(mock_agent_instance):
    """Extract the user prompt string passed to the agent call."""
    assert mock_agent_instance.called
    # agent(user_prompt) — first positional arg
    return mock_agent_instance.call_args[0][0]


def test_orchestrator_loads_cardiology_instructions():
    """Test that orchestrator pre-loads cardiology instructions into user prompt."""
    orchestrator = MedicalNudgingOrchestrator(specialty="cardiology")

    mock_ccda = """<?xml version="1.0" encoding="UTF-8"?>
    <ClinicalDocument xmlns="urn:hl7-org:v3">
        <recordTarget><patientRole><patient>
            <name><given>John</given><family>Doe</family></name>
        </patient></patientRole></recordTarget>
    </ClinicalDocument>"""

    visit_context = {"visit_type": "ambulatory", "specialty": "cardiology"}

    with patch("medical_nudging.agents.agent_builder.Agent") as mock_agent_class:
        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent
        mock_agent.return_value.message = {
            "content": [
                {
                    "text": """[{
                    "title": "Blood pressure check",
                    "description": "Patient is overdue for BP monitoring",
                    "urgency": "warning",
                    "category": "gap_in_care",
                    "action_type": ["order"],
                    "rationale": "ADA guidelines",
                    "grounding": "guideline"
                }]"""
                }
            ]
        }

        orchestrator.generate_nudges(mock_ccda, visit_context)

        # Specialty instructions are NOT a tool anymore
        call_kwargs = mock_agent_class.call_args[1]
        tools = call_kwargs.get("tools", [])
        tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        assert "load_specialty_instructions" not in tool_names

        # Specialty instructions are pre-loaded in the user prompt
        user_prompt = _extract_user_prompt(mock_agent)
        assert "<specialty_instructions>" in user_prompt
        assert "cardiology" in user_prompt.lower()


def test_orchestrator_loads_endocrinology_instructions():
    """Test that orchestrator pre-loads endocrinology instructions into user prompt."""
    orchestrator = MedicalNudgingOrchestrator(specialty="endocrinology")

    mock_ccda = """<?xml version="1.0" encoding="UTF-8"?>
    <ClinicalDocument xmlns="urn:hl7-org:v3">
        <recordTarget><patientRole><patient>
            <name><given>Jane</given><family>Smith</family></name>
        </patient></patientRole></recordTarget>
    </ClinicalDocument>"""

    visit_context = {"visit_type": "ambulatory", "specialty": "endocrinology"}

    with patch("medical_nudging.agents.agent_builder.Agent") as mock_agent_class:
        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent
        mock_agent.return_value.message = {
            "content": [
                {
                    "text": """[{
                    "title": "A1c check",
                    "description": "Patient needs diabetes monitoring",
                    "urgency": "warning",
                    "category": "gap_in_care",
                    "action_type": ["order"],
                    "rationale": "ADA standards",
                    "grounding": "guideline"
                }]"""
                }
            ]
        }

        orchestrator.generate_nudges(mock_ccda, visit_context)

        user_prompt = _extract_user_prompt(mock_agent)
        assert "<specialty_instructions>" in user_prompt
        assert "endocrinology" in user_prompt.lower()


def test_orchestrator_uses_default_specialty():
    """Test that orchestrator uses default specialty when not specified in visit context."""
    orchestrator = MedicalNudgingOrchestrator(specialty="cardiology")

    mock_ccda = """<?xml version="1.0" encoding="UTF-8"?>
    <ClinicalDocument xmlns="urn:hl7-org:v3">
        <recordTarget><patientRole><patient>
            <name><given>John</given><family>Doe</family></name>
        </patient></patientRole></recordTarget>
    </ClinicalDocument>"""

    visit_context = {"visit_type": "ambulatory"}

    with patch("medical_nudging.agents.agent_builder.Agent") as mock_agent_class:
        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent
        mock_agent.return_value.message = {
            "content": [
                {
                    "text": '[{"title": "Test", "description": "Test", "urgency": "informational", "category": "preventive", "action_type": ["education"], "rationale": "Test", "grounding": "clinical_reasoning"}]'
                }
            ]
        }

        orchestrator.generate_nudges(mock_ccda, visit_context)

        # Default specialty (cardiology) should be used
        assert orchestrator.specialty == "cardiology"
        user_prompt = _extract_user_prompt(mock_agent)
        assert "<specialty>cardiology</specialty>" in user_prompt


def test_orchestrator_fallback_to_general():
    """Test that orchestrator falls back to general instructions for unknown specialty."""
    orchestrator = MedicalNudgingOrchestrator(specialty="general")

    mock_ccda = """<?xml version="1.0" encoding="UTF-8"?>
    <ClinicalDocument xmlns="urn:hl7-org:v3">
        <recordTarget><patientRole><patient>
            <name><given>John</given><family>Doe</family></name>
        </patient></patientRole></recordTarget>
    </ClinicalDocument>"""

    visit_context = {"visit_type": "ambulatory", "specialty": "unknown_specialty"}

    with patch("medical_nudging.agents.agent_builder.Agent") as mock_agent_class:
        mock_agent = MagicMock()
        mock_agent_class.return_value = mock_agent
        mock_agent.return_value.message = {
            "content": [
                {
                    "text": '[{"title": "Test", "description": "Test", "urgency": "informational", "category": "preventive", "action_type": ["education"], "rationale": "Test", "grounding": "clinical_reasoning"}]'
                }
            ]
        }

        orchestrator.generate_nudges(mock_ccda, visit_context)

        # Should fall back to general guidance
        user_prompt = _extract_user_prompt(mock_agent)
        assert "<specialty_instructions>" in user_prompt
        assert "general" in user_prompt.lower()
        assert orchestrator.specialty == "general"
