"""Export the OpenAPI document and the event JSON Schemas for the frontend's type generation.

CI regenerates these and fails on any diff, so the backend and frontend cannot drift (spec 6.4).
"""

from __future__ import annotations

import json
from pathlib import Path

from src.api.app import create_app
from src.config import Settings
from src.core.events import EVENT_ADAPTER


def build_openapi() -> dict[str, object]:
    """The OpenAPI document with a fixed local configuration (so the output is deterministic)."""
    app = create_app(Settings(access_mode="local", _env_file=None))
    app.openapi_url = "/api/v1/openapi.json"
    schema: dict[str, object] = app.openapi()
    return schema


def build_event_schema() -> dict[str, object]:
    """One JSON Schema covering every event, discriminated by ``type``."""
    schema: dict[str, object] = EVENT_ADAPTER.json_schema(mode="serialization")
    return schema


def export_schemas(out_dir: Path) -> list[Path]:
    """Write ``openapi.json`` and ``events.schema.json`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (
        ("openapi.json", build_openapi()),
        ("events.schema.json", build_event_schema()),
    ):
        path = out_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
