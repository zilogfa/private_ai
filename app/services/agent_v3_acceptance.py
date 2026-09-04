"""ATLAS v3 layered acceptance semantics.

Acceptance is intentionally split into three evidence domains:

1. user_deliverable: behavior the generated project itself must implement;
2. execution: facts proven by authoritative build/test execution;
3. platform: guarantees supplied by ATLAS (sandbox, network policy, mounts, etc.).

A model may help judge user-deliverable semantics, but it cannot invent new
acceptance criteria and it cannot make project code responsible for ATLAS
platform guarantees.
"""

import re

KIND_USER = "user_deliverable"
KIND_EXECUTION = "execution"
KIND_PLATFORM = "platform"

_PLATFORM_STRONG_RE = re.compile(
    r"\b(?:sandbox[_ -]?isolation|isolated\s+sandbox|network\s+(?:disabled|off)|"
    r"read[- ]?only\s+(?:source|workspace|mount)|disposable\s+runtime|ephemeral\s+runtime|"
    r"no[- ]?new[- ]?privileges|non[- ]?root|docker\s+(?:container|sandbox)|security\s+boundary)\b",
    re.IGNORECASE,
)
_EXECUTION_RE = re.compile(
    r"\b(?:tests?\s+(?:pass|passed|passing)|verification|verified|verify|exit\s+code|"
    r"run\s+(?:the\s+)?(?:real\s+)?(?:sandbox\s+)?tests?|build\s+(?:pass|passed|succeed)|"
    r"sandbox\s+(?:test|verification|execution))\b",
    re.IGNORECASE,
)
_PLATFORM_GENERIC_RE = re.compile(
    r"\b(?:sandbox|container|network|read[- ]?only|ephemeral|disposable|isolation|privileges?)\b",
    re.IGNORECASE,
)


def _criterion_text(item):
    if not isinstance(item, dict):
        return str(item or "")
    return " ".join(
        str(item.get(key) or "")
        for key in ("id", "description")
    ).strip()


def classify_criterion(item):
    """Classify one acceptance criterion without consulting a model.

    Strong platform terms win first. Execution language wins before generic
    sandbox language so "run the sandbox tests" remains execution evidence,
    while "sandbox isolation" remains a platform guarantee.
    """
    text = _criterion_text(item)
    low = text.lower().replace("_", " ")

    if _PLATFORM_STRONG_RE.search(low):
        return KIND_PLATFORM
    if _EXECUTION_RE.search(low):
        return KIND_EXECUTION
    if _PLATFORM_GENERIC_RE.search(low) and any(
        token in low
        for token in (
            "isolation",
            "network",
            "read only",
            "ephemeral",
            "disposable",
            "privilege",
            "container",
        )
    ):
        return KIND_PLATFORM
    return KIND_USER


def criteria_from_spec(spec):
    """Return normalized acceptance criteria from behaviors + constraints.

    Older v3 specs stored constraints separately as strings. Treat them as
    first-class criteria at acceptance time without requiring a schema migration.
    """
    items = []
    for raw in (spec or {}).get("behaviors") or []:
        if isinstance(raw, dict):
            items.append(dict(raw))
    for index, raw in enumerate((spec or {}).get("constraints") or []):
        text = str(raw or "").strip()
        if not text:
            continue
        items.append({
            "id": f"constraint_{index + 1}",
            "description": text[:600],
            "evidence_keywords": [],
        })
    return items


def partition_criteria(behaviors):
    result = {
        KIND_USER: [],
        KIND_EXECUTION: [],
        KIND_PLATFORM: [],
    }
    for raw in behaviors or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        kind = classify_criterion(item)
        item["acceptance_kind"] = kind
        result[kind].append(item)
    return result


