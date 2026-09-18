"""FHIR API query tool for the Strands agent.

Provides a @tool that queries patient data from a FHIR R4 server.
The agent uses this to make targeted queries (active conditions, recent
labs, current meds) instead of ingesting entire patient records.

Usage (standalone test against a HealthLake datastore):
    from medical_nudging.tools.fhir_query import create_fhir_query_tool
    from medical_nudging.search.fhir_client import create_healthlake_client

    client = create_healthlake_client(datastore_endpoint, region="us-east-1")
    tool = create_fhir_query_tool(client)
    result = tool(patient_id="abc123", resource_types=["Condition", "MedicationRequest"])
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from strands import tool

from medical_nudging.config import get_fhir_config
from medical_nudging.search.fhir_auth import FHIRAuthError
from medical_nudging.search.fhir_client import (
    DEFAULT_MAX_PAGES,
    FHIRClientError,
    FHIRSearchParams,
    RestFHIRClient,
    create_healthlake_client,
)
from medical_nudging.tools.raw_resource_channel import RawResourceChannel, default_channel
from medical_nudging.tools.tool_budget import CallBudget

logger = logging.getLogger(__name__)

# Default resource queries when the agent doesn't specify resource_types.
# No status filters: MIMIC-IV FHIR Conditions carry no clinicalStatus element and its
# MedicationRequests never match status=active, so a status filter silently returns an
# empty bundle (HTTP 200) and hides the entire resource type from the agent.  Status
# interpretation belongs to the medication-status rule, which reads raw status fields.
DEFAULT_RESOURCE_QUERIES: dict[str, dict[str, str]] = {
    "Condition": {},
    "MedicationRequest": {},
    "AllergyIntolerance": {},
    "Observation": {"category": "laboratory", "_sort": "-date", "_count": "20"},
}

# Resources the tool supports querying
SUPPORTED_RESOURCES = {
    "Patient",
    "Condition",
    "Observation",
    "MedicationRequest",
    "MedicationAdministration",
    "MedicationDispense",
    "MedicationStatement",
    "Encounter",
    "Procedure",
    "AllergyIntolerance",
    "Immunization",
}

RECENT_HISTORY_RESOURCES = {
    "Observation",
    "MedicationAdministration",
    "MedicationDispense",
    "Encounter",
    "Procedure",
    "Immunization",
}

FHIRParamScalar = str | int | float | bool | None
FHIRParamValue = FHIRParamScalar | list[FHIRParamScalar]
HistoryAnchor = Literal["wall_clock", "latest_resource"]

# Code search params that servers index as tokens only; display-name values
# in these params silently match nothing, so the tool filters them client-side.
CODE_FILTER_PARAMS = ("code", "code:text")
NAME_FILTER_PAGE_COUNT = "100"
NAME_FILTER_MAX_PAGES = 3


def _normalize_param_scalar(value: FHIRParamScalar) -> str | None:
    """Convert one model-supplied scalar to a FHIR query string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _normalize_search_params(params: dict[str, FHIRParamValue] | None) -> FHIRSearchParams:
    """Convert model-supplied scalar search params to FHIR query strings."""
    normalized: FHIRSearchParams = {}
    for key, value in (params or {}).items():
        if isinstance(value, list):
            repeated = [
                normalized_value
                for item in value
                if (normalized_value := _normalize_param_scalar(item)) is not None
            ]
            if repeated:
                normalized[key] = repeated
            continue
        normalized_value = _normalize_param_scalar(value)
        if normalized_value is not None:
            normalized[key] = normalized_value
    return normalized


def _format_resources(resource_type: str, resources: list[dict]) -> str:
    """Format FHIR resources into a readable string for the agent."""
    if not resources:
        return f"No {resource_type} resources found."

    lines = [f"### {resource_type} ({len(resources)} results)"]
    for r in resources:
        # Use display-friendly summary based on resource type
        summary = _summarize_resource(r)
        lines.append(f"- {summary}")
    return "\n".join(lines)


