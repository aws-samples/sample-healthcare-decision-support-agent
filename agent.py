"""Medical Nudging Agent - FastAPI entry point for AgentCore.

This module provides the HTTP interface for the Medical Nudging agent,
compatible with Amazon Bedrock AgentCore runtime requirements.

Endpoints:
    - POST /invocations: Process nudge generation requests
    - GET /ping: Health check for load balancer
"""

import asyncio
import binascii
import json
import logging
import os
import time
import traceback
from functools import cache
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocket, WebSocketDisconnect

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Medical Nudging Agent",
    description="Clinical decision support agent for generating evidence-based nudges",
    version="1.0.0",
)

# ============================================================================
# Configuration
# ============================================================================

# Configuration - deferred to avoid import-time side effects (Rule 5)
# All settings come from config/settings.yaml as the single source of truth


@cache
def _get_model_id() -> str:
    """Return model ID from config/settings.yaml (cached after first call)."""
    from medical_nudging.config import get_model_config

    return get_model_config()["model_id"]


@cache
def _get_max_nudges() -> int:
    """Return max nudges from config/settings.yaml (cached after first call)."""
    from medical_nudging.config import get_agent_config

    return get_agent_config()["max_nudges"]


@cache
def _get_agent_runtime_config() -> dict[str, Any]:
    """Return request-level agent defaults from config/settings.yaml."""
    from medical_nudging.config import get_agent_config

    return dict(get_agent_config())


@cache
def _bundle_resolver() -> Any:
    """One resolver per process; bundle versions are immutable, so it caches by version."""
    from medical_nudging.config_bundle import ConfigBundleResolver

    return ConfigBundleResolver(region=os.environ.get("AWS_REGION"))


class ConfigBundleUnavailable(ValueError):
    """The request named a configuration bundle version this runtime cannot apply."""


def _bundle_for_request(headers: Any) -> Any:
    """The configuration bundle a request selects through its ``baggage`` header.

    Returns ``None`` when the header names no bundle (the repository prompt applies).
    A named version that cannot be fetched or has no section for this runtime is an
    error, never a silent fallback: the caller pinned a prompt version and must learn
    it was not the one used.
    """
    from medical_nudging.config_bundle import ConfigBundleError, bundle_ref_from_baggage

    ref = bundle_ref_from_baggage(headers.get("baggage"))
    if ref is None:
        return None
    try:
        return _bundle_resolver().resolve(ref)
    except ConfigBundleError as error:
        raise ConfigBundleUnavailable(str(error)) from error


@cache
def _get_search_backend() -> str:
    """Return search backend from config/settings.yaml (cached after first call)."""
    from medical_nudging.config import get_agent_config

    return get_agent_config()["search_backend"]


@cache
def _get_opensearch_endpoint() -> str | None:
    """Return OpenSearch endpoint from config/settings.yaml (cached after first call)."""
    from medical_nudging.config import get_opensearch_config

    return get_opensearch_config()["opensearch_endpoint"]


# ============================================================================
# Request/Response Models
# ============================================================================


class VisitContext(BaseModel):
    """Context about the patient visit."""

    visit_type: str | None = Field(default="ambulatory", description="Type of visit")
    specialty: str | None = Field(default="general", description="Medical specialty")
    chief_complaint: str | None = Field(default=None, description="Chief complaint")
    data_source: Literal["file", "fhir_api"] | None = Field(
        default=None,
        description="Data source: 'file' (or null) for document string, 'fhir_api' for FHIR API",
    )
    patient_id: str | None = Field(
        default=None,
        description="FHIR Patient resource ID (required when data_source='fhir_api')",
    )


class NudgeGenerationConfig(BaseModel):
    """Configuration for nudge generation."""

    max_nudges: int = Field(default=5, ge=1, le=20, description="Maximum nudges to generate")
    model_version: str | None = Field(
        default=None, description="Model version (derived from config/settings.yaml if not set)"
    )
    tool_limits: dict[str, dict[str, int | None]] | None = Field(
        default=None,
        description="Request-scoped guideline-search and FHIR-query limits",
    )
    return_evidence: bool = Field(
        default=False,
        description=(
            "Run the contract-steered generator and return the execution trace, tool "
            "ledger, and deterministic verifier reports next to the response. Used by "
            "the candidate-acceptance gate; the evidence carries raw patient records."
        ),
    )
    generator_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Frozen steered-generator configuration for return_evidence requests: "
            "config_version, retry_cap, corpus_version, model_id, entailment_model_id, "
            "require_citation, custom_instructions, fhir_max_pages"
        ),
    )


