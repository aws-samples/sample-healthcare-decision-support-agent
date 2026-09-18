"""Tests for medical_nudging.agents.agent_builder module."""

from unittest.mock import patch, MagicMock

from medical_nudging.agents.agent_builder import (
    build_agent,
    build_tool_list,
    resolve_effective_mode,
    CONTEXT_CONDITIONS,
    SEARCH_MODES,
)


def test_model_config_reaches_real_strands_and_botocore_clients(monkeypatch):
    """Exercise builder → Strands → botocore configuration without inference calls."""
    from medical_nudging.config import load_config

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-test-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-test-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setattr(
        "medical_nudging.agents.agent_builder.emit_processing_started", lambda **kwargs: None
    )
    for model_id, configured, expected in (
        ("us.xai.grok-4.6", {}, 600),
        ("us.anthropic.claude-sonnet-5", {"read_timeout": 180}, 180),
    ):
        load_config()["model"] = {"model_id": model_id, **configured}
        agent = build_agent(
            model_id=None,
            extra_headers=None,
            system_prompt="Synthetic integration check.",
            tools=[],
            enable_trace=False,
            trace=None,
            request_id="transport-test",
        )
        client_config = agent.model.client.meta.config
        assert client_config.read_timeout == expected
        # Botocore normalizes max_attempts=2 retries to three total attempts.
        assert client_config.retries["total_max_attempts"] == 3
        agent.model.client.close()


# ---------------------------------------------------------------------------
# resolve_effective_mode
# ---------------------------------------------------------------------------


class TestResolveEffectiveMode:
    """Tests for resolve_effective_mode()."""

    def test_knowledge_base_maps_to_full(self):
        assert resolve_effective_mode("knowledge_base", "full") == "full"

    def test_auto_maps_to_full(self):
        assert resolve_effective_mode("auto", "full") == "full"

    def test_summaries_maps_to_summaries_only(self):
        assert resolve_effective_mode("summaries", "full") == "summaries_only"

    def test_none_falls_back_to_context_condition(self):
        assert resolve_effective_mode(None, "no_guidelines") == "no_guidelines"
        assert resolve_effective_mode(None, "summaries_only") == "summaries_only"
        assert resolve_effective_mode(None, "full") == "full"


# ---------------------------------------------------------------------------
# build_tool_list
# ---------------------------------------------------------------------------


class TestBuildToolList:
    """Tests for build_tool_list()."""

    def test_full_mode_includes_search_tools(self):
        tools = build_tool_list("full")
        tool_names = {getattr(t, "__name__", str(t)) for t in tools}
        assert "search_guidelines" in tool_names
        assert "list_guidelines" in tool_names
        assert "list_sources" in tool_names

    def test_summaries_mode_includes_list_guidelines(self):
        tools = build_tool_list("summaries_only")
        tool_names = {getattr(t, "__name__", str(t)) for t in tools}
        assert "list_guidelines" in tool_names
        # Should NOT include list_sources (OpenSearch-only)
        assert "list_sources" not in tool_names

    def test_no_guidelines_mode_minimal_tools(self):
        tools = build_tool_list("no_guidelines")
        tool_names = {getattr(t, "__name__", str(t)) for t in tools}
        assert "search_guidelines" not in tool_names
        assert "list_guidelines" not in tool_names
        assert "invoke_subagent" in tool_names

    def test_all_modes_include_base_tools(self):
        for mode in ("full", "summaries_only", "no_guidelines"):
            tools = build_tool_list(mode)
            tool_names = {getattr(t, "__name__", str(t)) for t in tools}
            assert "get_patient_data" in tool_names

    def test_all_modes_exclude_specialty_tool(self):
        for mode in ("full", "summaries_only", "no_guidelines"):
            tools = build_tool_list(mode)
            tool_names = {getattr(t, "__name__", str(t)) for t in tools}
            assert "load_specialty_instructions" not in tool_names


# ---------------------------------------------------------------------------
# build_agent (mocked)
# ---------------------------------------------------------------------------