def _summarize_resource(resource: dict) -> str:
    """Create a one-line summary of a FHIR resource."""
    summarize = _RESOURCE_SUMMARIZERS.get(resource.get("resourceType", "Unknown"))
    if summarize is None:
        # Fallback: dump as compact JSON
        return json.dumps(resource, default=str)[:200]
    return summarize(resource)


def _with_date(label: str, date: str) -> str:
    """Append a parenthesised date to a label when one is present."""
    return label + (f" ({date})" if date else "")


def _summarize_patient(resource: dict) -> str:
    names = resource.get("name", [])
    name = "Unknown"
    if names and isinstance(names[0], dict):
        given = " ".join(names[0].get("given", []))
        family = names[0].get("family", "")
        name = f"{given} {family}".strip()
    dob = resource.get("birthDate", "unknown DOB")
    gender = resource.get("gender", "unknown")
    return f"{name}, {gender}, DOB: {dob}"


def _summarize_condition(resource: dict) -> str:
    code = _extract_display(resource.get("code"))
    status_text = _extract_coding_text(resource.get("clinicalStatus", {}))
    onset = resource.get("onsetDateTime", "")
    return f"{code} (status: {status_text})" + (f" onset: {onset}" if onset else "")


def _summarize_observation(resource: dict) -> str:
    code = _extract_display(resource.get("code"))
    value = _extract_value(resource)
    return _with_date(f"{code}: {value}", resource.get("effectiveDateTime", ""))


def _medication_name(resource: dict) -> str:
    """Medication display from the codeable concept, else the reference."""
    med = resource.get("medicationCodeableConcept")
    if med:
        return _extract_display(med)
    med_ref = resource.get("medicationReference", {})
    return med_ref.get("display", med_ref.get("reference", "Unknown"))


def _summarize_medication_order(resource: dict) -> str:
    """MedicationRequest and MedicationStatement: name plus order status."""
    return f"{_medication_name(resource)} (status: {resource.get('status', '')})"


def _summarize_medication_event(resource: dict) -> str:
    """MedicationAdministration and MedicationDispense: name plus event date."""
    date = resource.get("effectiveDateTime", resource.get("whenHandedOver", ""))
    return _with_date(_medication_name(resource), date)


def _summarize_encounter(resource: dict) -> str:
    enc_type = resource.get("type", [])
    type_text = _extract_display(enc_type[0]) if enc_type else "encounter"
    return _with_date(type_text, resource.get("period", {}).get("start", ""))


def _summarize_procedure(resource: dict) -> str:
    code = _extract_display(resource.get("code"))
    date = resource.get("performedDateTime", "")
    period = resource.get("performedPeriod", {})
    if not date and period:
        date = period.get("start", "")
    return _with_date(code, date)


def _summarize_allergy(resource: dict) -> str:
    code = _extract_display(resource.get("code"))
    status_text = _extract_coding_text(resource.get("clinicalStatus", {}))
    return f"{code} (status: {status_text})"


def _summarize_immunization(resource: dict) -> str:
    code = _extract_display(resource.get("vaccineCode"))
    return _with_date(code, resource.get("occurrenceDateTime", ""))


_RESOURCE_SUMMARIZERS: dict[str, Any] = {
    "Patient": _summarize_patient,
    "Condition": _summarize_condition,
    "Observation": _summarize_observation,
    "MedicationRequest": _summarize_medication_order,
    "MedicationStatement": _summarize_medication_order,
    "MedicationAdministration": _summarize_medication_event,
    "MedicationDispense": _summarize_medication_event,
    "Encounter": _summarize_encounter,
    "Procedure": _summarize_procedure,
    "AllergyIntolerance": _summarize_allergy,
    "Immunization": _summarize_immunization,
}


