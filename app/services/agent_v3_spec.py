"""ATLAS v3 project specification.

The project spec is an execution contract, not a final acceptance gate.  It
captures the user's explicit deliverables and verification sequence so BUILD,
VERIFY and REPAIR can share one stable source of truth without stuffing the
entire run history into every model call.
"""

import re

from app.services.agents import list_agent_inputs
from app.services.agent_v3_model_gateway import V3ModelError, run_json
from app.services.agent_v3_acceptance import classify_criterion, KIND_USER

_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+\.(?:js|mjs|cjs|jsx|ts|tsx|json|py|html|css|md|txt))(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_TEST_COUNT_RE = re.compile(r"\b(?:at\s+least\s+)?(\d+)\s+tests?\b", re.IGNORECASE)
_NPM_DEP_RE = re.compile(
    r"\b(?:use|using)\s+(?:the\s+)?[`'\"]?(@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)[`'\"]?\s+npm\s+package\b",
    re.IGNORECASE,
)


def _dedupe(values):
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _latest_input_text(run):
    try:
        inputs = list_agent_inputs(run["user_id"], run["id"])
    except Exception:
        return ""
    if not inputs:
        return ""
    return "\n".join("- " + str(item.get("content") or "") for item in inputs[-4:])[:5000]


def deterministic_spec(run, runtime):
    goal = str(run.get("goal") or "")
    required_files = _dedupe(
        match.group(1).rstrip(".,;:")
        for match in _FILE_RE.finditer(goal)
        if match.group(1).lower().rstrip(".,;:") not in {"node.js", "javascript.js"}
    )
    dependencies = _dedupe(match.group(1) for match in _NPM_DEP_RE.finditer(goal))
    counts = [int(match.group(1)) for match in _TEST_COUNT_RE.finditer(goal)]

    lower = goal.lower()
    scripts = ["test"] if re.search(r"\btest(?:\s+npm)?\s+script\b|\bnpm\s+test\b", lower) else []
    fail_then_repair = bool(
        re.search(r"\b(?:deliberately|intentionally)\b.{0,120}\b(?:defect|bug|failure)\b", goal, re.I | re.S)
        and re.search(r"\b(?:repair|fix)\b", goal, re.I)
    )
    forbid_external_dependencies = bool(
        re.search(
            r"\b(?:do\s+not|don't|without|no)\b.{0,80}\b(?:external|third[- ]party)?\s*(?:npm\s+)?dependenc(?:y|ies)\b",
            goal,
            re.I | re.S,
        )
    )

    # Capture common explicit behavior-list blocks without making them a hard
    # build-time gate. Semantic final acceptance can evaluate them later.
    behaviors = []
    lines = [line.strip(" -*\t") for line in goal.splitlines()]
    capture = False
    for line in lines:
        low = line.lower().strip()
        if low.endswith("must support:") or low.endswith("must support") or low.endswith("should support:"):
            capture = True
            continue
        if capture:
            if not line:
                continue
            if re.match(r"^(?:create|add at least|index\.|do not|verification|finish|after the initial|requirements?)\b", low):
                capture = False
                continue
            if len(line) <= 180:
                behavior = {
                    "id": "behavior_" + str(len(behaviors) + 1),
                    "description": line.rstrip("."),
                    "evidence_keywords": [
                        word for word in re.findall(r"[a-z0-9]+", low)
                        if word not in {"a", "an", "the", "by", "and", "or", "to", "of", "for", "must"}
                    ][:6],
                }
                behavior["acceptance_kind"] = classify_criterion(behavior)
                behaviors.append(behavior)

    return {
        "version": 2,
        "runtime": runtime,
        "summary": goal.splitlines()[0][:500] if goal.strip() else "Agent coding project",
        "required_files": required_files,
        "required_dependencies": dependencies,
        "required_scripts": scripts,
        "min_tests": max(counts) if counts else 0,
        "behaviors": behaviors,
        "constraints": [],
        "requires_verification": bool(re.search(r"\b(?:run|rerun|re-run).{0,80}\btests?\b", lower, re.S) or "finish only" in lower),
        "requires_fail_then_repair": fail_then_repair,
        "forbid_external_dependencies": forbid_external_dependencies,
        "source": "deterministic_fallback",
    }


def _validate_spec(data, fallback):
    if not isinstance(data, dict):
        return fallback

    def strings(key, limit=24):
        raw = data.get(key) or []
        if not isinstance(raw, list):
            return []
        return _dedupe(str(item) for item in raw[:limit] if isinstance(item, (str, int, float)))

    behaviors = []
    for index, item in enumerate(data.get("behaviors") or []):
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        keywords = item.get("evidence_keywords") or []
        if not isinstance(keywords, list):
            keywords = []
        behavior = {
            "id": str(item.get("id") or f"behavior_{index + 1}")[:80],
            "description": description[:600],
            "evidence_keywords": _dedupe(str(word).lower() for word in keywords[:8])[:8],
        }
        behavior["acceptance_kind"] = classify_criterion(behavior)
        behaviors.append(behavior)

    required_files = _dedupe(strings("required_files") + fallback.get("required_files", []))
    dependencies = _dedupe(strings("required_dependencies") + fallback.get("required_dependencies", []))
    scripts = _dedupe(strings("required_scripts") + fallback.get("required_scripts", []))

    try:
        min_tests = int(data.get("min_tests") or fallback.get("min_tests") or 0)
    except (TypeError, ValueError):
        min_tests = int(fallback.get("min_tests") or 0)

    return {
        "version": 2,
        "runtime": str(data.get("runtime") or fallback.get("runtime") or "node").lower(),
        "summary": str(data.get("summary") or fallback.get("summary") or "")[:1000],
        "required_files": required_files,
        "required_dependencies": dependencies,
        "required_scripts": scripts,
        "min_tests": max(0, min(100, min_tests)),
        "behaviors": behaviors or fallback.get("behaviors") or [],
        "constraints": strings("constraints", 30),
        "requires_verification": bool(data.get("requires_verification", fallback.get("requires_verification", True))),
        "requires_fail_then_repair": bool(data.get("requires_fail_then_repair", fallback.get("requires_fail_then_repair", False))),
        "forbid_external_dependencies": bool(data.get("forbid_external_dependencies", fallback.get("forbid_external_dependencies", False))),
        "source": "model_with_deterministic_merge",
    }


def build_project_spec(run, runtime):
    fallback = deterministic_spec(run, runtime)
    system = (
        "You convert one software-engineering goal into a compact execution specification for ATLAS. "
        "Do not write code. Preserve explicit deliverables and constraints, but do not invent requirements. "
        "The spec guides BUILD and final ACCEPTANCE; it must not require the initial build to already be perfect. "
        "Return ONLY JSON with keys: summary, runtime, required_files, required_dependencies, required_scripts, "
        "min_tests, behaviors, constraints, requires_verification, requires_fail_then_repair, forbid_external_dependencies. "
        "behaviors is an array of {id, description, evidence_keywords}. Include ONLY behavior owned by the generated project itself. "
        "Do NOT turn test execution, sandbox isolation, Docker/network policy, read-only mounts, permissions, or other ATLAS platform guarantees into project behaviors. "
        "Verification belongs in requires_verification. evidence_keywords should be short words/phrases that could appear in test names or source and are only hints, not exact-string rules."
    )
    user = (
        "ORIGINAL GOAL:\n" + str(run.get("goal") or "")
        + "\n\nLATEST USER REVISION/INPUT (if any):\n"
        + (_latest_input_text(run) or "None")
        + "\n\nDETERMINISTIC EXPLICIT REQUIREMENTS ALREADY EXTRACTED:\n"
        + str(fallback)
    )

    try:
        data, model = run_json(
            run,
            phase="project_spec",
            purpose="v3_project_spec",
            system_prompt=system,
            user_prompt=user,
            tier="worker",
            prompt_budget_chars=18000,
        )
        spec = _validate_spec(data, fallback)
        spec["model"] = model
        return spec
    except V3ModelError:
        return fallback


def spec_summary(spec):
    lines = [
        "Project specification created.",
        f"Runtime: {spec.get('runtime')}",
        "Required files: " + (", ".join(spec.get("required_files") or []) or "none explicitly named"),
        "Dependencies: " + (", ".join(spec.get("required_dependencies") or []) or "none explicitly required"),
        "Required scripts: " + (", ".join(spec.get("required_scripts") or []) or "none explicitly required"),
        f"Minimum tests: {int(spec.get('min_tests') or 0)}",
        f"Behavior criteria: {len(spec.get('behaviors') or [])}",
        f"Project-owned behavior criteria: {sum(1 for item in (spec.get('behaviors') or []) if str(item.get('acceptance_kind') or classify_criterion(item)) == KIND_USER)}",
        "Required fail→repair demonstration: " + ("yes" if spec.get("requires_fail_then_repair") else "no"),
        "External dependencies forbidden: " + ("yes" if spec.get("forbid_external_dependencies") else "no"),
        "Spec source: " + str(spec.get("source") or "unknown"),
    ]
    return "\n".join(lines)