class InvocationRequest(BaseModel):
    """Request body for /invocations endpoint.

    Supports three formats:
    1. AgentCore standard: {"prompt": "<CCDA or FHIR content>"}
    2. Structured XML/JSON: {"ccda_xml": "...", "visit_context": {...}, "config": {...}}
    3. Pre-parsed: {"patient_data": {...}, "visit_context": {...}, "config": {...}}
    """

    # AgentCore standard format
    prompt: str | None = Field(default=None, description="Prompt containing patient data")

    # Structured format (for direct API calls)
    ccda_xml: str | None = Field(default=None, description="CCDA XML document")
    fhir_json: str | None = Field(
        default=None, description="FHIR JSON bundle (alternative to CCDA)"
    )
    patient_data: dict | None = Field(
        default=None, description="Pre-parsed patient data (structured JSON object)"
    )
    visit_context: VisitContext = Field(default_factory=VisitContext)
    config: NudgeGenerationConfig = Field(default_factory=NudgeGenerationConfig)


class HealthResponse(BaseModel):
    """Response body for /ping endpoint."""

    status: str = "healthy"
    model_id: str = ""

    def __init__(self, **data: Any) -> None:
        if "model_id" not in data:
            data["model_id"] = _get_model_id()
        super().__init__(**data)


# ============================================================================
# Middleware
# ============================================================================


@app.middleware("http")
async def log_requests(request: Request, call_next: Any) -> Response:
    """Log all incoming requests and decode base64 payload if needed."""
    logger.info(f"{request.method} {request.url.path}")

    # For POST requests, check if body is base64 encoded
    if request.method == "POST":
        body = await request.body()
        logger.info(f"Raw request body (first 200 chars): {body[:200]}")

        # Try to decode base64 if the body doesn't look like JSON
        decoded_body = body
        if body and not body.strip().startswith(b"{"):
            try:
                import base64

                decoded_body = base64.b64decode(body)
                logger.info(f"Decoded base64 body: {decoded_body[:200]}")
            except (ValueError, binascii.Error) as e:
                # Rule 2: Catch specific exceptions, not broad Exception
                logger.debug(f"Body is not base64 encoded: {e}")

        # Reconstruct request with decoded body
        from starlette.requests import Request as StarletteRequest

        async def receive():
            return {"type": "http.request", "body": decoded_body}

        request = StarletteRequest(request.scope, receive)

    response = await call_next(request)
    logger.info(f"Response status: {response.status_code}")
    return response


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/ping", response_model=HealthResponse)
async def ping() -> HealthResponse:
    """Health check endpoint for load balancer.

    Returns:
        HealthResponse with status and configuration info.
    """
    return HealthResponse()


@app.get("/health")
async def health() -> dict[str, str]:
    """Alternative health check endpoint.

    Returns:
        Simple status dict.
    """
    return {"status": "ok"}


