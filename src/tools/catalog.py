"""The default tool set (spec 11): every tool the agent can use, registered in one place."""

from __future__ import annotations

from src.tools.attachments import AttachmentReader
from src.tools.cognition import ProposeMemory, RecallChat, RecallMemories, SearchPastChats
from src.tools.forge import ArtifactForge
from src.tools.ping import NetworkPingDiagnostic
from src.tools.registry import ToolRegistry
from src.tools.sandbox import IsolatedPythonSandbox
from src.tools.scribe import FileSystemScribe
from src.tools.search import WebOmniscienceSearch
from src.tools.telemetry import HostTelemetryMonitor


def build_registry() -> ToolRegistry:
    """Create a registry holding the five core tools, the forge, the reader, and cognition tools."""
    registry = ToolRegistry()
    registry.register_all(
        [
            WebOmniscienceSearch(),
            IsolatedPythonSandbox(),
            HostTelemetryMonitor(),
            FileSystemScribe(),
            NetworkPingDiagnostic(),
            SearchPastChats(),
            RecallChat(),
            RecallMemories(),
            ProposeMemory(),
            AttachmentReader(),
            ArtifactForge(),
        ]
    )
    return registry
