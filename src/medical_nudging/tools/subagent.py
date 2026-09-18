"""Subagent invocation tool for medical nudging system."""

import logging
from typing import Any, List, Optional

from strands import Agent, tool

logger = logging.getLogger(__name__)


@tool
def invoke_subagent(
    prompt: str,
    system_prompt: str,
    tools: Optional[List[str]] = None,
    agent: Optional[Any] = None,
) -> str:
    """Invoke an isolated subagent with specified prompt and system instructions.

    Creates a new Agent instance with filtered tools from parent, executes the
    prompt, and returns the result. Useful for delegating specialized subtasks
    within the medical nudging pipeline.

    Args:
        prompt: Task for subagent to perform
        system_prompt: Role and instructions for subagent
        tools: Optional list of tool names to provide subagent (filters from parent).
            If None, inherits all parent tools except invoke_subagent.
        agent: Parent agent context (automatically injected by Strands)

    Returns:
        String with the subagent's response text.
        On error, returns an error message string.

    Examples:
        # Invoke subagent with specific tools
        result = agent.tool.invoke_subagent(
            prompt="Summarize patient medications",
            system_prompt="You are a medication review specialist.",
            tools=["search_guidelines"]
        )

        # Invoke with all parent tools
        result = agent.tool.invoke_subagent(
            prompt="Analyze lab results",
            system_prompt="You are a lab results analyzer."
        )
    """
    try:
        # Get tools and configuration from parent agent
        filtered_tools = []
        trace_attributes = {}
        extra_kwargs = {}

        if agent:
            trace_attributes = agent.trace_attributes
            extra_kwargs["callback_handler"] = agent.callback_handler

            # Filter tools - exclude invoke_subagent to prevent recursion
            if tools is not None:
                # Use specified tool names
                for tool_name in tools:
                    if tool_name in agent.tool_registry.registry:
                        filtered_tools.append(agent.tool_registry.registry[tool_name])
                    else:
                        logger.warning(
                            f"Tool '{tool_name}' not found in parent agent's tool registry"
                        )
            else:
                # Inherit all tools except invoke_subagent
                filtered_tools = [
                    tool_obj
                    for name, tool_obj in agent.tool_registry.registry.items()
                    if name != "invoke_subagent"
                ]

        # Use parent agent's model
        model = agent.model if agent else None

        logger.debug(f"\nSubagent prompt: {prompt[:100]}...")
        logger.debug("Creating isolated subagent instance...")

        # Create new Agent instance with filtered tools
        subagent = Agent(
            model=model,
            messages=[],
            tools=filtered_tools,
            system_prompt=system_prompt,
            trace_attributes=trace_attributes,
            **extra_kwargs,
        )

        # Execute prompt
        result = subagent(prompt)

        # Extract response text from agent message structure
        # Skip reasoningContent blocks (adaptive thinking) to find the text block
        if hasattr(result, "message") and result.message:
            content = result.message.get("content", [])
            response_text = str(result)
            if content and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        response_text = block["text"]
                        break
        else:
            response_text = str(result)
        logger.debug(f"Subagent response: {response_text[:100]}...")

        return response_text

    except Exception as e:
        error_msg = f"ERROR: Error in invoke_subagent: {str(e)}"
        logger.error(error_msg)
        return error_msg