def _extract_display(codeable_concept: dict | None) -> str:
    """Extract human-readable text from a FHIR CodeableConcept."""
    if not codeable_concept:
        return "Unknown"
    if isinstance(codeable_concept, dict):
        text = codeable_concept.get("text")
        if text:
            return text
        codings = codeable_concept.get("coding", [])
        if codings and isinstance(codings[0], dict):
            return codings[0].get("display", codings[0].get("code", "Unknown"))
    return "Unknown"


def _extract_coding_text(codeable_concept: dict | None) -> str:
    """Extract text from a status CodeableConcept (clinicalStatus etc)."""
    if not codeable_concept or not isinstance(codeable_concept, dict):
        return "unknown"
    codings = codeable_concept.get("coding", [])
    if codings and isinstance(codings[0], dict):
        return codings[0].get("code", "unknown")
    return codeable_concept.get("text", "unknown")


def _extract_value(observation: dict) -> str:
    """Extract the value from an Observation resource."""
    # valueQuantity
    vq = observation.get("valueQuantity")
    if vq and isinstance(vq, dict):
        val = vq.get("value", "")
        unit = vq.get("unit", vq.get("code", ""))
        return f"{val} {unit}".strip()

    # valueCodeableConcept
    vcc = observation.get("valueCodeableConcept")
    if vcc:
        return _extract_display(vcc)

    # valueString
    vs = observation.get("valueString")
    if vs:
        return vs

    # component (e.g., blood pressure)
    components = observation.get("component", [])
    if components:
        parts = []
        for c in components:
            code = _extract_display(c.get("code"))
            val = _extract_value(c)
            parts.append(f"{code}={val}")
        return "; ".join(parts)

    return "no value"


def _resource_datetime(resource: dict) -> datetime | None:
    """Extract the best available clinical timestamp for history filtering."""
    candidates = [
        resource.get("effectiveDateTime"),
        resource.get("issued"),
        resource.get("authoredOn"),
        resource.get("whenHandedOver"),
        resource.get("occurrenceDateTime"),
        resource.get("performedDateTime"),
    ]
    for period_key in ("effectivePeriod", "performedPeriod", "period"):
        period = resource.get(period_key)
        if isinstance(period, dict):
            candidates.append(period.get("start"))

    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _filter_recent_history(
    resource_type: str,
    resources: list[dict],
    *,
    recent_history_days: int | None,
    include_older_history: bool,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    """Filter dated historical resources while retaining undated records."""
    if (
        recent_history_days is None
        or include_older_history
        or resource_type not in RECENT_HISTORY_RESOURCES
    ):
        return resources, 0

    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=recent_history_days)
    retained = []
    excluded = 0
    for resource in resources:
        resource_time = _resource_datetime(resource)
        if resource_time is not None and resource_time < cutoff:
            excluded += 1
        else:
            retained.append(resource)
    return retained, excluded


def _history_anchor_time(
    resources: list[dict],
    history_anchor: HistoryAnchor,
) -> datetime | None:
    """Resolve the clock used for recent-history filtering."""
    if history_anchor == "wall_clock":
        return None

    timestamps = [
        timestamp
        for resource in resources
        if (timestamp := _resource_datetime(resource)) is not None
    ]
    return max(timestamps, default=None)


def _apply_result_limit(
    search_params: FHIRSearchParams,
    max_results_per_resource: int | None,
) -> FHIRSearchParams:
    """Apply the configured first-page result bound to FHIR search params.

    Requests one extra result beyond the bound so the caller can detect that
    matches were truncated (the sentinel row is dropped before formatting).
    """
    if max_results_per_resource is None:
        return search_params

    requested_count = search_params.get("_count")
    try:
        parsed_count = (
            int(requested_count) if requested_count and isinstance(requested_count, str) else None
        )
    except ValueError:
        parsed_count = None
    bounded_count = (
        min(parsed_count, max_results_per_resource)
        if parsed_count is not None
        else max_results_per_resource
    )
    return {**search_params, "_count": str(max(1, bounded_count) + 1)}