def _extract_patient_data(request: InvocationRequest) -> tuple[str | None, dict, dict]:
    """Extract patient data, visit context, and config from request.

    Handles AgentCore standard format (prompt), structured format, and FHIR API mode.

    Returns:
        Tuple of (patient_data_or_none, visit_context, config).
        patient_data is ``None`` when ``data_source='fhir_api'``.
    """
    # Check for AgentCore standard format first
    if request.prompt:
        logger.info("Processing AgentCore standard format (prompt)")
        patient_data_str: str | None = request.prompt.strip()
        visit_context = {
            "visit_type": "ambulatory",
            "specialty": "general",
            "chief_complaint": None,
        }
        config = {
            **_get_agent_runtime_config(),
            "max_nudges": _get_max_nudges(),
            "model_version": _get_model_id(),
        }
        return patient_data_str, visit_context, config

    # Structured format
    if request.ccda_xml:
        logger.info("Processing structured CCDA XML input")
        patient_data_str = request.ccda_xml
    elif request.fhir_json:
        logger.info("Processing structured FHIR JSON input")
        patient_data_str = request.fhir_json
    elif request.patient_data:
        logger.info("Processing pre-parsed patient data")
        # Convert dict to JSON string for orchestrator (it expects a string)
        patient_data_str = json.dumps(request.patient_data)
    elif request.visit_context.data_source == "fhir_api":
        logger.info("Processing FHIR API mode (no document)")
        if not request.visit_context.patient_id:
            raise ValueError("patient_id required in visit_context when data_source='fhir_api'")
        patient_data_str = None
    else:
        raise ValueError("No patient data provided")

    visit_context: dict[str, Any] = {
        "visit_type": request.visit_context.visit_type,
        "specialty": request.visit_context.specialty,
        "chief_complaint": request.visit_context.chief_complaint,
    }
    # Pass through FHIR API fields when set
    if request.visit_context.data_source:
        visit_context["data_source"] = request.visit_context.data_source
    if request.visit_context.patient_id:
        visit_context["patient_id"] = request.visit_context.patient_id

    config = {
        **_get_agent_runtime_config(),
        "max_nudges": request.config.max_nudges,
        "model_version": request.config.model_version or _get_model_id(),
    }
    if request.config.tool_limits is not None:
        config["tool_limits"] = request.config.tool_limits
    return patient_data_str, visit_context, config


class EvidenceConfigurationError(ValueError):
    """The runtime cannot honour the frozen generator configuration it was sent."""


def _check_evidence_configuration(generator_config: dict[str, Any]) -> dict[str, Any]:
    """Refuse a return_evidence request whose frozen retrieval settings this runtime lacks.

    The corpus index and the FHIR page cap are runtime environment, not request
    parameters. A silent mismatch would score the candidate against a different
    corpus or coverage window than the dataset was frozen on, so the request fails
    instead. Returns the effective retrieval settings for the evidence record.
    """
    from medical_nudging.config import get_fhir_config, get_opensearch_config

    opensearch = get_opensearch_config()
    fhir = get_fhir_config()
    requested_corpus = generator_config.get("corpus_version")
    if requested_corpus and requested_corpus != opensearch["opensearch_index"]:
        raise EvidenceConfigurationError(
            f"Runtime searches index {opensearch['opensearch_index']!r}; the request is "
            f"frozen on corpus {requested_corpus!r}. Deploy the runtime with "
            "OPENSEARCH_INDEX_NAME set to the frozen corpus."
        )
    requested_pages = generator_config.get("fhir_max_pages")
    if requested_pages is not None and requested_pages != fhir["max_pages"]:
        raise EvidenceConfigurationError(
            f"Runtime FHIR page cap is {fhir['max_pages']!r}; the request is frozen on "
            f"fhir_max_pages={requested_pages!r}. Set FHIR_MAX_PAGES on the runtime."
        )
    return {
        "opensearch_index": opensearch["opensearch_index"],
        "fhir_max_pages": fhir["max_pages"],
        "fhir_api_enabled": fhir["enabled"],
    }