class TestBuildAgent:
    """Tests for build_agent() with mocked dependencies."""

    @patch("medical_nudging.agents.agent_builder.Agent")
    @patch("medical_nudging.agents.agent_builder.BedrockModel")
    @patch("medical_nudging.agents.agent_builder.get_model_config")
    def test_creates_agent_with_model(self, mock_config, mock_bedrock, mock_agent):
        mock_config.return_value = {
            "model_id": "us.anthropic.claude-sonnet-4-5-20250514-v1:0",
            "thinking_type": "adaptive",
            "effort": "high",
            "cache_system_prompt": False,
            "temperature": None,
        }

        build_agent(
            model_id=None,
            extra_headers=None,
            system_prompt="System prompt",
            tools=[],
            enable_trace=False,
            trace=None,
            request_id="test-123",
        )

        mock_bedrock.assert_called_once()
        mock_agent.assert_called_once()

    @patch("medical_nudging.agents.agent_builder.Agent")
    @patch("medical_nudging.agents.agent_builder.BedrockModel")
    @patch("medical_nudging.agents.agent_builder.get_model_config")
    def test_model_id_override(self, mock_config, mock_bedrock, mock_agent):
        mock_config.return_value = {
            "model_id": "default-model",
            "thinking_type": "adaptive",
            "effort": "high",
            "cache_system_prompt": False,
            "temperature": None,
        }

        build_agent(
            model_id="custom-model",
            extra_headers=None,
            system_prompt="System prompt",
            tools=[],
            enable_trace=False,
            trace=None,
            request_id="test-123",
        )

        # BedrockModel should get the custom model_id
        call_kwargs = mock_bedrock.call_args
        assert call_kwargs.kwargs["model_id"] == "custom-model"

    @patch("medical_nudging.agents.agent_builder.Agent")
    @patch("medical_nudging.agents.agent_builder.BedrockModel")
    @patch("medical_nudging.agents.agent_builder.get_model_config")
    def test_max_tokens_passed_to_bedrock_model(self, mock_config, mock_bedrock, mock_agent):
        mock_config.return_value = {
            "model_id": "default-model",
            "thinking_type": "disabled",
            "cache_system_prompt": False,
            "temperature": None,
            "max_tokens": 32000,
        }

        build_agent(
            model_id=None,
            extra_headers=None,
            system_prompt="System prompt",
            tools=[],
            enable_trace=False,
            trace=None,
            request_id="test-123",
        )

        assert mock_bedrock.call_args.kwargs["max_tokens"] == 32000

    @patch("medical_nudging.agents.agent_builder.Agent")
    @patch("medical_nudging.agents.agent_builder.BedrockModel")
    @patch("medical_nudging.agents.agent_builder.get_model_config")
    def test_max_tokens_omitted_when_unset(self, mock_config, mock_bedrock, mock_agent):
        mock_config.return_value = {
            "model_id": "default-model",
            "thinking_type": "disabled",
            "cache_system_prompt": False,
            "temperature": None,
            "max_tokens": None,
        }

        build_agent(
            model_id=None,
            extra_headers=None,
            system_prompt="System prompt",
            tools=[],
            enable_trace=False,
            trace=None,
            request_id="test-123",
        )

        assert "max_tokens" not in mock_bedrock.call_args.kwargs

    @patch("medical_nudging.agents.agent_builder.Agent")
    @patch("medical_nudging.agents.agent_builder.BedrockModel")
    @patch("medical_nudging.agents.agent_builder.get_model_config")
    def test_openai_bedrock_model_excludes_anthropic_fields_and_cache(
        self, mock_config, mock_bedrock, mock_agent
    ):
        mock_config.return_value = {
            "model_id": "us.openai.gpt-5.6-sol",
            "thinking_type": "disabled",
            "effort": None,
            "cache_system_prompt": True,
            "extra_headers": {},
            "temperature": None,
        }

        build_agent(
            model_id=None,
            extra_headers=None,
            system_prompt="System prompt",
            tools=[],
            enable_trace=False,
            trace=None,
            request_id="test-openai",
        )

        assert mock_bedrock.call_args.kwargs["model_id"] == "us.openai.gpt-5.6-sol"
        assert "additional_request_fields" not in mock_bedrock.call_args.kwargs
        assert mock_bedrock.call_args.kwargs["boto_client_config"].read_timeout == 600
        assert mock_agent.call_args.kwargs["system_prompt"] == "System prompt"

    @patch("medical_nudging.agents.agent_builder.Agent")
    @patch("medical_nudging.agents.agent_builder.BedrockModel")
    @patch("medical_nudging.agents.agent_builder.get_model_config")
    def test_trace_callback_attached(self, mock_config, mock_bedrock, mock_agent):
        mock_config.return_value = {
            "model_id": "us.anthropic.claude-sonnet-4-5-20250514-v1:0",
            "thinking_type": "disabled",
            "effort": None,
            "cache_system_prompt": False,
            "temperature": None,
        }

        build_agent(
            model_id=None,
            extra_headers=None,
            system_prompt="System prompt",
            tools=[],
            enable_trace=True,
            trace=MagicMock(),
            request_id="test-123",
        )

        agent_kwargs = mock_agent.call_args.kwargs
        assert "callback_handler" in agent_kwargs
        assert agent_kwargs["hooks"] == [agent_kwargs["callback_handler"]]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for exported constants."""

    def test_context_conditions(self):
        assert "full" in CONTEXT_CONDITIONS
        assert "summaries_only" in CONTEXT_CONDITIONS
        assert "no_guidelines" in CONTEXT_CONDITIONS

    def test_search_modes(self):
        assert "auto" in SEARCH_MODES
        assert "knowledge_base" in SEARCH_MODES
        assert "summaries" in SEARCH_MODES