def _is_token_code(value: str) -> bool:
    """True when a code search value is a FHIR token the server can match."""
    if "|" in value:
        return True
    return not any(ch.isalpha() for ch in value)


def _split_code_filter(
    search_params: FHIRSearchParams,
) -> tuple[FHIRSearchParams, list[str]]:
    """Pop display-name code values out of the server search params.

    Token codes (``system|code`` or bare numeric codes) stay in the server
    params. Human-readable names (e.g. "Hematocrit") become client-side filter
    terms, because FHIR servers index token codes only and a name value would
    silently match nothing.
    """
    server_params = dict(search_params)
    terms: list[str] = []
    for key in CODE_FILTER_PARAMS:
        raw = server_params.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        parts = [part.strip() for value in values for part in str(value).split(",") if part.strip()]
        if parts and not all(_is_token_code(part) for part in parts):
            terms.extend(parts)
            del server_params[key]
    return server_params, terms


def _matches_code_filter(resource: dict, terms: list[str]) -> bool:
    """Match a resource's code against name terms (or exact bare codes).

    Medication resources carry their code in ``medicationCodeableConcept``
    (or a display on ``medicationReference``) rather than ``code``.
    """
    names: list[str] = []
    bare_codes: list[str] = []
    for concept in (resource.get("code"), resource.get("medicationCodeableConcept")):
        if not isinstance(concept, dict):
            continue
        text = concept.get("text")
        if isinstance(text, str):
            names.append(text.lower())
        for coding in concept.get("coding", []):
            if not isinstance(coding, dict):
                continue
            display = coding.get("display")
            if isinstance(display, str):
                names.append(display.lower())
            coding_code = coding.get("code")
            if isinstance(coding_code, str):
                bare_codes.append(coding_code)
    med_ref = resource.get("medicationReference")
    if isinstance(med_ref, dict) and isinstance(med_ref.get("display"), str):
        names.append(med_ref["display"].lower())
    for term in terms:
        lowered = term.lower()
        if term in bare_codes or any(lowered in name for name in names):
            return True
    return False


