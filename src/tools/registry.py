"""The tool registry: registration, duplicate checks, and provider-specific schema export.

Original brief deliverable (spec 9): ``src/tools/registry.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.config import Settings
from src.tools.base import Tool


class ToolRegistry:
    """Holds every available tool and exports their schemas."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register ``tool``; raise on a duplicate name or an unnamed tool."""
        name = getattr(tool, "name", "")
        if not name or not isinstance(name, str):
            raise ValueError(f"{type(tool).__name__} has no tool name")
        if name in self._tools:
            raise ValueError(f"duplicate tool name: {name}")
        if not getattr(tool, "description", ""):
            raise ValueError(f"tool {name} has no description")
        self._tools[name] = tool

    def register_all(self, tools: Iterable[Tool]) -> None:
        """Register several tools."""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        """Return a tool by name."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Return every registered name in registration order."""
        return list(self._tools)

    def all(self) -> list[Tool]:
        """Return every registered tool."""
        return list(self._tools.values())

    def enabled(self, settings: Settings, disabled_at_runtime: Iterable[str] = ()) -> list[Tool]:
        """Return tools enabled by ``ENABLED_TOOLS`` and not toggled off in Settings."""
        off = set(disabled_at_runtime)
        return [
            t for t in self._tools.values() if settings.tool_enabled(t.name) and t.name not in off
        ]

    def as_tool_schemas(
        self,
        provider: str,
        settings: Settings | None = None,
        disabled_at_runtime: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Export enabled tools in the shape ``provider`` expects.

        * ``anthropic``: ``{name, description, input_schema}``
        * ``groq`` / ``openai``: ``{type: "function", function: {name, description, parameters}}``
        * ``gemini``: ``{name, description, parameters_json_schema}`` (a FunctionDeclaration)
        """
        tools = self.enabled(settings, disabled_at_runtime) if settings is not None else self.all()
        out: list[dict[str, Any]] = []
        for tool in tools:
            schema = tool.json_schema()
            if provider == "anthropic":
                out.append(
                    {"name": tool.name, "description": tool.description, "input_schema": schema}
                )
            elif provider in {"groq", "openai"}:
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": schema,
                        },
                    }
                )
            elif provider == "gemini":
                out.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters_json_schema": schema,
                    }
                )
            else:
                raise ValueError(f"unknown provider for tool schemas: {provider}")
        return out
