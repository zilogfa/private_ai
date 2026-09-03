"""
ATLAS v2.3.1a - Persistent Goal / Acceptance Contract.

The sandbox answers "did the latest executable test pass?".  This layer answers
the separate product question "did the Agent actually deliver what the user
asked for?".  A green test suite is necessary, but it is not sufficient when a
requested file, dependency, script, behavior, or amount of test coverage was
silently dropped during debugging.

The first implementation is intentionally deterministic and conservative.  It
extracts explicit, machine-checkable requirements from the original goal and
stores them per run.  Language-specific Project Intelligence can add richer
checks later without changing the acceptance boundary.
"""

import hashlib
import json
import re

from app.database import get_connection
from app.services.agents import utc_iso
from app.services.agent_sandbox import list_workspace_files


_STORAGE_READY = False

_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"([A-Za-z0-9_.-]+\.(?:js|mjs|cjs|jsx|ts|tsx|json|py|html|css|md|txt))"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)

_TEST_COUNT_RE = re.compile(
    r"\b(?:(?:at\s+least|minimum(?:\s+of)?|at\s+minimum)\s+)?(\d+)\s+tests?\b",
    re.IGNORECASE,
)

_TEST_COVERING_RE = re.compile(
    r"\btests?\s+covering\s+(.+?)(?:(?:\.|\n)|$)",
    re.IGNORECASE,
)

_DEPENDENCY_PATTERNS = (
    re.compile(
        r"\busing\s+(?:the\s+)?[`'\"]?(@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)[`'\"]?\s+npm\s+package\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:npm\s+package|dependency)\s+(?:named\s+)?[`'\"](@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)[`'\"]",
        re.IGNORECASE,
    ),
)

_SCRIPT_RE = re.compile(
    r"[`'\"]([A-Za-z0-9:_-]{1,80})[`'\"]\s+script\b",
    re.IGNORECASE,
)

_TEST_FILE_RE = re.compile(
    r"(?:^test(?:s)?[._-]|\.test\.|\.spec\.|^test\.)",
    re.IGNORECASE,
)


class AgentAcceptanceContractError(Exception):
    pass


