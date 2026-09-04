"""ATLAS v3 project specification.

The project spec is an execution contract, not a final acceptance gate.  It
captures the user's explicit deliverables and verification sequence so BUILD,
VERIFY and REPAIR can share one stable source of truth without stuffing the
entire run history into every model call.
"""

import hashlib
import json
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

# Version constraints belong to the user contract, not to the model.  These
# intentionally cover the common explicit forms without attempting to parse
# arbitrary prose as semver.  If the user did not state a version, the v3
# environment layer is free to validate/recover a model-suggested compatible
# version against the real npm registry.
_DEP_INLINE_PIN_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)@([0-9][A-Za-z0-9*.+<>=~^|_-]{0,79})(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_DEP_VERSION_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)\s+(?:version|v)\s*([0-9][A-Za-z0-9*.+<>=~^|_-]{0,79})",
    re.IGNORECASE,
)
_INTENTIONAL_DEFECT_TARGET_RE = re.compile(
    r"\b(?:implementation\s+)?(?:defect|bug|failure)\s+in\s+[`'\"]?([A-Za-z0-9_.-]+\.(?:js|mjs|cjs|jsx|ts|tsx))[`'\"]?",
    re.IGNORECASE,
)

_FORBID_EXTERNAL_DEPENDENCIES_RES = (
    re.compile(
        r"\b(?:do\s+not|don\'t)\s+(?:use|add|install|include|introduce)\s+"
        r"(?:any\s+)?(?:external|third[- ]party)(?:\s+npm)?\s+dependenc(?:y|ies)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:without|no)\s+(?:any\s+)?(?:external|third[- ]party)(?:\s+npm)?\s+dependenc(?:y|ies)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:do\s+not|don\'t)\s+(?:use|add|install|include|introduce)\s+(?:any\s+)?npm\s+dependenc(?:y|ies)\b",
        re.IGNORECASE,
    ),
)

_HARD_CONTRACT_FIELDS = (
    "required_files",
    "required_dependencies",
    "dependency_constraints",
    "required_scripts",
    "min_tests",
    "requires_verification",
    "requires_fail_then_repair",
    "intentional_defect_target",
    "forbid_external_dependencies",
)

def _forbid_external_dependencies_from_goal(goal):
    text = str(goal or "")
    return any(pattern.search(text) for pattern in _FORBID_EXTERNAL_DEPENDENCIES_RES)

def _hard_contract_payload(spec):
    payload = {}
    for key in _HARD_CONTRACT_FIELDS:
        value = (spec or {}).get(key)
        if isinstance(value, dict):
            payload[key] = dict(sorted((str(k), str(v)) for k, v in value.items()))
        elif isinstance(value, list):
            payload[key] = [str(item) for item in value]
        else:
            payload[key] = value
    return payload