def create_fhir_query_tool(
    client: RestFHIRClient,
    raw_channel: RawResourceChannel | None = None,
    *,
    max_calls: int | None = None,
    max_resource_types_per_call: int | None = None,
    max_results_per_resource: int | None = None,
    recent_history_days: int | None = None,
    history_anchor: HistoryAnchor = "wall_clock",
) -> Any:
    """Create a query_patient_fhir tool bound to a specific FHIR client.

    Args:
        client: RestFHIRClient instance connected to a FHIR server.
        raw_channel: Optional side channel that receives the exact raw FHIR
            resources each call retrieved.  Defaults to the process-local channel
            from :mod:`medical_nudging.tools.raw_resource_channel`.  The tool's
            formatted return value is identical either way — the channel exists so
            the evidence-contract tool ledger can build one evidence span per raw
            resource instead of one span per prose blob.
        max_calls: Maximum external query rounds for this request
        max_resource_types_per_call: Maximum resource types in one round
        max_results_per_resource: Maximum first-page matches retained per resource type
        recent_history_days: Default history window for dated historical resources
        history_anchor: Clock used for the history window. ``latest_resource``
            supports deidentified datasets whose dates are shifted.

    Returns:
        A Strands @tool function that queries patient data.
    """
    channel = raw_channel if raw_channel is not None else default_channel()

    budget = CallBudget("query_patient_fhir", max_calls)

    @tool
    def query_patient_fhir(
        patient_id: str,
        resource_types: list[str] | None = None,
        params: dict[str, FHIRParamValue] | None = None,
        include_older_history: bool = False,
    ) -> str:
        """Query patient data from a FHIR R4 API.

        Makes targeted FHIR queries to retrieve specific clinical data for a patient.
        Use this instead of loading entire patient files — only fetch the resources
        you need for the current clinical context.

        Args:
            patient_id: The FHIR Patient resource ID.
            resource_types: List of FHIR resource types to query.
                Supported: Patient, Condition, Observation, MedicationRequest,
                MedicationAdministration, MedicationDispense, MedicationStatement,
                Encounter, Procedure, AllergyIntolerance, Immunization.
                If not specified, queries: Condition, MedicationRequest,
                AllergyIntolerance, and recent Observations.
            params: Optional additional FHIR search parameters to apply to ALL queries.
                E.g., {"date": "ge2025-01-01"} to filter by date.
                The "code" parameter accepts token codes ("system|code") or
                human-readable names (e.g. "Hematocrit" or "Hematocrit,INR");
                names are matched against code display text client-side, so use
                names for lab-trend queries such as
                {"code": "Hemoglobin,Hematocrit", "date": "le2025-01-01"}.
                Resource-specific defaults (like the laboratory category for
                Observations) are applied automatically but can be overridden here.
            include_older_history: Set true only when older context is clinically
                necessary. Otherwise dated historical resources are limited to the
                configured recent-history window.

        Returns:
            Formatted patient data organized by resource type.
        """
        sections: list[str] = []
        raw_resources: list[dict] = []
        normalized_params = _normalize_search_params(params)

        # Determine which resources to query
        if resource_types:
            unsupported = set(resource_types) - SUPPORTED_RESOURCES
            if unsupported:
                sections.append(f"Warning: unsupported resource types ignored: {unsupported}")
            query_types = [rt for rt in resource_types if rt in SUPPORTED_RESOURCES]
        else:
            query_types = list(DEFAULT_RESOURCE_QUERIES.keys())

        if (
            max_resource_types_per_call is not None
            and len(query_types) > max_resource_types_per_call
        ):
            return (
                "BUDGET_EXCEEDED: query_patient_fhir requested "
                f"{len(query_types)} resource types; the configured maximum is "
                f"{max_resource_types_per_call}. Narrow the query."
            )

        allowed, _remaining = budget.consume()
        if not allowed:
            return budget.exhausted_message()

        for resource_type in query_types:
            try:
                if resource_type == "Patient":
                    resource = client.read("Patient", patient_id)
                    raw_resources.append(resource)
                    sections.append(_format_resources("Patient", [resource]))
                    continue

                # Build search params: defaults + overrides
                search_params: FHIRSearchParams = {"patient": patient_id}
                defaults = DEFAULT_RESOURCE_QUERIES.get(resource_type, {})
                search_params.update(defaults)
                search_params.update(normalized_params)
                search_params, code_filter_terms = _split_code_filter(search_params)
                if code_filter_terms:
                    # Fetch a deeper window to filter by name client-side.
                    search_params["_count"] = NAME_FILTER_PAGE_COUNT
                    fetch_pages: int | None = NAME_FILTER_MAX_PAGES
                else:
                    search_params = _apply_result_limit(search_params, max_results_per_resource)
                    fetch_pages = 1 if max_results_per_resource is not None else None

                try:
                    resources = client.search(resource_type, search_params, max_pages=fetch_pages)
                except FHIRClientError as error:
                    if error.status_code != 400 or not normalized_params:
                        raise
                    fallback_params = _apply_result_limit(
                        {"patient": patient_id, **defaults},
                        max_results_per_resource,
                    )
                    logger.info(
                        "FHIR search %s rejected shared params; retrying with defaults",
                        resource_type,
                    )
                    if max_results_per_resource is not None:
                        resources = client.search(resource_type, fallback_params, max_pages=1)
                    else:
                        resources = client.search(resource_type, fallback_params)
                    sections.append(
                        f"Note: {resource_type} server rejected shared search parameters; "
                        "retried with resource defaults."
                    )
                if getattr(client, "last_search_hit_page_cap", False):
                    # Parsed into the tool ledger's page-capped record (coverage-v2):
                    # a truncated window cannot establish absence.
                    sections.append(
                        f"Page cap reached for {resource_type}: more result pages exist "
                        "beyond the retrieved window. Do not treat these results as "
                        "complete; add date or code filters to narrow the query."
                    )
                if code_filter_terms:
                    resources = [r for r in resources if _matches_code_filter(r, code_filter_terms)]
                truncated = (
                    max_results_per_resource is not None
                    and len(resources) > max_results_per_resource
                )
                if max_results_per_resource is not None:
                    resources = resources[:max_results_per_resource]
                resources, excluded = _filter_recent_history(
                    resource_type,
                    resources,
                    recent_history_days=recent_history_days,
                    include_older_history=include_older_history,
                    now=_history_anchor_time(resources, history_anchor),
                )
                # Publish the post-filter set: evidence spans must match exactly what
                # the agent was shown, not results the cap or history window dropped.
                raw_resources.extend(resources)
                sections.append(_format_resources(resource_type, resources))
                if code_filter_terms:
                    sections.append(
                        f"Note: code value(s) {code_filter_terms} were matched by name "
                        "client-side; the server indexes token codes (system|code) only."
                    )
                if truncated:
                    sections.append(
                        f"Result cap: showing {max_results_per_resource} {resource_type} "
                        "matches; MORE MATCHES EXIST beyond this cap. Add date or code "
                        "filters to reach the remaining results."
                    )
                if excluded:
                    sections.append(
                        f"History window excluded {excluded} older {resource_type} "
                        f"resource(s); set include_older_history=true only if needed."
                    )
            except Exception as e:
                logger.warning(f"Error querying {resource_type} for {patient_id}: {e}")
                sections.append(f"### {resource_type}\nError: {e}")

        # Side channel only: the raw resources go to the evidence-contract ledger, the
        # formatted sections go to the agent. The return value below is unchanged.
        channel.publish(
            tool_name="query_patient_fhir",
            query=f"query_patient_fhir(patient_id={patient_id!r}, resource_types={query_types!r})",
            resources=raw_resources,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )

        return "\n\n".join(sections)

    return query_patient_fhir


