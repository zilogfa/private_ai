"""Normalized Node.js failure evidence for the ATLAS v3 control plane.

Node can render the same underlying failure in several ways: ordinary stderr,
TAP YAML-ish diagnostics, stack traces, or test-runner metadata.  Agent policy
must consume normalized facts rather than brittle renderer-specific strings.

This module is intentionally pure: it does not mutate workspaces and has no
model/runtime dependencies.  Future Node/TypeScript intelligence can build on
the same normalized evidence contract.
"""

import os
import re


_UNDEFINED_PATTERNS = (
    re.compile(r"\bReferenceError:\s*([A-Za-z_$][A-Za-z0-9_$]*)\s+is not defined\b", re.I),
    re.compile(r"\berror:\s*['\"]([A-Za-z_$][A-Za-z0-9_$]*)\s+is not defined['\"]", re.I),
    re.compile(r"\berror:\s*([A-Za-z_$][A-Za-z0-9_$]*)\s+is not defined\b", re.I),
)

_LOCATION_PATTERNS = (
    re.compile(r"\blocation:\s*['\"]([^'\"\n]+\.(?:js|mjs|cjs|ts|tsx)):(\d+):(\d+)['\"]", re.I),
    re.compile(r"(?:file://)?(?:/runtime/|/workspace/)?([^\s():'\"]+\.(?:js|mjs|cjs|ts|tsx)):(\d+):(\d+)", re.I),
)


def _combined_text(execution):
    execution = execution or {}
    return "\n".join(
        str(execution.get(key) or "")
        for key in ("stdout", "stderr")
    )


def _locations(text):
    result = []
    seen = set()
    for pattern in _LOCATION_PATTERNS:
        for match in pattern.finditer(text):
            raw = str(match.group(1) or "").replace("\\", "/")
            filename = os.path.basename(raw)
            item = {
                "filename": filename,
                "path": raw,
                "line": int(match.group(2)),
                "column": int(match.group(3)),
            }
            key = (filename.lower(), item["line"], item["column"])
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result


def normalize_node_execution(execution):
    """Return renderer-independent facts from one Node execution result."""
    text = _combined_text(execution)
    lower = text.lower()
    locations = _locations(text)
    facts = []
    seen = set()

    def add(kind, **payload):
        key = (kind, tuple(sorted((str(k), str(v)) for k, v in payload.items())))
        if key in seen:
            return
        seen.add(key)
        facts.append({"kind": kind, **payload})

    for pattern in _UNDEFINED_PATTERNS:
        for match in pattern.finditer(text):
            add("undefined_identifier", identifier=str(match.group(1)))

    if "cannot determine intended module format" in lower or "both require() and top-level await" in lower:
        add("module_format_conflict")

    if "cancelledbyparent" in lower or "test did not finish before its parent and was cancelled" in lower:
        add("cancelled_child_tests")

    # Node assert.rejects calls waitForActual().  If the callback itself throws
    # synchronously, the stack contains both frames and the thrown application
    # error escapes the async assertion mechanism.  This is a verifier-harness
    # fact, not evidence that the implementation must become async.
    if "waitforactual" in lower and "function.rejects" in lower:
        add("sync_callback_used_with_assert_rejects")

    if any(
        token in lower
        for token in (
            "referenceerror: describe is not defined",
            "error: 'describe is not defined'",
            'error: "describe is not defined"',
        )
    ):
        add("missing_node_test_binding", identifier="describe")
    if any(
        token in lower
        for token in (
            "referenceerror: it is not defined",
            "error: 'it is not defined'",
            'error: "it is not defined"',
        )
    ):
        add("missing_node_test_binding", identifier="it")
    if any(
        token in lower
        for token in (
            "referenceerror: test is not defined",
            "error: 'test is not defined'",
            'error: "test is not defined"',
        )
    ):
        add("missing_node_test_binding", identifier="test")

    return {
        "facts": facts,
        "locations": locations,
        "text": text,
        "status": str((execution or {}).get("status") or ""),
        "exit_code": (execution or {}).get("exit_code"),
    }


def has_fact(normalized, kind, **matches):
    for fact in (normalized or {}).get("facts") or []:
        if str(fact.get("kind") or "") != str(kind):
            continue
        if all(str(fact.get(key) or "").lower() == str(value or "").lower() for key, value in matches.items()):
            return True
    return False


def fact_values(normalized, kind, field):
    result = []
    for fact in (normalized or {}).get("facts") or []:
        if str(fact.get("kind") or "") != str(kind):
            continue
        value = fact.get(field)
        if value is not None and value not in result:
            result.append(value)
    return result


def implicated_lines(normalized, filename):
    target = os.path.basename(str(filename or "")).lower()
    return sorted({
        int(item.get("line") or 0)
        for item in (normalized or {}).get("locations") or []
        if os.path.basename(str(item.get("filename") or "")).lower() == target
        and int(item.get("line") or 0) > 0
    })