def _contract_fingerprint(spec):
    raw = json.dumps(_hard_contract_payload(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

def _apply_contract_provenance(spec):
    result = dict(spec or {})
    result["contract_schema"] = 4
    result["hard_contract_source"] = "original_goal"
    result["revision_inputs_are_execution_guidance"] = True
    result["contract_provenance"] = {field: "original_goal" for field in _HARD_CONTRACT_FIELDS}
    result["contract_fingerprint"] = _contract_fingerprint(result)
    return result


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


def _explicit_dependency_constraints(goal, dependencies):
    """Return only version constraints explicitly stated by the user.

    Model-produced package.json versions are implementation suggestions, not
    contract facts.  Keeping provenance here lets the environment resolver
    recover a hallucinated registry version without overriding a real user pin.
    """
    wanted = {str(name).lower(): str(name) for name in dependencies or []}
    result = {}
    for pattern in (_DEP_INLINE_PIN_RE, _DEP_VERSION_WORD_RE):
        for match in pattern.finditer(str(goal or "")):
            name = str(match.group(1) or "").strip()
            version = str(match.group(2) or "").strip()
            canonical = wanted.get(name.lower())
            if canonical and version:
                result[canonical] = version
    return result


def deterministic_spec(run, runtime):
    goal = str(run.get("goal") or "")
    required_files = _dedupe(
        match.group(1).rstrip(".,;:")
        for match in _FILE_RE.finditer(goal)
        if match.group(1).lower().rstrip(".,;:") not in {"node.js", "javascript.js"}
    )
    # Only explicit package-use syntax or package@version syntax may create a
    # required dependency. Generic "Name v3" text (for example "ATLAS v3")
    # is product/control-plane language, not npm provenance.
    dependencies = _dedupe(
        [match.group(1) for match in _NPM_DEP_RE.finditer(goal)]
        + [match.group(1) for match in _DEP_INLINE_PIN_RE.finditer(goal)]
    )
    dependency_constraints = _explicit_dependency_constraints(goal, dependencies)
    counts = [int(match.group(1)) for match in _TEST_COUNT_RE.finditer(goal)]

    lower = goal.lower()
    scripts = ["test"] if re.search(r"\btest(?:\s+npm)?\s+script\b|\bnpm\s+test\b", lower) else []
    fail_then_repair = bool(
        re.search(r"\b(?:deliberately|intentionally)\b.{0,120}\b(?:defect|bug|failure)\b", goal, re.I | re.S)
        and re.search(r"\b(?:repair|fix)\b", goal, re.I)
    )
    defect_target_match = _INTENTIONAL_DEFECT_TARGET_RE.search(goal) if fail_then_repair else None
    intentional_defect_target = (
        defect_target_match.group(1).rstrip(".,;:")
        if defect_target_match
        else None
    )
    forbid_external_dependencies = _forbid_external_dependencies_from_goal(goal)

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

    return _apply_contract_provenance({
        "version": 4,
        "runtime": runtime,
        "summary": goal.splitlines()[0][:500] if goal.strip() else "Agent coding project",
        "required_files": required_files,
        "required_dependencies": dependencies,
        "dependency_constraints": dependency_constraints,
        "required_scripts": scripts,
        "min_tests": max(counts) if counts else 0,
        "behaviors": behaviors,
        "constraints": [],
        "requires_verification": bool(re.search(r"\b(?:run|rerun|re-run).{0,80}\btests?\b", lower, re.S) or "finish only" in lower),
        "requires_fail_then_repair": fail_then_repair,
        "intentional_defect_target": intentional_defect_target,
        "forbid_external_dependencies": forbid_external_dependencies,
        "source": "deterministic_fallback",
    })


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

    # Hard acceptance requirements are user-owned. The model may help with
    # semantic interpretation, but it may not add files, dependencies, scripts,
    # test counts, verification policy, or dependency policy to the contract.
    required_files = _dedupe(fallback.get("required_files", []))
    dependencies = _dedupe(fallback.get("required_dependencies", []))
    scripts = _dedupe(fallback.get("required_scripts", []))
    min_tests = int(fallback.get("min_tests") or 0)

    return _apply_contract_provenance({
        "version": 4,
        "runtime": str(data.get("runtime") or fallback.get("runtime") or "node").lower(),
        "summary": str(data.get("summary") or fallback.get("summary") or "")[:1000],
        "required_files": required_files,
        "required_dependencies": dependencies,
        # Dependency version provenance is deterministic/user-owned. Never let
        # the semantic spec model invent a pin that the user did not request.
        "dependency_constraints": dict(fallback.get("dependency_constraints") or {}),
        "required_scripts": scripts,
        "min_tests": max(0, min(100, min_tests)),
        "behaviors": behaviors or fallback.get("behaviors") or [],
        # Model constraints are advisory implementation hints, not acceptance
        # requirements. Persistent hard constraints come only from the goal.
        "constraints": list(fallback.get("constraints") or []),
        "model_constraints_advisory": strings("constraints", 30),
        "requires_verification": bool(fallback.get("requires_verification", True)),
        "requires_fail_then_repair": bool(fallback.get("requires_fail_then_repair", False)),
        # The exact controlled-defect file is explicit user provenance only.
        "intentional_defect_target": fallback.get("intentional_defect_target"),
        # External-dependency policy is explicit user provenance only. The
        # semantic model must not invent this restriction (for example while
        # simultaneously requiring an npm package such as validator).
        "forbid_external_dependencies": bool(fallback.get("forbid_external_dependencies", False)),
        "source": "model_with_user_contract_merge",
    })


def upgrade_project_spec(run, spec, runtime):
    """Deterministically upgrade an already-persisted older v3 spec.

    Resume must gain new contract fields without asking the model to reinterpret
    history. Explicit files/dependencies/behaviors from the stored spec are
    preserved while new user-provenance fields come from the original goal.
    """
    fallback = deterministic_spec(run, runtime)
    upgraded = _validate_spec(dict(spec or {}), fallback)
    old_hard = _hard_contract_payload(spec or {})
    new_hard = _hard_contract_payload(upgraded)
    if int((spec or {}).get("version") or 0) < 4 or old_hard != new_hard:
        upgraded["source"] = "original_goal_contract_reconciled_v4"
        upgraded["contract_reconciliation"] = {
            "removed_required_dependencies": sorted(
                set(str(x) for x in (spec or {}).get("required_dependencies") or [])
                - set(str(x) for x in upgraded.get("required_dependencies") or [])
            ),
            "forbid_external_dependencies_before": bool((spec or {}).get("forbid_external_dependencies", False)),
            "forbid_external_dependencies_after": bool(upgraded.get("forbid_external_dependencies", False)),
        }
    else:
        upgraded["source"] = str((spec or {}).get("source") or upgraded.get("source") or "persistent")
    if (spec or {}).get("model"):
        upgraded["model"] = (spec or {}).get("model")
    return upgraded


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
        "ORIGINAL GOAL (the only source allowed to define persistent hard acceptance requirements):\n"
        + str(run.get("goal") or "")
        + "\n\nDETERMINISTIC USER-OWNED REQUIREMENTS ALREADY EXTRACTED:\n"
        + str(fallback)
        + "\n\nRevision/Continue instructions are execution guidance and are intentionally excluded from this persistent contract."
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
        "User-pinned dependency versions: " + (
            ", ".join(f"{name}@{version}" for name, version in (spec.get("dependency_constraints") or {}).items())
            or "none"
        ),
        "Required scripts: " + (", ".join(spec.get("required_scripts") or []) or "none explicitly required"),
        f"Minimum tests: {int(spec.get('min_tests') or 0)}",
        f"Behavior criteria: {len(spec.get('behaviors') or [])}",
        f"Project-owned behavior criteria: {sum(1 for item in (spec.get('behaviors') or []) if str(item.get('acceptance_kind') or classify_criterion(item)) == KIND_USER)}",
        "Required fail→repair demonstration: " + ("yes" if spec.get("requires_fail_then_repair") else "no"),
        "Controlled defect target: " + str(spec.get("intentional_defect_target") or "not explicitly constrained"),
        "External dependencies forbidden beyond explicitly required packages: " + ("yes" if spec.get("forbid_external_dependencies") else "no"),
        "Hard contract source: " + str(spec.get("hard_contract_source") or "unknown"),
        "Contract fingerprint: " + str(spec.get("contract_fingerprint") or "unknown"),
        "Revision inputs are execution guidance: " + ("yes" if spec.get("revision_inputs_are_execution_guidance") else "no"),
        "Spec source: " + str(spec.get("source") or "unknown"),
    ]
    return "\n".join(lines)
