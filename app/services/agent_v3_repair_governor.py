"""ATLAS v3.2 evidence-driven repair governor.

This module owns repair-budget policy independently from language-specific
mutation code.  A repair budget is based on *committed engineering changes and
measured progress*, not on model attempts or rejected candidates.

Design rules:
- invalid/rejected repair candidates do not consume committed-repair budget
- changed failure evidence counts as progress, even when verification still fails
- a verified execution always wins immediately
- repeated unchanged failures trigger a stall stop
- a small base repair tranche may extend when each committed repair is still
  producing measurable progress, up to an absolute safety ceiling
"""

import hashlib
import re

BASE_REPAIR_TRANCHE = 3
MAX_PROGRESS_EXTENSIONS = 3
ABSOLUTE_MAX_COMMITTED_REPAIRS = BASE_REPAIR_TRANCHE + MAX_PROGRESS_EXTENSIONS
STALL_LIMIT = 2

_SUMMARY_FIELDS = {
    "tests": re.compile(r"^\s*#\s*tests\s+(\d+)\s*$", re.I | re.M),
    "pass": re.compile(r"^\s*#\s*pass\s+(\d+)\s*$", re.I | re.M),
    "fail": re.compile(r"^\s*#\s*fail\s+(\d+)\s*$", re.I | re.M),
    "cancelled": re.compile(r"^\s*#\s*cancelled\s+(\d+)\s*$", re.I | re.M),
    "skipped": re.compile(r"^\s*#\s*skipped\s+(\d+)\s*$", re.I | re.M),
    "todo": re.compile(r"^\s*#\s*todo\s+(\d+)\s*$", re.I | re.M),
}


def _execution_text(execution):
    return "\n".join([
        str((execution or {}).get("stdout") or ""),
        str((execution or {}).get("stderr") or ""),
    ])


def _normalize_failure_text(text):
    value = str(text or "")
    value = value.replace("/runtime/", "").replace("/workspace/", "")
    value = re.sub(r"\bduration_ms:\s*[0-9.]+", "duration_ms:#", value, flags=re.I)
    value = re.sub(r":\d+:\d+\b", ":#:#", value)
    value = re.sub(r"\b\d+(?:\.\d+)?\s*ms\b", "#ms", value, flags=re.I)
    return value


def execution_evidence(execution):
    """Extract compact comparable evidence from one sandbox execution."""
    execution = dict(execution or {})
    text = _execution_text(execution)
    normalized = _normalize_failure_text(text)
    lowered = normalized.lower()

    summary = {}
    for key, pattern in _SUMMARY_FIELDS.items():
        match = pattern.search(normalized)
        summary[key] = int(match.group(1)) if match else 0

    # Older/partial TAP output can omit final summary lines.  Keep useful
    # lower-bound counts so progress is still measurable.
    ok_lines = len(re.findall(r"(?m)^\s*ok\s+\d+\s+-", normalized))
    not_ok_lines = len(re.findall(r"(?m)^\s*not ok\s+\d+\s+-", normalized))
    if not summary["pass"]:
        summary["pass"] = ok_lines
    if not summary["fail"]:
        summary["fail"] = not_ok_lines
    if not summary["tests"]:
        summary["tests"] = max(summary["pass"] + summary["fail"] + summary["cancelled"], ok_lines + not_ok_lines)

    status = str(execution.get("status") or "unknown").lower()
    exit_code = execution.get("exit_code")
    verified = status == "success" and int(exit_code or 0) == 0
    has_tap = "tap version" in lowered or summary["tests"] > 0 or ok_lines > 0 or not_ok_lines > 0

    error_markers = []
    for marker in (
        "syntaxerror",
        "referenceerror",
        "typeerror",
        "assertionerror",
        "cancelledbyparent",
        "subtestsfailed",
        "err_test_failure",
        "module_not_found",
        "cannot find module",
        "err_ambiguous_module_syntax",
        "cannot determine intended module format",
    ):
        if marker in lowered:
            error_markers.append(marker)

    meaningful = []
    for line in normalized.splitlines():
        low = line.lower()
        if any(
            token in low
            for token in (
                "not ok",
                "failuretype",
                "error:",
                "expected",
                "actual",
                "assert",
                "typeerror",
                "referenceerror",
                "syntaxerror",
                "cancelledbyparent",
                "subtestsfailed",
                "cannot determine intended module format",
                "err_ambiguous_module_syntax",
            )
        ):
            meaningful.append(line.strip())
    payload = "\n".join(meaningful[-40:]) or normalized[-4500:]
    fingerprint_raw = "|".join([
        str(execution.get("execution_action") or ""),
        str(execution.get("command") or ""),
        payload,
    ])
    fingerprint = hashlib.sha1(
        fingerprint_raw.encode("utf-8", errors="ignore")
    ).hexdigest()[:18]

    # Score is intentionally ordinal, not a fake probability.  It is used only
    # to determine whether the latest committed repair moved execution forward.
    score = 0
    if has_tap:
        score += 100
    if summary["tests"]:
        score += 20
    score += summary["pass"] * 18
    score -= summary["fail"] * 5
    score -= summary["cancelled"] * 2
    if "syntaxerror" in error_markers or "referenceerror" in error_markers:
        score -= 20
    if verified:
        score = 10000

    return {
        "status": status,
        "exit_code": exit_code,
        "verified": verified,
        "has_tap": has_tap,
        "tests": summary["tests"],
        "passed": summary["pass"],
        "failed": summary["fail"],
        "cancelled": summary["cancelled"],
        "skipped": summary["skipped"],
        "todo": summary["todo"],
        "error_markers": error_markers,
        "fingerprint": fingerprint,
        "score": score,
    }


