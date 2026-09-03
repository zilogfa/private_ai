"""
ATLAS v2.4.1 - evidence-driven Node/JavaScript transaction recovery.

This module is deliberately model-independent.  It turns raw Node/TAP sandbox
output into a compact failure state, scores whether a project transaction made
measurable progress, and performs a very small set of high-confidence verifier
repairs when the execution evidence proves the *test harness* is mechanically
wrong rather than the requested behavior.

The goal is not to replace language models with regexes.  The goal is to stop
asking a model to rediscover deterministic facts that ATLAS can already prove.
"""

import hashlib
import re


_TAP_COUNT_RE = re.compile(
    r"(?m)^\s*#\s*(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$",
    re.IGNORECASE,
)
_NOT_OK_RE = re.compile(
    r"(?m)^\s*not ok\s+\d+\s+-\s*(.+?)\s*$",
)
_OK_RE = re.compile(
    r"(?m)^\s*ok\s+\d+\s+-\s*(.+?)\s*$",
)
_LOCATION_RE = re.compile(
    r"location:\s*['\"](?:/runtime/)?([^'\"]+)['\"]",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(
    r"(?m)^\s*error:\s*(?:['\"])(.*?)(?:['\"])\s*$",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"(?m)^\s*name:\s*(?:['\"])(.*?)(?:['\"])\s*$",
    re.IGNORECASE,
)
_STACK_RE = re.compile(
    r"(?m)^\s*at\s+(.+?)\s+\((?:file://)?/runtime/([^():\s]+):(\d+):(\d+)\)\s*$"
)


def _combined_output(execution):
    if not execution:
        return ""
    return "\n".join(
        text
        for text in (
            str(execution.get("stdout") or ""),
            str(execution.get("stderr") or ""),
        )
        if text.strip()
    )


def _normalize_text(value):
    text = str(value or "")
    text = text.replace("/runtime/", "")
    text = re.sub(r"\bduration_ms:\s*[0-9.]+", "duration_ms:#", text, flags=re.I)
    text = re.sub(r":\d+:\d+\b", ":#:#", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:3000]


def extract_execution_evidence(execution):
    """Return a compact, stable view of one Node sandbox execution."""
    if not execution:
        return {
            "status": None,
            "exit_code": None,
            "execution_action": None,
            "verification_label": None,
            "test_count": None,
            "pass_count": None,
            "fail_count": None,
            "failing_tests": [],
            "passing_tests": [],
            "error_name": None,
            "error_message": None,
            "location": None,
            "stack_frames": [],
            "fingerprint": None,
        }

    combined = _combined_output(execution)
    counts = {}
    for match in _TAP_COUNT_RE.finditer(combined):
        counts[match.group(1).lower()] = int(match.group(2))

    failing = [m.group(1).strip() for m in _NOT_OK_RE.finditer(combined)][:20]
    passing = [m.group(1).strip() for m in _OK_RE.finditer(combined)][:20]

    location_match = _LOCATION_RE.search(combined)
    location = None
    if location_match:
        location = location_match.group(1).replace("/runtime/", "")

    error_match = _ERROR_RE.search(combined)
    name_match = _NAME_RE.search(combined)
    error_message = error_match.group(1).strip() if error_match else None
    error_name = name_match.group(1).strip() if name_match else None

    frames = []
    for match in _STACK_RE.finditer(combined):
        frames.append(
            {
                "symbol": match.group(1).strip(),
                "file": match.group(2),
                "line": int(match.group(3)),
                "column": int(match.group(4)),
            }
        )
        if len(frames) >= 8:
            break

    action = str(execution.get("execution_action") or "")
    if action == "run_npm":
        command = str(execution.get("command") or "test").strip() or "test"
        label = "npm run " + command
    elif action == "run_node":
        label = str(execution.get("filename") or "node verification")
    else:
        label = str(execution.get("filename") or execution.get("command") or "Node verification")

    raw = "|".join(
        [
            action,
            label,
            str(counts.get("tests")),
            str(counts.get("pass")),
            str(counts.get("fail")),
            "|".join(failing),
            str(error_name or ""),
            _normalize_text(error_message),
            re.sub(r":\d+:\d+$", ":#:#", str(location or "")),
        ]
    )
    fingerprint = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]

    return {
        "status": str(execution.get("status") or ""),
        "exit_code": execution.get("exit_code"),
        "execution_action": action,
        "verification_label": label,
        "test_count": counts.get("tests"),
        "pass_count": counts.get("pass"),
        "fail_count": counts.get("fail"),
        "failing_tests": failing,
        "passing_tests": passing,
        "error_name": error_name,
        "error_message": error_message,
        "location": location,
        "stack_frames": frames,
        "fingerprint": fingerprint,
    }


