"""Schema helpers for structured outputs and tool definitions (spec 7.6).

Providers accept different subsets of JSON Schema. The strictest common subset used for
structured outputs is: every property required, ``additionalProperties: false``, optional values
expressed as nullable unions, ``$ref`` inlined, and no ``default``/``title`` noise. Results are
always re-validated with Pydantic afterwards; a validation failure is the safe outcome.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from src.core.errors import StructuredOutputError

T = TypeVar("T", bound=BaseModel)

_DROP_KEYS = {"title", "default", "examples", "$defs", "definitions", "discriminator"}
# Keywords that strict constrained decoders reject on some providers.
_UNSUPPORTED_STRICT = {
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "multipleOf",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}


def _inline_refs(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                raise ValueError(f"recursive schema reference {name} is not supported")
            target = defs.get(name)
            if target is None:
                raise ValueError(f"unresolvable schema reference {ref}")
            merged = {
                **_inline_refs(target, defs, (*seen, name)),
                **{k: v for k, v in node.items() if k != "$ref"},
            }
            return merged
        return {k: _inline_refs(v, defs, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs, seen) for v in node]
    return node


def _clean(node: Any, props_map: bool = False) -> Any:
    """Drop noise keywords. ``props_map`` marks a ``properties`` mapping, whose keys are names."""
    if isinstance(node, dict):
        if props_map:
            return {name: _clean(sub) for name, sub in node.items()}
        return {
            key: _clean(value, props_map=(key == "properties"))
            for key, value in node.items()
            if key not in _DROP_KEYS
        }
    if isinstance(node, list):
        return [_clean(item) for item in node]
    return node


def simplify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref``, drop titles and defaults. Keeps validation keywords (for tool schemas)."""
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    result: dict[str, Any] = _clean(_inline_refs(schema, defs))
    return result


def to_strict_schema(model: type[BaseModel], *, keep_constraints: bool = False) -> dict[str, Any]:
    """Build the strictest-common-subset schema for ``model``.

    Every property becomes required; properties that were optional become ``T | null``.
    Constraint keywords that strict decoders reject are dropped unless ``keep_constraints``.
    """
    raw = model.model_json_schema()
    schema = simplify_schema(raw)

    def strictify(node: Any) -> Any:
        if isinstance(node, list):
            return [strictify(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {
            k: strictify(v)
            for k, v in node.items()
            if keep_constraints or k not in _UNSUPPORTED_STRICT
        }
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties", {})
            required = set(out.get("required", []))
            new_props: dict[str, Any] = {}
            for name, prop in props.items():
                new_props[name] = prop if name in required else _nullable(prop)
            out["properties"] = new_props
            out["required"] = list(new_props.keys())
            out["additionalProperties"] = False
        return out

    return strictify(schema)  # type: ignore[no-any-return]


def _nullable(prop: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in prop:
        options = prop["anyOf"]
        if not any(o.get("type") == "null" for o in options):
            return {**prop, "anyOf": [*options, {"type": "null"}]}
        return prop
    kind = prop.get("type")
    if isinstance(kind, list):
        return prop if "null" in kind else {**prop, "type": [*kind, "null"]}
    if kind is not None:
        return {**prop, "type": [kind, "null"]}
    return {"anyOf": [prop, {"type": "null"}]}


def validate_structured(model: type[T], payload: str | dict[str, Any]) -> T:
    """Parse and validate a model's structured output, raising ``StructuredOutputError``."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
        return model.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise StructuredOutputError(
            f"model output did not validate against {model.__name__}", detail=str(exc)[:300]
        ) from exc
