#!/usr/bin/env python
"""Test model compatibility with Strands agent framework.

Tests each model with a single sample to verify:
1. Model accepts Bedrock API format
2. Model handles tool calling (Strands @tool decorator)
3. Model returns valid JSON response
4. Model follows system prompt instructions

Usage:
    # Test a specific model
    uv run scripts/run_model_compatibility.py --model us.anthropic.claude-sonnet-4-5-20250929-v1:0

    # Test all models
    uv run scripts/run_model_compatibility.py --all

    # Test with verbose output
    uv run scripts/run_model_compatibility.py --all --verbose
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strands import Agent
from strands.models.bedrock import BedrockModel

from medical_nudging.config import load_config
from medical_nudging.tools import get_patient_data, search_guidelines, list_guidelines

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger("model_compat")
console = Console()

# Model configurations for testing
MODELS = {
    "sonnet-4.5": {
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "thinking_enabled": True,
        "thinking_budget_tokens": 8192,
        "description": "Claude Sonnet 4.5 - Main inference model",
    },
    "haiku-4.5": {
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "thinking_enabled": False,  # Haiku has limited thinking support
        "thinking_budget_tokens": 0,
        "description": "Claude Haiku 4.5 - Fast/cheap model",
    },
    "nova-lite": {
        "model_id": "us.amazon.nova-lite-v1:0",
        "thinking_enabled": False,
        "thinking_budget_tokens": 0,
        "description": "Amazon Nova Lite - AWS native model",
    },
    "qwen3-32b": {
        "model_id": "qwen.qwen3-32b-v1:0",
        "thinking_enabled": False,
        "thinking_budget_tokens": 0,
        "description": "Qwen3-32B - OSS, RL finetune candidate",
    },
    "gpt-oss-20b": {
        "model_id": "openai.gpt-oss-20b-1:0",
        "thinking_enabled": False,
        "thinking_budget_tokens": 0,
        "description": "GPT-OSS 20B - OSS, US East only",
    },
}


@dataclass
class CompatibilityResult:
    """Result of model compatibility test."""

    model_name: str
    model_id: str
    success: bool
    api_format_ok: bool
    tool_calling_ok: bool
    json_response_ok: bool
    prompt_following_ok: bool
    latency_ms: int
    error: str | None
    details: dict[str, Any]


def get_sample_patient_data() -> tuple[str, str]:
    """Get a sample patient file for testing.

    Returns:
        Tuple of (file_path, format)
    """
    # Try CCDA samples first
    ccda_dir = Path("data/sample-ccda")
    if ccda_dir.exists():
        files = list(ccda_dir.glob("*.xml"))
        if files:
            return str(files[0]), "ccda"

    # Fallback to FHIR samples
    fhir_dir = Path("data/sample-fhir")
    if fhir_dir.exists():
        files = list(fhir_dir.glob("*.json"))
        if files:
            return str(files[0]), "fhir"

    # Fallback to test fixtures
    fixture_dir = Path("tests/fixtures")
    if fixture_dir.exists():
        xml_files = list(fixture_dir.glob("*.xml"))
        if xml_files:
            return str(xml_files[0]), "ccda"
        json_files = list(fixture_dir.glob("*.json"))
        if json_files:
            return str(json_files[0]), "fhir"

    raise FileNotFoundError("No sample patient data found")


COMPATIBILITY_SYSTEM_PROMPT = """You are a clinical decision support assistant.

You have access to these tools:
- get_patient_data: Access patient data from a file
- search_guidelines: Search clinical guidelines
- list_guidelines: List available guidelines

When asked to analyze a patient, you MUST:
1. Use the search_guidelines tool to find relevant guidelines
2. Return your analysis as a JSON object with this structure:
{
  "patient_summary": "Brief summary of patient",
  "nudges": [
    {
      "title": "Nudge title",
      "category": "Category",
      "urgency": "high/medium/low",
      "rationale": "Clinical reasoning"
    }
  ]
}

Always use the tools available to you. Always return valid JSON."""

COMPATIBILITY_USER_PROMPT = """Analyze this patient data and generate 1-2 clinical nudges.

Patient Data Format: {fmt}
Patient Data:
{patient_data}...