def progress_between(before, after, acceptance_before=None, acceptance_after=None):
    """Classify whether a transaction improved the externally observable state."""
    before = dict(before or {})
    after = dict(after or {})
    acceptance_before = dict(acceptance_before or {})
    acceptance_after = dict(acceptance_after or {})

    if (
        str(after.get("status") or "") == "success"
        and int(after.get("exit_code") or 0) == 0
    ):
        return {
            "classification": "verified_execution",
            "score": 100,
            "reason": "Sandbox verification passed.",
        }

    score = 0
    reasons = []
    before_fail = before.get("fail_count")
    after_fail = after.get("fail_count")
    before_pass = before.get("pass_count")
    after_pass = after.get("pass_count")

    if isinstance(before_fail, int) and isinstance(after_fail, int):
        delta = before_fail - after_fail
        if delta > 0:
            score += min(60, delta * 15)
            reasons.append(f"failing tests decreased {before_fail}->{after_fail}")
        elif delta < 0:
            score -= min(60, (-delta) * 15)
            reasons.append(f"failing tests increased {before_fail}->{after_fail}")

    if isinstance(before_pass, int) and isinstance(after_pass, int):
        delta = after_pass - before_pass
        if delta > 0:
            score += min(40, delta * 10)
            reasons.append(f"passing tests increased {before_pass}->{after_pass}")
        elif delta < 0:
            score -= min(40, (-delta) * 10)
            reasons.append(f"passing tests decreased {before_pass}->{after_pass}")

    before_open = len(acceptance_before.get("issues") or [])
    after_open = len(acceptance_after.get("issues") or [])
    if after_open < before_open:
        score += min(30, (before_open - after_open) * 10)
        reasons.append(f"acceptance blockers decreased {before_open}->{after_open}")
    elif after_open > before_open:
        score -= min(30, (after_open - before_open) * 10)
        reasons.append(f"acceptance blockers increased {before_open}->{after_open}")

    if before.get("fingerprint") and after.get("fingerprint"):
        if before["fingerprint"] != after["fingerprint"]:
            score += 5
            reasons.append("failure evidence changed")
        elif score <= 0:
            reasons.append("same failure fingerprint remains")

    score = max(-100, min(100, score))

    if score >= 25:
        classification = "strong_progress"
    elif score > 0:
        classification = "changed_failure"
    elif score < 0:
        classification = "regression"
    else:
        classification = "stalled"

    return {
        "classification": classification,
        "score": score,
        "reason": "; ".join(reasons) if reasons else "No measurable progress signal.",
    }


def evidence_summary(evidence):
    evidence = dict(evidence or {})
    parts = []
    if evidence.get("verification_label"):
        parts.append("verification=" + str(evidence["verification_label"]))
    if evidence.get("test_count") is not None:
        parts.append(
            "tests="
            + str(evidence.get("test_count"))
            + ", pass="
            + str(evidence.get("pass_count"))
            + ", fail="
            + str(evidence.get("fail_count"))
        )
    if evidence.get("failing_tests"):
        parts.append("failing=" + " | ".join(evidence["failing_tests"][:5]))
    if evidence.get("error_name") or evidence.get("error_message"):
        parts.append(
            "error="
            + ": ".join(
                x
                for x in (
                    str(evidence.get("error_name") or "").strip(),
                    str(evidence.get("error_message") or "").strip(),
                )
                if x
            )
        )
    if evidence.get("location"):
        parts.append("location=" + str(evidence["location"]))
    return "; ".join(parts)[:2500]


def _normalize_expression(value):
    text = str(value or "").strip()
    text = re.sub(r"\bawait\s+", "", text)
    text = text.rstrip(";").strip()
    text = re.sub(r"\s+", "", text)
    return text


def find_uncaptured_expected_throw(source, evidence=None):
    """
    Detect a mechanical test-harness defect such as:

        await manager.complete(1);
        assert.throws(() => manager.complete(1), {message: 'Invalid task index'});

    The first call throws before assert.throws can observe it. Removing only the
    unguarded pre-call preserves the exact expected behavior and test coverage.
    """
    text = str(source or "")
    lines = text.splitlines(keepends=True)
    error_message = str((evidence or {}).get("error_message") or "").strip()

    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)await\s+(.+?)\s*;\s*$", line.rstrip("\r\n"))
        if not match:
            continue
        expression = _normalize_expression(match.group(2))
        if not expression:
            continue

        window_text = "".join(lines[index + 1 : min(len(lines), index + 8)])
        throws_match = re.search(
            r"assert\.throws\s*\(\s*\(\s*\)\s*=>\s*([^,\n]+?)\s*,",
            window_text,
            re.S,
        )
        if not throws_match:
            continue
        expected_expression = _normalize_expression(throws_match.group(1))
        if expected_expression != expression:
            continue

        message_match = re.search(
            r"message\s*:\s*['\"]([^'\"]+)['\"]",
            window_text,
            re.S,
        )
        expected_message = message_match.group(1).strip() if message_match else ""
        if error_message and expected_message and error_message != expected_message:
            continue

        repaired = "".join(lines[:index] + lines[index + 1 :])
        return {
            "line": index + 1,
            "expression": match.group(2).strip(),
            "expected_message": expected_message,
            "content": repaired,
            "reason": (
                "Remove the unguarded pre-call that throws before assert.throws can capture "
                "the same expected error. This repairs test mechanics without weakening the specification."
            ),
        }
    return None