def compare_evidence(before_execution, after_execution):
    """Classify progress after one committed repair."""
    before = execution_evidence(before_execution)
    after = execution_evidence(after_execution)

    if after["verified"]:
        classification = "verified"
        reason = "Sandbox verification passed."
    elif before["fingerprint"] == after["fingerprint"]:
        classification = "stalled"
        reason = "The authoritative failure fingerprint did not change."
    elif (
        before["has_tap"]
        and not after["has_tap"]
        and (before["passed"] > 0 or before["tests"] > 0)
    ):
        classification = "regression"
        reason = "The repair regressed from executable test evidence to a loader/parser/runtime-start failure."
    elif after["score"] + 30 < before["score"] and after["passed"] < before["passed"]:
        classification = "regression"
        reason = "The latest committed repair measurably reduced verification progress."
    elif (
        after["has_tap"]
        and (
            after["passed"] > before["passed"]
            or (
                before["tests"] == 0
                and after["tests"] > 0
            )
            or (
                before["cancelled"] > 0
                and after["cancelled"] < before["cancelled"]
            )
            or after["score"] >= before["score"] + 25
        )
    ):
        classification = "strong_progress"
        reason = "The latest verification reached materially more working behavior."
    else:
        classification = "changed_failure"
        reason = "The failure changed, so the previous hypothesis is no longer authoritative."

    return {
        "classification": classification,
        "reason": reason,
        "before": before,
        "after": after,
        "score_delta": int(after["score"] - before["score"]),
    }


def recent_stall_count(outcomes):
    count = 0
    for item in reversed(list(outcomes or [])):
        classification = str(item.get("progress_class") or item.get("classification") or "")
        if classification == "stalled":
            count += 1
            continue
        break
    return count


def repair_permission(committed_repairs, outcomes=None):
    """Return whether another committed repair may be attempted.

    The base tranche is unconditional.  Beyond that, ATLAS grants extensions
    only while the most recent committed repair demonstrated progress.
    """
    committed = max(0, int(committed_repairs or 0))
    outcomes = list(outcomes or [])
    stalls = recent_stall_count(outcomes)

    if stalls >= STALL_LIMIT:
        return {
            "allowed": False,
            "reason": f"Repair stopped after {stalls} consecutive unchanged failures.",
            "lane": "stalled",
        }

    if committed < BASE_REPAIR_TRANCHE:
        return {
            "allowed": True,
            "reason": f"Base repair tranche has {BASE_REPAIR_TRANCHE - committed} committed slot(s) remaining.",
            "lane": "base",
        }

    if committed >= ABSOLUTE_MAX_COMMITTED_REPAIRS:
        return {
            "allowed": False,
            "reason": (
                "Absolute committed-repair safety ceiling reached "
                f"({ABSOLUTE_MAX_COMMITTED_REPAIRS})."
            ),
            "lane": "hard_cap",
        }

    latest = outcomes[-1] if outcomes else {}
    latest_class = str(latest.get("progress_class") or latest.get("classification") or "")
    if latest_class in {"strong_progress", "changed_failure"}:
        return {
            "allowed": True,
            "reason": "Latest committed repair made measurable progress, so ATLAS granted a bounded progress extension.",
            "lane": "progress_extension",
        }

    if latest_class == "stalled" and stalls < STALL_LIMIT:
        return {
            "allowed": True,
            "reason": "One unchanged failure is allowed a single bounded alternative-hypothesis probe before the stall breaker trips.",
            "lane": "stall_probe",
        }

    return {
        "allowed": False,
        "reason": "Base repair tranche ended without evidence supporting another extension.",
        "lane": "tranche_complete",
    }


def progress_summary(progress):
    progress = dict(progress or {})
    before = progress.get("before") or {}
    after = progress.get("after") or {}
    return (
        f"Progress: {progress.get('classification') or 'unknown'} "
        f"(score {before.get('score', 0)} → {after.get('score', 0)}; "
        f"pass {before.get('passed', 0)} → {after.get('passed', 0)}; "
        f"fail {before.get('failed', 0)} → {after.get('failed', 0)}; "
        f"cancelled {before.get('cancelled', 0)} → {after.get('cancelled', 0)})."
    )