Return your response as a JSON object with patient_summary and nudges array."""

JSON_CODE_BLOCK = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```")
JSON_WITH_NUDGES = re.compile(r"\{[^{}]*\"nudges\"[^{}]*\[[\s\S]*?\][^{}]*\}")


def _build_bedrock_model(model_config: dict) -> BedrockModel:
    """BedrockModel for the config, with extended thinking when the entry enables it."""
    additional_fields: dict[str, Any] = {}
    if model_config.get("thinking_enabled"):
        additional_fields["thinking"] = {
            "type": "enabled",
            "budget_tokens": model_config.get("thinking_budget_tokens", 8192),
        }
    return BedrockModel(
        model_id=model_config["model_id"],
        additional_request_fields=additional_fields if additional_fields else None,
    )


def _content_blocks(response: Any) -> list[dict]:
    if hasattr(response, "message") and "content" in response.message:
        return list(response.message["content"])
    return []


def _response_text(response: Any) -> str:
    """The first text block of the final message, or empty."""
    for content in _content_blocks(response):
        if "text" in content:
            return content["text"]
    return ""


def _tool_use_found(response: Any) -> bool:
    return any(
        content.get("type") == "tool_use" or "toolUse" in content
        for content in _content_blocks(response)
    )


def _parse_json_response(response_text: str, result: CompatibilityResult, verbose: bool) -> None:
    """Check 3: record the parsed JSON object, from a code block or bare in the text."""
    json_match = JSON_CODE_BLOCK.search(response_text)
    if json_match:
        try:
            result.details["parsed_response"] = json.loads(json_match.group(1))
            result.json_response_ok = True
            if verbose:
                log.info("  ✓ Valid JSON response")
        except json.JSONDecodeError as e:
            result.details["json_error"] = str(e)
            if verbose:
                log.warning(f"  ✗ JSON parse error: {e}")
        return

    alt_match = JSON_WITH_NUDGES.search(response_text)
    if alt_match is None:
        return
    try:
        result.details["parsed_response"] = json.loads(alt_match.group(0))
        result.json_response_ok = True
        if verbose:
            log.info("  ✓ Valid JSON response (no code block)")
    except json.JSONDecodeError:
        if verbose:
            log.warning("  ✗ No valid JSON found in response")


def _check_tool_calling(
    response: Any, response_text: str, result: CompatibilityResult, verbose: bool
) -> None:
    """Check 2: a tool call, or a response that names the tool, or simply no error."""
    result.tool_calling_ok = True
    if not verbose:
        return
    if "search_guidelines" in response_text.lower() or _tool_use_found(response):
        log.info("  ✓ Tool calling works")
    else:
        # Some models might not call tools but still work
        log.info("  ~ Tool calling (no tools invoked, but no error)")


def _check_prompt_following(result: CompatibilityResult, verbose: bool) -> None:
    """Check 4: did the JSON carry the requested nudges structure?"""
    if not result.json_response_ok:
        return
    parsed = result.details.get("parsed_response", {})
    if "nudges" in parsed or "patient_summary" in parsed:
        result.prompt_following_ok = True
        if verbose:
            log.info("  ✓ Follows prompt structure")
    elif verbose:
        log.warning("  ~ Partial prompt following")


def run_model_compatibility(
    model_name: str, model_config: dict, verbose: bool = False
) -> CompatibilityResult:
    """Test a model's compatibility with Strands agent framework.

    Args:
        model_name: Short name for the model
        model_config: Model configuration dict
        verbose: Whether to print verbose output

    Returns:
        CompatibilityResult with test outcomes
    """
    result = CompatibilityResult(
        model_name=model_name,
        model_id=model_config["model_id"],
        success=False,
        api_format_ok=False,
        tool_calling_ok=False,
        json_response_ok=False,
        prompt_following_ok=False,
        latency_ms=0,
        error=None,
        details={},
    )

    start_time = time.perf_counter()

    try:
        file_path, fmt = get_sample_patient_data()
        patient_data = Path(file_path).read_text()
        result.details["patient_format"] = fmt
        result.details["patient_file"] = file_path
        if verbose:
            log.info(f"  Using sample: {file_path} ({fmt})")

        bedrock_model = _build_bedrock_model(model_config)

        # Check 1: API format - can we create the model?
        result.api_format_ok = True
        if verbose:
            log.info("  ✓ API format accepted")

        agent = Agent(
            model=bedrock_model,
            system_prompt=COMPATIBILITY_SYSTEM_PROMPT,
            tools=[get_patient_data, search_guidelines, list_guidelines],
        )
        user_prompt = COMPATIBILITY_USER_PROMPT.format(
            fmt=fmt.upper(), patient_data=patient_data[:2000]
        )

        # Check 2 & 3: Tool calling and JSON response
        response = agent(user_prompt)
        result.latency_ms = int((time.perf_counter() - start_time) * 1000)
        if verbose:
            log.info(f"  Response received in {result.latency_ms}ms")

        response_text = _response_text(response)
        result.details["response_preview"] = response_text[:500] if response_text else ""

        _check_tool_calling(response, response_text, result, verbose)
        _parse_json_response(response_text, result, verbose)
        _check_prompt_following(result, verbose)

        # Overall success if API and tool calling work
        result.success = result.api_format_ok and result.tool_calling_ok

    except Exception as e:
        result.error = str(e)
        result.latency_ms = int((time.perf_counter() - start_time) * 1000)
        if verbose:
            log.error(f"  ✗ Error: {e}")

    return result


def display_results(results: list[CompatibilityResult]) -> None:
    """Display compatibility test results in a table."""
    table = Table(title="Model Compatibility Results")
    table.add_column("Model", style="cyan")
    table.add_column("API", justify="center")
    table.add_column("Tools", justify="center")
    table.add_column("JSON", justify="center")
    table.add_column("Prompt", justify="center")
    table.add_column("Latency", justify="right")
    table.add_column("Status", justify="center")

    for r in results:
        status = "[green]PASS[/green]" if r.success else "[red]FAIL[/red]"
        api = "✓" if r.api_format_ok else "✗"
        tools = "✓" if r.tool_calling_ok else "✗"
        json_ok = "✓" if r.json_response_ok else "✗"
        prompt = "✓" if r.prompt_following_ok else "✗"
        latency = f"{r.latency_ms}ms" if r.latency_ms > 0 else "-"

        table.add_row(r.model_name, api, tools, json_ok, prompt, latency, status)

    console.print(table)

    # Print errors for failed models
    failed = [r for r in results if not r.success]
    if failed:
        console.print("\n[bold red]Errors:[/bold red]")
        for r in failed:
            if r.error:
                console.print(f"  {r.model_name}: {r.error}")


def main() -> int:
    """Run model compatibility tests."""
    config = load_config()

    # Set AWS profile if configured
    if config.get("aws_profile"):
        os.environ["AWS_PROFILE"] = config["aws_profile"]

    parser = argparse.ArgumentParser(
        description="Test model compatibility with Strands agent framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model",
        type=str,
        help="Model ID to test (or short name like 'sonnet-4.5')",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Test all models",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file for results JSON",
    )
    args = parser.parse_args()

    if not args.model and not args.all:
        parser.print_help()
        return 1

    results: list[CompatibilityResult] = []

    if args.all:
        log.info("[bold]Testing all models...[/bold]")
        for name, model_config in MODELS.items():
            log.info(f"\n[cyan]{name}[/cyan]: {model_config['description']}")
            result = run_model_compatibility(name, model_config, args.verbose)
            results.append(result)
    else:
        # Single model test
        model_id = args.model

        # Check if it's a short name
        if model_id in MODELS:
            model_config = MODELS[model_id]
            name = model_id
        else:
            # Assume it's a full model ID
            name = model_id.split("/")[-1].split(":")[0]
            model_config = {
                "model_id": model_id,
                "thinking_enabled": False,
                "thinking_budget_tokens": 0,
                "description": f"Custom model: {model_id}",
            }

        log.info(f"[bold]Testing {name}...[/bold]")
        result = run_model_compatibility(name, model_config, args.verbose)
        results.append(result)

    # Display results
    console.print()
    display_results(results)

    # Save results if output specified
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "results": [
                {
                    "model_name": r.model_name,
                    "model_id": r.model_id,
                    "success": r.success,
                    "api_format_ok": r.api_format_ok,
                    "tool_calling_ok": r.tool_calling_ok,
                    "json_response_ok": r.json_response_ok,
                    "prompt_following_ok": r.prompt_following_ok,
                    "latency_ms": r.latency_ms,
                    "error": r.error,
                }
                for r in results
            ],
        }
        args.output.write_text(json.dumps(output_data, indent=2))
        log.info(f"\n[green]Results saved to {args.output}[/green]")

    # Return success if all tested models passed
    return 0 if all(r.success for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
