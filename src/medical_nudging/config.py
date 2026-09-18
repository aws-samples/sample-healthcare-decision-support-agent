"""Configuration loader for Medical Nudging.

The one settings seam for the package, the scripts, and the evaluation runner.
Loads ``config/settings.yaml`` for local development. Precedence, stated once:
environment variable > settings.yaml > default. Environment variables exist for
Lambda/AgentCore deployments; the yaml file is for local runs.

The loaded settings are cached for the process. Anything that needs to change
them at runtime (an experiment config, a CLI flag) goes through :func:`override`
so the mutation is explicit and lands in the one cache every reader shares.
"""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

_CONFIG_FILE = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
_PROJECT_ROOT = Path(__file__).parent.parent.parent


def get_project_root() -> Path:
    """Get project root directory path."""
    return _PROJECT_ROOT


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load configuration from settings.yaml with caching.

    Returns:
        Configuration dictionary, empty dict if file not found
    """
    if _CONFIG_FILE.exists():
        with open(_CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def get_value(yaml_key: str, env_var: str | None = None, default: Any = None) -> Any:
    """Get config value with env var override.

    Priority: env var > settings.yaml > default

    Args:
        yaml_key: Key in settings.yaml (supports nested with dots, e.g., 'agent.search_backend')
        env_var: Environment variable name (optional)
        default: Default value if neither source has the value
    """
    # Env var takes priority (for deployed environments)
    if env_var and env_var in os.environ:
        return os.environ[env_var]

    # Fall back to config file
    config = load_config()
    keys = yaml_key.split(".")
    value = config
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = None
            break

    return value if value is not None else default


def override(values: Mapping[str, Any], *, replace: bool = False) -> None:
    """Apply runtime settings on top of the cached configuration.

    Top-level sections that are mappings on both sides are merged key by key;
    everything else is assigned. With ``replace=True`` each given section
    replaces the cached one wholesale. Every reader of ``load_config()`` and the
    ``get_*`` accessors sees the result, so callers never touch the cache directly.
    """
    config = load_config()
    for key, value in values.items():
        current = config.get(key)
        if not replace and isinstance(current, dict) and isinstance(value, Mapping):
            current.update(value)
        else:
            config[key] = dict(value) if isinstance(value, Mapping) else value


EXPERIMENTS_DIR = _PROJECT_ROOT / "config" / "exps"
EXPERIMENT_RESULTS_ROOT = Path("results") / "experiments"

# Experiment sections that are settings for the agent runtime. Everything else in
# an experiment file (``inference``, ``context``) parameterizes the batch run and
# is read by the script that owns the loop.
_EXPERIMENT_SETTINGS_SECTIONS = ("model", "agent", "opensearch_index")


def load_experiment_config(path: Path) -> dict[str, Any]:
    """Read one experiment YAML (``config/exps/*.yaml``) and normalize its header.

    The file is a mapping. ``experiment.name`` defaults to the file stem so every
    run has an output directory name.

    Raises:
        OSError: the file cannot be read
        ValueError: the file is not valid YAML or not a mapping
    """
    path = Path(path)
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: invalid YAML ({error})") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: experiment config must be a YAML mapping")
    header = data.get("experiment")
    header = dict(header) if isinstance(header, dict) else {}
    header.setdefault("name", path.stem)
    data["experiment"] = header
    return data


def apply_experiment_config(experiment: Mapping[str, Any]) -> None:
    """Push an experiment's runtime settings into the cached configuration.

    ``model`` and ``agent`` merge over settings.yaml, ``opensearch_index`` replaces
    it, and ``inference.fhir_api.datastore_endpoint`` (or the legacy ``base_url``)
    enables the HealthLake data source. An empty endpoint keeps the configured one.
    """
    override({key: experiment[key] for key in _EXPERIMENT_SETTINGS_SECTIONS if key in experiment})
    inference = experiment.get("inference")
    fhir = inference.get("fhir_api") if isinstance(inference, Mapping) else None
    if isinstance(fhir, Mapping):
        fhir_settings: dict[str, Any] = {"enabled": True}
        endpoint = fhir.get("datastore_endpoint") or fhir.get("base_url")
        if endpoint:
            fhir_settings["datastore_endpoint"] = endpoint
        override({"fhir_api": fhir_settings})


def experiment_output_dir(experiment: Mapping[str, Any]) -> Path:
    """Where a run of this experiment writes its report and traces."""
    return EXPERIMENT_RESULTS_ROOT / str(experiment["experiment"]["name"])


_REGION_IN_HOST = re.compile(r"(?:^|\.)([a-z]{2}(?:-gov)?-[a-z]+-\d)\.(?:[a-z]+\.)?amazonaws\.com$")


def aws_region_from_endpoint(endpoint: str | None) -> str | None:
    """The region an AWS service endpoint lives in, read from its hostname.

    ``healthlake.us-east-1.amazonaws.com`` and ``abc.us-east-1.aoss.amazonaws.com``
    both yield ``us-east-1``. SigV4 signatures must use the endpoint's region, so a
    process-wide ``AWS_REGION`` set for some other purpose must not be used for
    regional endpoints whose region is already in the URL.
    """
    if not endpoint:
        return None
    host = endpoint.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    match = _REGION_IN_HOST.search(host)
    return match.group(1) if match else None


def observability_enabled() -> bool:
    """Whether the deployed observability pipeline (SNS events, hook metrics) is on.

    Deployment-only switch: ``OBSERVABILITY_ENABLED=true``. Parsed here once so the
    publisher and the agent builder cannot disagree on the default.
    """
    return os.environ.get("OBSERVABILITY_ENABLED", "false").strip().lower() == "true"


def get_guidelines_path() -> Path:
    """Get guidelines directory path.

    Returns:
        Path to guidelines directory
    """
    guidelines_path = get_value("guidelines_path", "GUIDELINES_PATH", "guidelines/")
    path = Path(guidelines_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def get_prompts_path() -> Path:
    """Get prompts directory path.

    Returns:
        Path to prompts directory
    """
    prompts_path = get_value("prompts_path", "PROMPTS_PATH", "prompts/")
    path = Path(prompts_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def get_model_config() -> dict[str, Any]:
    """Get model configuration.

    Returns:
        Dict with keys: model_id, thinking_type, effort,
                       cache_system_prompt, extra_headers, temperature
    """
    config = load_config()
    model_config = config.get("model", {})

    return {
        "model_id": get_value(
            "model.model_id", "MODEL_ID", "us.anthropic.claude-sonnet-5"
        ),
        "thinking_type": model_config.get("thinking_type", "adaptive"),
        "effort": model_config.get("effort", "high"),
        "budget_tokens": model_config.get("budget_tokens"),
        "cache_system_prompt": model_config.get("cache_system_prompt", True),
        "extra_headers": model_config.get("extra_headers", {}),
        "temperature": model_config.get("temperature"),
        "max_tokens": model_config.get("max_tokens", 32000),
        "read_timeout": model_config.get("read_timeout", 600),
    }


def get_aws_config() -> dict[str, Any]:
    """Get AWS configuration.

    Returns:
        Dict with keys: aws_profile, aws_region, agent_arn
    """
    return {
        "aws_profile": get_value("aws_profile", "AWS_PROFILE"),
        "aws_region": get_value("aws_region", "AWS_REGION", "us-east-1"),
        "agent_arn": get_value("agent_arn"),
    }


def get_opensearch_config() -> dict[str, Any]:
    """Get OpenSearch configuration.

    Returns:
        Dict with keys: opensearch_endpoint, opensearch_index
    """
    return {
        "opensearch_endpoint": get_value("opensearch_endpoint", "OPENSEARCH_ENDPOINT"),
        "opensearch_index": get_value("opensearch_index", "OPENSEARCH_INDEX_NAME", "guidelines"),
    }


def get_agent_config() -> dict[str, Any]:
    """Get agent configuration.

    Returns:
        Dict with keys: max_nudges, search_backend, tool_limits
    """
    config = load_config()
    agent_config = config.get("agent", {})

    return {
        "max_nudges": int(get_value("agent.max_nudges", "MAX_NUDGES", 5)),
        "search_backend": get_value("agent.search_backend", "SEARCH_BACKEND", "auto"),
        "tool_limits": agent_config.get("tool_limits", {}),
    }


def get_search_backend_type() -> str:
    """Get search backend type.

    Returns:
        Backend type: 'opensearch', 'ripgrep', or 'auto'
    """
    return get_value("agent.search_backend", "SEARCH_BACKEND", "auto")


def get_fhir_config() -> dict[str, Any]:
    """Get FHIR API configuration (AWS HealthLake).

    ``datastore_endpoint`` must be the datastore's ``DatastoreEndpoint`` as
    returned by HealthLake (terraform output ``healthlake_datastore_endpoint``).
    Never assemble the URL by hand.

    Returns:
        Dict with keys: enabled, datastore_endpoint, region, max_pages. ``region`` is
        the one in the endpoint hostname when present.
    """
    raw_enabled = get_value("fhir_api.enabled", "FHIR_API_ENABLED", False)
    # Normalize: YAML gives bool, env var gives str
    enabled = (
        raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).lower() in ("true", "1")
    )
    endpoint = get_value(
        "fhir_api.datastore_endpoint", "HEALTHLAKE_DATASTORE_ENDPOINT", ""
    ) or get_value("fhir_api.base_url", "FHIR_API_BASE_URL", "")
    raw_max_pages = get_value("fhir_api.max_pages", "FHIR_MAX_PAGES")
    try:
        max_pages = int(raw_max_pages) if raw_max_pages is not None else None
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Ignoring non-numeric fhir_api.max_pages / FHIR_MAX_PAGES value %r", raw_max_pages
        )
        max_pages = None
    return {
        "enabled": enabled,
        "datastore_endpoint": endpoint,
        # Sign for the datastore's own region; AWS_REGION only decides when the
        # endpoint does not name one.
        "region": aws_region_from_endpoint(endpoint)
        or get_value("aws_region", "AWS_REGION", "us-east-1"),
        "max_pages": max_pages,
    }