def _generate_with_evidence(
    patient_data: str | None,
    visit_context: dict[str, Any],
    config: dict[str, Any],
    generator_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the steered generator and return the response with its evidence record.

    The response is the same eligible output a plain invocation returns; the
    ``evidence`` block adds the trace, tool ledger, coverage, verifier reports,
    and the effective settings so a gate can detect configuration drift.
    """
    from medical_nudging.config import get_agent_config, get_model_config
    from medical_nudging.steered_generation import steered_generation_record

    generator_config = dict(generator_config or {})
    retrieval = _check_evidence_configuration(generator_config)
    if generator_config.get("model_id"):
        config["model_version"] = generator_config["model_id"]
    model_settings = {
        key: value for key, value in get_model_config().items() if key != "extra_headers"
    }
    model_settings["model_id"] = config["model_version"]
    generation_settings = {
        "model": model_settings,
        "agent": {
            "max_nudges": config["max_nudges"],
            "search_backend": get_agent_config()["search_backend"],
            "tool_limits": config.get("tool_limits") or {},
        },
        "generator_config": generator_config,
        "specialty": visit_context.get("specialty", "general"),
    }
    data_source = visit_context.get("data_source") or "file"
    record = steered_generation_record(
        patient_id=visit_context.get("patient_id") or "document",
        visit_context=visit_context,
        data_source=data_source,
        patient_data=patient_data,
        generator_config=generator_config,
        generation_settings=generation_settings,
        specialty=visit_context.get("specialty", "general"),
        request_config=config,
    )
    response = record.pop("response")
    return {
        "output": response,
        "evidence": record,
        "runtime": {
            "image_tag": os.environ.get("IMAGE_TAG"),
            "stack_name": os.environ.get("STACK_NAME"),
            "prompt": record.get("prompt"),
            **retrieval,
        },
    }


@app.post("/invocations")
async def invoke_agent(request: InvocationRequest, raw_request: Request) -> JSONResponse:
    """Process a nudge generation request.

    Supports two payload formats:
    1. AgentCore standard: {"prompt": "<CCDA or FHIR content>"}
    2. Structured: {"ccda_xml": "...", "visit_context": {...}, "config": {...}}

    A ``baggage`` header naming a configuration bundle version selects the base
    system prompt for this request (see ``medical_nudging.config_bundle``).

    Args:
        request: InvocationRequest with patient data and configuration.
        raw_request: The HTTP request, read for its ``baggage`` header.

    Returns:
        JSONResponse with NudgeResponse data.

    Raises:
        HTTPException: If no patient data provided or processing fails.
    """
    logger.info("Processing invocation request")

    try:
        # Extract patient data from either format
        patient_data, visit_context, config = _extract_patient_data(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        bundle = _bundle_for_request(raw_request.headers)
    except ConfigBundleUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))

    from medical_nudging.config_bundle import bundle_scope, prompt_provenance
    from medical_nudging.evidence_stream import EVENT_STREAM_MEDIA_TYPE, accepts_event_stream

    if request.config.return_evidence and accepts_event_stream(raw_request.headers.get("accept")):
        # A steered generation runs for minutes. A synchronous body that sends nothing
        # for that long never reaches the client, so an evidence request that accepts
        # server-sent events gets keepalives while the generation runs and the record
        # in chunks at the end. Configuration refusals stay real 409s: check first.
        try:
            _check_evidence_configuration(dict(request.config.generator_config or {}))
        except EvidenceConfigurationError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return StreamingResponse(
            _stream_evidence(
                bundle, patient_data, visit_context, config, request.config.generator_config
            ),
            media_type=EVENT_STREAM_MEDIA_TYPE,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    with bundle_scope(bundle):
        return _invoke(request, patient_data, visit_context, config, prompt_provenance)


async def _stream_evidence(
    bundle: Any,
    patient_data: str | None,
    visit_context: dict,
    config: dict,
    generator_config: dict[str, Any] | None,
) -> Any:
    """Yield SSE events: keepalives during generation, then the evidence payload."""
    from medical_nudging.config_bundle import bundle_scope
    from medical_nudging.evidence_stream import (
        KEEPALIVE_SECONDS,
        error_event,
        keepalive_event,
        payload_events,
    )

    def generate() -> dict[str, Any]:
        # The bundle scope is a context variable; to_thread copies the context, but the
        # request's own scope has already exited by the time this generator runs.
        with bundle_scope(bundle):
            return _generate_with_evidence(patient_data, visit_context, config, generator_config)

    started = time.monotonic()
    task = asyncio.ensure_future(asyncio.to_thread(generate))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=KEEPALIVE_SECONDS)
            if done:
                break
            yield keepalive_event(time.monotonic() - started)
        payload = task.result()
    except EvidenceConfigurationError as e:
        yield error_event(409, str(e))
        return
    except Exception as e:  # noqa: BLE001 - the stream is the only channel left
        logger.error(f"Error processing streamed evidence request: {e}")
        logger.error(traceback.format_exc())
        yield error_event(500, f"Internal error: {e}")
        return
    output = payload["output"]
    logger.info(
        f"Generated {len(output.get('nudges', []))} nudges with status: "
        f"{output.get('status')} (evidence streamed, {time.monotonic() - started:.0f}s)"
    )
    for event in payload_events(payload):
        yield event


def _invoke(
    request: InvocationRequest,
    patient_data: str | None,
    visit_context: dict,
    config: dict,
    prompt_provenance: Any,
) -> JSONResponse:
    """Run one invocation under the already-activated configuration bundle."""
    if request.config.return_evidence:
        try:
            payload = _generate_with_evidence(
                patient_data, visit_context, config, request.config.generator_config
            )
        except EvidenceConfigurationError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ImportError as e:
            logger.error(f"Import error: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to import required modules: {str(e)}"
            )
        except Exception as e:
            logger.error(f"Error processing evidence request: {e}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
        output = payload["output"]
        logger.info(
            f"Generated {len(output.get('nudges', []))} nudges with status: "
            f"{output.get('status')} (evidence returned)"
        )
        return JSONResponse(content=payload)

    try:
        # Import orchestrator (deferred to avoid import issues during startup)
        from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator

        # Initialize orchestrator
        specialty = visit_context.get("specialty", "general")
        orchestrator = MedicalNudgingOrchestrator(specialty=specialty)

        # Generate nudges
        response = orchestrator.generate_nudges(
            patient_data=patient_data,
            visit_context=visit_context,
            config=config,
        )

        # Convert response to dict
        response_dict = response.model_dump()

        logger.info(f"Generated {len(response.nudges)} nudges with status: {response.status}")

        return JSONResponse(content={"output": response_dict, "prompt": prompt_provenance()})

    except ImportError as e:
        logger.error(f"Import error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to import required modules: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Internal error: {str(e)}",
        )


@app.post("/invoke")
async def invoke_agent_alias(request: InvocationRequest) -> JSONResponse:
    """Alias for /invocations endpoint.

    Some AgentCore configurations may use /invoke instead of /invocations.
    """
    return await invoke_agent(request)


# ============================================================================
# WebSocket Streaming Endpoint
# ============================================================================

# AgentCore enforces a 32KB WebSocket frame size limit (not adjustable).
# Large payloads like raw CCDA XML (~100KB) must use application-level chunking.


async def _receive_ws_request(websocket: WebSocket) -> dict | None:
    """Receive a WebSocket request, supporting both single-message and chunked protocols.

    Single-message protocol (payloads < 32KB):
        Client sends one JSON message with the full request.

    Chunked protocol (payloads > 32KB):
        1. Client sends: {"_chunked": true, "total_chunks": N, "meta": {...}}
           where meta contains visit_context, config, _data_field (field name for large data)
        2. Client sends N messages: {"_chunk_index": i, "_chunk_data": "..."}
        3. Server reassembles the data field and returns the full request dict.

    Returns:
        Parsed request dict, or None if an error was sent to the client.
    """
    raw_message = await websocket.receive_text()
    logger.info(f"WebSocket received initial message: {len(raw_message)} bytes")

    try:
        first_msg = json.loads(raw_message)
    except json.JSONDecodeError as e:
        await websocket.send_json(
            {"event": "fatal", "data": {"code": "INVALID_JSON", "message": str(e)}}
        )
        return None

    # Check if this is a chunked transfer
    if not first_msg.get("_chunked"):
        return first_msg

    # Chunked protocol: reassemble data from multiple messages
    total_chunks = first_msg.get("total_chunks", 0)
    meta = first_msg.get("meta", {})
    data_field = meta.pop("_data_field", None)

    if not data_field or total_chunks <= 0:
        await websocket.send_json(
            {
                "event": "fatal",
                "data": {
                    "code": "INVALID_CHUNKED_REQUEST",
                    "message": "Missing _data_field or total_chunks in chunked init",
                },
            }
        )
        return None

    logger.info(f"WebSocket chunked transfer: {total_chunks} chunks for field '{data_field}'")

    # Collect chunks (they arrive in order)
    chunks: list[str] = [""] * total_chunks
    received = 0
    while received < total_chunks:
        chunk_raw = await websocket.receive_text()
        try:
            chunk_msg = json.loads(chunk_raw)
        except json.JSONDecodeError as e:
            await websocket.send_json(
                {"event": "fatal", "data": {"code": "INVALID_JSON", "message": f"Bad chunk: {e}"}}
            )
            return None

        idx = chunk_msg.get("_chunk_index")
        data = chunk_msg.get("_chunk_data", "")
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= total_chunks:
            await websocket.send_json(
                {
                    "event": "fatal",
                    "data": {
                        "code": "INVALID_CHUNK",
                        "message": f"Invalid chunk index: {idx}",
                    },
                }
            )
            return None

        chunks[idx] = data
        received += 1

    # Reassemble the full data
    full_data = "".join(chunks)
    logger.info(
        f"WebSocket chunked transfer complete: {total_chunks} chunks, "
        f"{len(full_data)} bytes reassembled for '{data_field}'"
    )

    # Build the full request dict
    request_data = dict(meta)
    request_data[data_field] = full_data
    return request_data


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for streaming nudge generation.

    Protocol:
        1. Client connects, server accepts
        2. Client sends JSON message with patient data (same schema as /invocations)
        3. Server streams event dicts as JSON messages:
           - {"event": "text", "data": "..."}
           - {"event": "tool_start", "data": {...}}
           - {"event": "tool_end", "data": {...}}
           - {"event": "complete", "data": {...}}
           - {"event": "error", "data": {...}}
           - {"event": "fatal", "data": {...}}
        4. Connection closes after "complete" or "fatal" event
    """
    await websocket.accept()
    logger.info("WebSocket connection accepted")

    try:
        # Receive the request - supports both single-message and chunked protocols.
        # AgentCore enforces a 32KB WebSocket frame limit, so large payloads
        # (e.g., raw CCDA XML) must be sent as multiple chunked messages.
        request_data = await _receive_ws_request(websocket)
        if request_data is None:
            return  # Error already sent to client

        # Log which data format was provided (without PHI)
        has_ccda = bool(request_data.get("ccda_xml"))
        has_fhir = bool(request_data.get("fhir_json"))
        has_preparsed = bool(request_data.get("patient_data"))
        logger.info(
            f"WebSocket request: ccda={has_ccda}, fhir={has_fhir}, " f"preparsed={has_preparsed}"
        )

        # Parse using existing request model and extraction logic
        try:
            request = InvocationRequest(**request_data)
            patient_data, visit_context, config = _extract_patient_data(request)
        except ValueError as e:
            await websocket.send_json(
                {"event": "fatal", "data": {"code": "INVALID_REQUEST", "message": str(e)}}
            )
            return

        try:
            bundle = _bundle_for_request(websocket.headers)
        except ConfigBundleUnavailable as e:
            await websocket.send_json(
                {"event": "fatal", "data": {"code": "CONFIG_BUNDLE_UNAVAILABLE", "message": str(e)}}
            )
            return

        # Import orchestrator (deferred to avoid import issues during startup)
        from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator
        from medical_nudging.config_bundle import bundle_scope

        specialty = visit_context.get("specialty", "general")
        orchestrator = MedicalNudgingOrchestrator(specialty=specialty)

        # Stream events from the orchestrator
        event_count = 0
        with bundle_scope(bundle):
            async for event in orchestrator.generate_nudges_streaming(
                patient_data=patient_data,
                visit_context=visit_context,
                config=config,
            ):
                await websocket.send_json(event)
                event_count += 1

        logger.info(f"WebSocket streaming complete: {event_count} events sent")

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        logger.error(traceback.format_exc())
        try:
            await websocket.send_json(
                {"event": "fatal", "data": {"code": "INTERNAL_ERROR", "message": str(e)}}
            )
        except Exception:
            logger.debug("Could not send fatal event - client already disconnected")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass  # Already closed


# ============================================================================
# Startup/Shutdown Events
# ============================================================================


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize resources on startup."""
    logger.info("Medical Nudging Agent starting up...")
    logger.info(f"MODEL_ID: {_get_model_id()}")
    logger.info(f"MAX_NUDGES: {_get_max_nudges()}")

    # Log search backend configuration (from config/settings.yaml)
    search_backend = _get_search_backend()
    opensearch_endpoint = _get_opensearch_endpoint()
    logger.info(f"SEARCH_BACKEND: {search_backend}")
    if opensearch_endpoint:
        logger.info(f"OPENSEARCH_ENDPOINT: {opensearch_endpoint}")
    else:
        logger.info("No OpenSearch configured, using file-based guidelines search")

    logger.info("Medical Nudging Agent ready to accept requests")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Cleanup resources on shutdown."""
    logger.info("Medical Nudging Agent shutting down...")

    # Close database connection pool
    try:
        from medical_nudging.db.connection import close_connection_pool

        close_connection_pool()
        logger.info("Database connection pool closed")
    except Exception as e:
        logger.warning(f"Error closing database connection pool: {e}")

    logger.info("Medical Nudging Agent shutdown complete")


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    # Get configuration from environment
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