def initialize_acceptance_storage():
    global _STORAGE_READY
    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_acceptance_contracts (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            goal_hash TEXT NOT NULL,
            contract_json TEXT NOT NULL DEFAULT '{}',
            latest_evaluation_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()
    _STORAGE_READY = True


def _dedupe(values):
    result = []
    seen = set()
    for value in values:
        item = str(value or "").strip()
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _normalize_behavior(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"\b(?:behavior|behaviour|handling|test|tests|case|cases)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _split_behaviors(text):
    value = str(text or "").strip()
    if not value:
        return []
    parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", value, flags=re.IGNORECASE)
    return _dedupe(
        _normalize_behavior(part)
        for part in parts
        if _normalize_behavior(part)
    )


def derive_acceptance_contract(goal):
    goal_text = str(goal or "")
    lower = goal_text.lower()

    required_files = _dedupe(
        match.group(1)
        for match in _FILE_RE.finditer(goal_text)
        if match.group(1).lower() not in {
            "node.js",
            "javascript.js",
        }
    )

    usage_dependencies = _dedupe(
        match.group(1)
        for match in _DEPENDENCY_PATTERNS[0].finditer(goal_text)
    )

    dependencies = []
    for pattern in _DEPENDENCY_PATTERNS:
        dependencies.extend(
            match.group(1)
            for match in pattern.finditer(goal_text)
        )
    dependencies = _dedupe(dependencies)

    required_scripts = _dedupe(
        match.group(1)
        for match in _SCRIPT_RE.finditer(goal_text)
    )

    test_counts = [
        int(match.group(1))
        for match in _TEST_COUNT_RE.finditer(goal_text)
    ]
    min_test_count = max(test_counts) if test_counts else 0

    behaviors = []
    for match in _TEST_COVERING_RE.finditer(goal_text):
        behaviors.extend(
            _split_behaviors(match.group(1))
        )
    behaviors = _dedupe(behaviors)

    requires_verified_execution = bool(
        re.search(
            r"\b(?:finish|complete|done|claim\s+completion).{0,80}\b(?:pass|passes|passing|verified|verification)\b",
            lower,
            re.IGNORECASE | re.DOTALL,
        )
        or re.search(
            r"\b(?:run|re-run|rerun).{0,50}\btests?\b",
            lower,
            re.IGNORECASE | re.DOTALL,
        )
    )

    protected_dependencies = []
    for dependency in dependencies:
        if re.search(
            r"(?:do\s+not|don't|must\s+not).{0,80}(?:replace|remove|avoid).{0,80}"
            + re.escape(dependency),
            lower,
            re.IGNORECASE | re.DOTALL,
        ) or re.search(
            r"(?:do\s+not|don't|must\s+not).{0,80}"
            + re.escape(dependency)
            + r".{0,80}(?:replace|remove|avoid)",
            lower,
            re.IGNORECASE | re.DOTALL,
        ):
            protected_dependencies.append(dependency)

    return {
        "version": 1,
        "required_files": required_files,
        "required_dependencies": dependencies,
        "required_dependency_usage": usage_dependencies,
        "protected_dependencies": _dedupe(protected_dependencies),
        "required_scripts": required_scripts,
        "min_test_count": int(min_test_count),
        "required_test_behaviors": behaviors,
        "requires_verified_execution": requires_verified_execution,
    }


def _goal_hash(goal):
    return hashlib.sha256(
        str(goal or "").encode("utf-8", errors="ignore")
    ).hexdigest()[:24]


def get_or_create_acceptance_contract(run):
    initialize_acceptance_storage()
    goal = str(run.get("goal") or "")
    goal_hash = _goal_hash(goal)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT goal_hash, contract_json
        FROM agent_acceptance_contracts
        WHERE run_id = ? AND user_id = ?
        """,
        (str(run["id"]), int(run["user_id"])),
    )
    row = cursor.fetchone()

    if row and str(row[0]) == goal_hash:
        try:
            contract = json.loads(row[1] or "{}")
        except Exception:
            contract = {}
        if isinstance(contract, dict) and contract:
            conn.close()
            return contract

    contract = derive_acceptance_contract(goal)
    timestamp = utc_iso()
    cursor.execute(
        """
        INSERT INTO agent_acceptance_contracts (
            run_id, user_id, goal_hash, contract_json,
            latest_evaluation_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, '{}', ?, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            user_id = excluded.user_id,
            goal_hash = excluded.goal_hash,
            contract_json = excluded.contract_json,
            updated_at = excluded.updated_at
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            goal_hash,
            json.dumps(contract, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    conn.close()
    return contract


def _test_names(project_contract):
    names = []
    for item in list(project_contract.get("files") or []):
        for test in list(item.get("tests") or []):
            name = str(test.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _word_roots(text):
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    roots = []
    aliases = {
        "adds": "add",
        "adding": "add",
        "added": "add",
        "removes": "remove",
        "removing": "remove",
        "removed": "remove",
        "completes": "complete",
        "completing": "complete",
        "completed": "complete",
        "lists": "list",
        "listing": "list",
        "listed": "list",
        "tasks": "task",
        "invalidates": "invalid",
    }
    for word in words:
        roots.append(aliases.get(word, word))
    return set(roots)


def behavior_is_covered(behavior, test_names):
    required = _word_roots(behavior)
    if not required:
        return True
    for name in test_names:
        available = _word_roots(name)
        if required.issubset(available):
            return True
    return False


def _acceptance_fingerprint(issues):
    payload = [
        {
            "type": item.get("type"),
            "item": item.get("item"),
            "required": item.get("required"),
            "actual": item.get("actual"),
        }
        for item in issues
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def evaluate_acceptance_contract(run, project_contract, sandbox_verified=None):
    contract = get_or_create_acceptance_contract(run)

    workspace_files = {
        str(item.get("filename") or "")
        for item in list_workspace_files(run["user_id"], run["id"])
        if str(item.get("filename") or "")
    }

    package = project_contract.get("package") or {}
    dependencies = {
        str(name).lower(): value
        for name, value in (package.get("dependencies") or {}).items()
    }
    scripts = {
        str(name).lower(): value
        for name, value in (package.get("scripts") or {}).items()
    }
    test_names = _test_names(project_contract)
    imported_packages = set()
    for item in list(project_contract.get("files") or []):
        for imported in list(item.get("imports") or []):
            normalized = str(
                imported.get("normalized")
                or imported.get("specifier")
                or ""
            ).strip().lower()
            if normalized and not normalized.startswith("."):
                imported_packages.add(normalized)

    issues = []

    for filename in contract.get("required_files") or []:
        if filename not in workspace_files:
            issues.append(
                {
                    "type": "missing_required_file",
                    "item": filename,
                    "message": f"Required deliverable is missing: {filename}.",
                }
            )

    for dependency in contract.get("required_dependencies") or []:
        if dependency.lower() not in dependencies:
            issues.append(
                {
                    "type": "missing_required_dependency",
                    "item": dependency,
                    "message": f"Required npm dependency is missing: {dependency}.",
                }
            )

    for dependency in contract.get("required_dependency_usage") or []:
        if dependency.lower() not in imported_packages:
            issues.append(
                {
                    "type": "required_dependency_not_used",
                    "item": dependency,
                    "message": (
                        f"The goal requires using npm dependency {dependency}, but no "
                        "current source file imports/requires it."
                    ),
                }
            )

    for script in contract.get("required_scripts") or []:
        if script.lower() not in scripts:
            issues.append(
                {
                    "type": "missing_required_script",
                    "item": script,
                    "message": f"Required package.json script is missing: {script}.",
                }
            )

    minimum = int(contract.get("min_test_count") or 0)
    if minimum and len(test_names) < minimum:
        issues.append(
            {
                "type": "insufficient_test_count",
                "item": "tests",
                "required": minimum,
                "actual": len(test_names),
                "message": (
                    f"Acceptance requires at least {minimum} tests, but the current "
                    f"workspace defines {len(test_names)}."
                ),
            }
        )

    for behavior in contract.get("required_test_behaviors") or []:
        if not behavior_is_covered(behavior, test_names):
            issues.append(
                {
                    "type": "missing_required_test_behavior",
                    "item": behavior,
                    "message": f"No current test covers the required behavior: {behavior}.",
                }
            )

    if (
        sandbox_verified is False
        and contract.get("requires_verified_execution")
    ):
        issues.append(
            {
                "type": "sandbox_verification_required",
                "item": "sandbox",
                "message": "The goal requires a verified passing execution for the current workspace.",
            }
        )

    evaluation = {
        "satisfied": not issues,
        "issues": issues,
        "issue_count": len(issues),
        "fingerprint": _acceptance_fingerprint(issues) if issues else None,
        "test_count": len(test_names),
        "test_names": test_names,
        "contract": contract,
    }

    initialize_acceptance_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_acceptance_contracts
        SET latest_evaluation_json = ?, updated_at = ?
        WHERE run_id = ? AND user_id = ?
        """,
        (
            json.dumps(evaluation, ensure_ascii=False),
            utc_iso(),
            str(run["id"]),
            int(run["user_id"]),
        ),
    )
    conn.commit()
    conn.close()
    return evaluation


def acceptance_summary(evaluation, limit=12):
    if not evaluation:
        return "Acceptance contract unavailable."
    if evaluation.get("satisfied"):
        return "Acceptance contract: SATISFIED."

    lines = [
        "Acceptance contract: INCOMPLETE.",
        f"Outstanding requirements: {int(evaluation.get('issue_count') or 0)}",
    ]
    for issue in list(evaluation.get("issues") or [])[:limit]:
        lines.append("- " + str(issue.get("message") or issue.get("type") or "requirement"))
    return "\n".join(lines)


def is_test_file(filename, project_contract=None):
    name = str(filename or "")
    if _TEST_FILE_RE.search(name):
        return True
    if project_contract:
        return name in set(project_contract.get("test_files") or [])
    return False


def extract_test_names(source):
    return [
        match.group(1).strip()
        for match in re.finditer(
            r"\b(?:test|it)\s*\(\s*['\"]([^'\"]+)['\"]",
            str(source or ""),
        )
    ]


def validate_test_candidate(
    run,
    filename,
    current_source,
    candidate_source,
    project_contract,
):
    """Prevent a repair from making its verifier easier by deleting coverage."""
    if not is_test_file(filename, project_contract):
        return None

    acceptance = get_or_create_acceptance_contract(run)
    current_names = extract_test_names(current_source)
    candidate_names = extract_test_names(candidate_source)

    missing_existing = [
        name
        for name in current_names
        if name not in candidate_names
    ]
    if missing_existing:
        return (
            "Test-integrity guard rejected the repair because it removed existing "
            "test specification(s): " + ", ".join(missing_existing[:8])
        )

    minimum = max(
        len(current_names),
        int(acceptance.get("min_test_count") or 0),
    )
    if len(candidate_names) < minimum:
        return (
            f"Test-integrity guard requires at least {minimum} tests in {filename}; "
            f"the proposed repair contains {len(candidate_names)}."
        )

    missing_behaviors = [
        behavior
        for behavior in acceptance.get("required_test_behaviors") or []
        if not behavior_is_covered(behavior, candidate_names)
    ]
    if missing_behaviors:
        return (
            "Test-integrity guard rejected the repair because required behavior "
            "coverage is still missing: " + ", ".join(missing_behaviors[:8])
        )

    return None
