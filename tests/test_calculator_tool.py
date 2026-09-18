"""Integration tests for isolated calculation tool invocations."""

from concurrent.futures import ThreadPoolExecutor

from medical_nudging.tools.calculator import create_calculator_tool
from medical_nudging.agents.agent_builder import build_tool_list


def test_calculator_executes_a_clinical_trend_calculation():
    calculate = create_calculator_tool()

    result = calculate(script="print(string.format('%.1f', (13.1 - 11.0) / 13.1 * 100))")

    assert result == "16.0"


def test_calculator_can_run_on_the_strands_worker_thread():
    calculate = create_calculator_tool()

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(calculate, script="print(6 * 7)").result()

    assert result == "42"


def test_calculator_does_not_expose_host_credentials(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sentinel-secret")
    monkeypatch.setenv("AWS_PROFILE", "sentinel-profile")
    calculate = create_calculator_tool()

    result = calculate(
        script="print(os.getenv('AWS_SECRET_ACCESS_KEY')); print(os.getenv('AWS_PROFILE'))"
    )

    assert result == "nil\nnil"


def test_each_calculation_gets_an_isolated_filesystem():
    first_tools = build_tool_list("no_guidelines")
    second_tools = build_tool_list("no_guidelines")
    first = next(tool for tool in first_tools if tool.__name__ == "calculate")
    second = next(tool for tool in second_tools if tool.__name__ == "calculate")

    assert first is not second
    assert (
        first(
            script=(
                "local f = io.open('/tmp/patient_value', 'w'); "
                "f:write('41'); f:close(); print(41 + 1)"
            )
        )
        == "42"
    )
    assert (
        first(
            script=(
                "local ok = pcall(function() io.open('/tmp/patient_value', 'r') end); " "print(ok)"
            )
        )
        == "false"
    )
    assert (
        second(
            script=(
                "local ok = pcall(function() io.open('/tmp/patient_value', 'r') end); " "print(ok)"
            )
        )
        == "false"
    )
