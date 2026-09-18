"""Tests for WebSocket /ws endpoint in agent.py."""

import json

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client for the FastAPI app."""
    from agent import app

    return TestClient(app)


class TestWebSocketEndpoint:
    """Tests for the /ws WebSocket endpoint."""

    def test_websocket_streams_events(self, client):
        """Test WebSocket connection receives streamed events."""
        mock_events = [
            {"event": "text", "data": "Analyzing patient data..."},
            {
                "event": "tool_start",
                "data": {"tool": "search_guidelines", "input": {"query": "diabetes"}},
            },
            {"event": "tool_end", "data": {"tool": "search_guidelines", "duration_ms": 500}},
            {
                "event": "complete",
                "data": {
                    "status": "success",
                    "patient_summary": "Test patient",
                    "nudges": [],
                    "usage": {"input_tokens": 100, "output_tokens": 200},
                    "processing_time_ms": 1000,
                    "request_id": "test-123",
                },
            },
        ]

        async def mock_streaming(*args, **kwargs):
            for event in mock_events:
                yield event

        with patch(
            "medical_nudging.agents.orchestrator.MedicalNudgingOrchestrator"
        ) as mock_orch_class:
            mock_orch = MagicMock()
            mock_orch.generate_nudges_streaming = mock_streaming
            mock_orch_class.return_value = mock_orch

            with client.websocket_connect("/ws") as ws:
                ws.send_json(
                    {
                        "patient_data": {"demographics": {"name": "Test Patient"}},
                        "visit_context": {"specialty": "general"},
                    }
                )

                received = []
                for _ in range(len(mock_events)):
                    data = ws.receive_json()
                    received.append(data)

                assert len(received) == 4
                assert received[0]["event"] == "text"
                assert received[1]["event"] == "tool_start"
                assert received[2]["event"] == "tool_end"
                assert received[3]["event"] == "complete"
                assert received[3]["data"]["status"] == "success"

    def test_websocket_invalid_json(self, client):
        """Test WebSocket with invalid JSON sends fatal event."""
        with client.websocket_connect("/ws") as ws:
            ws.send_text("not-valid-json{{{")
            data = ws.receive_json()
            assert data["event"] == "fatal"
            assert data["data"]["code"] == "INVALID_JSON"

    def test_websocket_missing_patient_data(self, client):
        """Test WebSocket with missing patient data sends fatal event."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"visit_context": {"specialty": "general"}})
            # The request is valid (all fields optional in InvocationRequest)
            # but _extract_patient_data raises ValueError when no data is provided
            # This will be caught and sent as a fatal event
            data = ws.receive_json()
            assert data["event"] == "fatal"
            assert data["data"]["code"] == "INVALID_REQUEST"

    def test_websocket_accepts_ccda_xml(self, client):
        """Test WebSocket accepts CCDA XML input format."""
        mock_events = [
            {"event": "complete", "data": {"status": "success", "nudges": []}},
        ]

        async def mock_streaming(*args, **kwargs):
            for event in mock_events:
                yield event

        with patch(
            "medical_nudging.agents.orchestrator.MedicalNudgingOrchestrator"
        ) as mock_orch_class:
            mock_orch = MagicMock()
            mock_orch.generate_nudges_streaming = mock_streaming
            mock_orch_class.return_value = mock_orch

            with client.websocket_connect("/ws") as ws:
                ws.send_json(
                    {
                        "ccda_xml": "<ClinicalDocument>test</ClinicalDocument>",
                        "visit_context": {"specialty": "cardiology"},
                    }
                )

                data = ws.receive_json()
                assert data["event"] == "complete"

    def test_websocket_accepts_prompt_format(self, client):
        """Test WebSocket accepts AgentCore prompt format."""
        mock_events = [
            {"event": "complete", "data": {"status": "success", "nudges": []}},
        ]

        async def mock_streaming(*args, **kwargs):
            for event in mock_events:
                yield event

        with patch(
            "medical_nudging.agents.orchestrator.MedicalNudgingOrchestrator"
        ) as mock_orch_class:
            mock_orch = MagicMock()
            mock_orch.generate_nudges_streaming = mock_streaming
            mock_orch_class.return_value = mock_orch

            with client.websocket_connect("/ws") as ws:
                ws.send_json({"prompt": "<ClinicalDocument>test</ClinicalDocument>"})

                data = ws.receive_json()
                assert data["event"] == "complete"

    def test_websocket_chunked_transfer(self, client):
        """Test WebSocket accepts chunked transfer for large payloads."""
        mock_events = [
            {"event": "complete", "data": {"status": "success", "nudges": []}},
        ]

        async def mock_streaming(*args, **kwargs):
            for event in mock_events:
                yield event

        with patch(
            "medical_nudging.agents.orchestrator.MedicalNudgingOrchestrator"
        ) as mock_orch_class:
            mock_orch = MagicMock()
            mock_orch.generate_nudges_streaming = mock_streaming
            mock_orch_class.return_value = mock_orch

            # Simulate chunked transfer of a large CCDA XML
            large_xml = "<ClinicalDocument>" + "x" * 50000 + "</ClinicalDocument>"
            chunk_size = 20000
            chunks = [large_xml[i : i + chunk_size] for i in range(0, len(large_xml), chunk_size)]

            with client.websocket_connect("/ws") as ws:
                # Send chunked init
                ws.send_json(
                    {
                        "_chunked": True,
                        "total_chunks": len(chunks),
                        "meta": {
                            "_data_field": "ccda_xml",
                            "visit_context": {"specialty": "general"},
                        },
                    }
                )
                # Send data chunks
                for i, chunk in enumerate(chunks):
                    ws.send_text(json.dumps({"_chunk_index": i, "_chunk_data": chunk}))

                data = ws.receive_json()
                assert data["event"] == "complete"

    def test_websocket_chunked_invalid_init(self, client):
        """Test chunked transfer with missing _data_field sends fatal event."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"_chunked": True, "total_chunks": 2, "meta": {}})
            data = ws.receive_json()
            assert data["event"] == "fatal"
            assert data["data"]["code"] == "INVALID_CHUNKED_REQUEST"
