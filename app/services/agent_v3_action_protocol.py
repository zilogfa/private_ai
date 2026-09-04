"""ATLAS v3.6 structured action protocol.

Local models are allowed to reason imperfectly, but the control plane must not
confuse free-form model text with an executable Agent action.  This module owns
small JSON schemas plus deterministic extraction/schema validation helpers used
by the v3 model gateway.

The protocol intentionally stays dependency-free so ATLAS does not need a
third-party JSON-schema package simply to validate its own control messages.
"""

import json
import re


FILE_CHANGE_SCHEMA = {
    "type": "object",
    "required": ["filename", "content"],
    "properties": {
        "filename": {"type": "string"},
        # A .json file may naturally be returned as an object/array; the Node
        # adapter canonicalizes it before any staged mutation is considered.
        "content": {"type": ["string", "object", "array"]},
        "reason": {"type": "string"},
    },
    "additionalProperties": True,
}

BUILD_ACTION_SCHEMA = {
    "type": "object",
    "required": ["files"],
    "properties": {
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "minItems": 1,
            "items": FILE_CHANGE_SCHEMA,
        },
    },
    "additionalProperties": True,
}

REPAIR_ACTION_SCHEMA = {
    "type": "object",
    "required": ["changes"],
    "properties": {
        "diagnosis": {"type": "string"},
        "hypothesis": {"type": "string"},
        "changes": {
            "type": "array",
            "minItems": 1,
            "items": FILE_CHANGE_SCHEMA,
        },
    },
    "additionalProperties": True,
}

DEFECT_ACTION_SCHEMA = {
    "type": "object",
    "required": ["changes"],
    "properties": {
        "changes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": FILE_CHANGE_SCHEMA,
        },
    },
    "additionalProperties": True,
}

GENERIC_OBJECT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
}


class V3ProtocolError(Exception):
    pass


def schema_text(schema):
    return json.dumps(schema or GENERIC_OBJECT_SCHEMA, ensure_ascii=False, separators=(",", ":"))


def _strip_fence(value):
    text = str(value or "").strip()
    # Some local reasoning models expose a <think>...</think> preamble even
    # when JSON mode is requested.  It is reasoning transport, not action data.
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _balanced_object_slice(text):
    """Return the first balanced JSON object from prose/fences.

    Braces inside quoted JSON strings are ignored.  This is intentionally a
    parser/extractor only; ATLAS never invents or repairs missing source text in
    this stage.
    """
    value = str(text or "")
    start = value.find("{")
    if start < 0:
        return None

    depth = 0
    quote = False
    escaped = False
    for index in range(start, len(value)):
        ch = value[index]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = False
            continue
        if ch == '"':
            quote = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return value[start:index + 1]
    return None


def parse_object(text, label="v3 action"):
    value = _strip_fence(text)
    candidates = [value]
    balanced = _balanced_object_slice(value)
    if balanced and balanced != value:
        candidates.append(balanced)

    last_error = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
            continue
        if not isinstance(parsed, dict):
            raise V3ProtocolError(f"The {label} returned a non-object JSON value.")
        return parsed

    if last_error is not None:
        detail = f" at line {last_error.lineno} column {last_error.colno}: {last_error.msg}"
    else:
        detail = ""
    raise V3ProtocolError(f"The {label} did not return valid JSON{detail}.")


def _matches_type(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value, schema, path="$", errors=None):
    """Validate the small JSON-schema subset used by ATLAS action contracts."""
    if errors is None:
        errors = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, item) for item in allowed):
            errors.append(f"{path}: expected {'/'.join(str(item) for item in allowed)}")
            return errors

    if "enum" in schema and value not in schema.get("enum", []):
        errors.append(f"{path}: value is not one of the allowed enum values")

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required field '{key}'")
        properties = schema.get("properties") or {}
        for key, child in properties.items():
            if key in value:
                validate_schema(value[key], child, f"{path}.{key}", errors)
        if schema.get("additionalProperties") is False:
            extras = [key for key in value if key not in properties]
            for key in extras:
                errors.append(f"{path}: unexpected field '{key}'")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < int(minimum):
            errors.append(f"{path}: expected at least {minimum} item(s)")
        if maximum is not None and len(value) > int(maximum):
            errors.append(f"{path}: expected at most {maximum} item(s)")
        child = schema.get("items")
        if child:
            for index, item in enumerate(value):
                validate_schema(item, child, f"{path}[{index}]", errors)
    return errors


def parse_and_validate(text, schema=None, label="v3 action"):
    parsed = parse_object(text, label=label)
    errors = validate_schema(parsed, schema or GENERIC_OBJECT_SCHEMA)
    if errors:
        joined = "; ".join(errors[:12])
        raise V3ProtocolError(f"The {label} violated its action schema: {joined}")
    return parsed


def raw_preview(text, limit=3500):
    value = str(text or "")
    if len(value) <= limit:
        return value
    head = max(500, limit // 2)
    tail = max(500, limit - head - 80)
    return value[:head] + "\n[... protocol preview truncated ...]\n" + value[-tail:]