def platform_evidence(run, execution=None):
    """Return deterministic ATLAS-owned runtime/security evidence."""
    try:
        from app.services.agent_sandbox import sandbox_runtime_profile
        profile = sandbox_runtime_profile(run.get("user_id"), run.get("id"))
    except Exception:
        profile = {}

    execution = dict(execution or {})
    execution_observed = bool(execution)
    network_disabled = profile.get("execution_network") is False
    source_read_only = profile.get("source_read_only") is True
    runtime_ephemeral = profile.get("runtime_ephemeral") is True
    runtime_writable = profile.get("runtime_writable") is True
    non_root = profile.get("runs_as_root") is False

    # Sandbox execution records are only produced by the Docker-backed sandbox
    # service. Requiring an observed execution prevents configuration metadata
    # alone from being misrepresented as proof that this run used the boundary.
    docker_container = bool(execution_observed and execution.get("image"))

    sandbox_isolation = bool(
        execution_observed
        and docker_container
        and network_disabled
        and source_read_only
        and runtime_ephemeral
        and runtime_writable
        and non_root
    )

    return {
        "execution_observed": execution_observed,
        "sandbox_isolation": sandbox_isolation,
        "docker_container": docker_container,
        "network_disabled": network_disabled,
        "source_read_only": source_read_only,
        "runtime_ephemeral": runtime_ephemeral,
        "runtime_writable": runtime_writable,
        "non_root": non_root,
        "profile": str(profile.get("profile") or ""),
        "runtime": str(profile.get("runtime") or execution.get("runtime") or ""),
        "image": str(execution.get("image") or profile.get("runtime_image") or ""),
    }


def _platform_requirement_key(item):
    text = _criterion_text(item).lower().replace("_", " ")
    if "network" in text:
        return "network_disabled"
    if "read only" in text or "readonly" in text:
        return "source_read_only"
    if "ephemeral" in text or "disposable" in text:
        return "runtime_ephemeral"
    if "non root" in text or "non-root" in text or "privilege" in text:
        return "non_root"
    if "docker" in text or "container" in text:
        return "docker_container"
    return "sandbox_isolation"


def evaluate_platform_criteria(criteria, run, execution=None):
    evidence = platform_evidence(run, execution)
    issues = []
    checks = []
    for item in criteria or []:
        key = _platform_requirement_key(item)
        satisfied = bool(evidence.get(key))
        checks.append({
            "id": str(item.get("id") or "")[:120],
            "description": str(item.get("description") or "")[:600],
            "evidence_key": key,
            "satisfied": satisfied,
        })
        if not satisfied:
            issues.append({
                "type": "platform_guarantee_unmet",
                "item": str(item.get("id") or item.get("description") or key),
                "evidence_key": key,
            })
    return issues, evidence, checks


def evaluate_execution_criteria(criteria, execution=None):
    execution = dict(execution or {})
    passed = (
        str(execution.get("status") or "").lower() == "success"
        and int(execution.get("exit_code") or 0) == 0
    )
    issues = []
    checks = []
    for item in criteria or []:
        # v3 currently enters final acceptance only after authoritative success,
        # so every recognized execution criterion is proven by the same stored
        # execution record. More specialized build/lint evidence can be added
        # here later without changing user-deliverable acceptance.
        satisfied = passed
        checks.append({
            "id": str(item.get("id") or "")[:120],
            "description": str(item.get("description") or "")[:600],
            "satisfied": satisfied,
            "command": str(execution.get("command") or execution.get("filename") or "")[:1000],
        })
        if not satisfied:
            issues.append({
                "type": "execution_requirement_unmet",
                "item": str(item.get("id") or item.get("description") or "verification"),
            })
    return issues, checks


def filter_model_unmet_ids(raw_ids, allowed_ids):
    """Whitelist semantic model output against the persistent contract."""
    allowed = {str(item) for item in (allowed_ids or []) if str(item).strip()}
    known = []
    unknown = []
    values = raw_ids if isinstance(raw_ids, list) else []
    for raw in values[:40]:
        item = str(raw or "").strip()
        if not item:
            continue
        target = known if item in allowed else unknown
        if item not in target:
            target.append(item)
    return known, unknown


def repairable_acceptance_issues(acceptance):
    """Return only issues project source is allowed to repair."""
    repairable_types = {
        "missing_file",
        "invalid_package_json",
        "missing_dependency",
        "dependency_not_used",
        "forbidden_dependency",
        "missing_script",
        "insufficient_tests",
        "required_failure_not_observed",
        "behavior_unmet",
    }
    issues = []
    for key in ("hard_issues", "semantic_issues"):
        for issue in acceptance.get(key) or []:
            if str(issue.get("type") or "") in repairable_types:
                issues.append(issue)
    return issues


def acceptance_layers_summary(acceptance):
    layers = acceptance.get("layers") or {}
    user_layer = layers.get(KIND_USER) or {}
    execution_layer = layers.get(KIND_EXECUTION) or {}
    platform_layer = layers.get(KIND_PLATFORM) or {}
    return {
        "user_deliverable": bool(user_layer.get("satisfied")),
        "execution": bool(execution_layer.get("satisfied")),
        "platform": bool(platform_layer.get("satisfied")),
    }