def create_fhir_query_tool_from_config(tool_limits: dict[str, Any] | None = None) -> Any | None:
    """Compose the HealthLake query tool from settings, or degrade to ``None``.

    This is the one place that knows how the FHIR data source is wired: settings
    (``fhir_api.enabled``, ``datastore_endpoint``, ``region``, ``max_pages``) become a
    SigV4 HealthLake client, and the request's ``fhir_query`` limits become the tool's
    budgets. ``None`` means the data source is off or could not be constructed; the
    caller keeps running without the tool and logs the reason.

    Args:
        tool_limits: Request-scoped budgets; the ``fhir_query`` section is used.

    Returns:
        The ``query_patient_fhir`` tool, or ``None``.
    """
    fhir_config = get_fhir_config()
    if not fhir_config.get("enabled"):
        return None

    datastore_endpoint = fhir_config.get("datastore_endpoint", "")
    if not datastore_endpoint:
        logger.warning("FHIR API enabled but no HealthLake datastore_endpoint configured")
        return None

    try:
        client = create_healthlake_client(
            datastore_endpoint=datastore_endpoint,
            region=fhir_config.get("region", "us-east-1"),
            max_pages=fhir_config.get("max_pages") or DEFAULT_MAX_PAGES,
        )
    except (ImportError, RuntimeError, OSError, FHIRAuthError) as e:
        logger.warning(f"FHIR tool creation failed: {e}")
        return None

    limits = (tool_limits or {}).get("fhir_query", {})
    query_tool = create_fhir_query_tool(
        client,
        max_calls=limits.get("max_calls"),
        max_resource_types_per_call=limits.get("max_resource_types_per_call"),
        max_results_per_resource=limits.get("max_results_per_resource"),
        recent_history_days=limits.get("recent_history_days"),
        history_anchor=limits.get("history_anchor", "wall_clock"),
    )
    logger.info(f"FHIR query tool created for HealthLake datastore {datastore_endpoint}")
    return query_tool
