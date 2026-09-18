"""Stateless calculation tool backed by isolated Strands Shell invocations."""

from __future__ import annotations

import shlex
from typing import Any, cast

from strands import tool
from strands_shell import Limits, Shell


def create_calculator_tool() -> Any:
    """Create a calculator tool for one nudge-generation request."""

    @tool
    def calculate(script: str) -> str:
        """Run a small calculation in a fresh, isolated Lua 5.4 shell.

        Use ``print(...)`` to return results. This tool has no host filesystem,
        network, credentials, package installation, or external processes.

        Args:
            script: Lua statements for arithmetic, dates, percentages, or trends.

        Returns:
            Printed calculation results, or a concise error message.
        """
        shell = Shell(
            timeout=5.0,
            limits=Limits(
                max_output=16 * 1024,
                max_file_size=16 * 1024,
                max_fds=8,
                max_bg_jobs=0,
                max_pipeline=4,
                max_input=16 * 1024,
                max_inodes=128,
                max_depth=8,
            ),
        )
        result = shell.run(f"lua -e {shlex.quote(script)}")
        if result.status != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            return f"Calculation failed: {detail}"
        return cast(str, result.stdout).strip()

    return calculate
