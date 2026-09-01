"""
ATLAS v2.1.1b - Persistent Project Contract / Debug Planner.

This layer sits between raw sandbox failures and the normal Agent controller.
It turns a multi-file workspace into a deterministic project contract, tracks
failure fingerprints/progress, persists recovery plans, and escalates difficult
planning to a stronger local model when Auto mode is selected.

It never executes host code. Python source is inspected with ast only.
"""

import ast
import hashlib
import json
import os
import re

from pathlib import Path

from app.config import (
    DEFAULT_MODEL,
    DEEP_MODEL,
)
from app.database import get_connection
from app.services import agent_runner as base_runner
from app.services.agents import (
    get_agent_run,
    list_agent_steps,
    utc_iso,
)
from app.services.agent_sandbox import (
    list_agent_sandbox_executions,
    list_workspace_files,
    read_workspace_file,
    sandbox_runtime_profile,
    write_workspace_file,
)


AGENT_WORKER_MODEL = os.environ.get(
    "PRIVATE_AI_AGENT_WORKER_MODEL",
    DEFAULT_MODEL,
).strip() or DEFAULT_MODEL

AGENT_REASONING_MODEL = os.environ.get(
    "PRIVATE_AI_AGENT_REASONING_MODEL",
    DEEP_MODEL,
).strip() or DEEP_MODEL

# Optional future tier. Leave unset today. When stronger hardware/model exists,
# configure it without changing the planner architecture.
AGENT_EXPERT_MODEL = os.environ.get(
    "PRIVATE_AI_AGENT_EXPERT_MODEL",
    "",
).strip()

AUTO_ESCALATION_ENABLED = (
    os.environ.get(
        "PRIVATE_AI_AGENT_AUTO_ESCALATION",
        "1",
    )
    != "0"
)

PROJECT_SOURCE_BUDGET = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_PROJECT_SOURCE_BUDGET",
        "18000",
    )
)

PROJECT_FILE_BUDGET = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_PROJECT_FILE_BUDGET",
        "6000",
    )
)

_STORAGE_READY = False


class AgentProjectPlannerError(Exception):
    pass


def initialize_agent_project_planner_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_project_states (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            project_kind TEXT NOT NULL DEFAULT 'python',
            contract_json TEXT NOT NULL DEFAULT '{}',
            latest_failure_fingerprint TEXT,
            repeated_failure_count INTEGER NOT NULL DEFAULT 0,
            progress_state TEXT NOT NULL DEFAULT 'unknown',
            contract_issue_count INTEGER NOT NULL DEFAULT 0,
            repair_churn_count INTEGER NOT NULL DEFAULT 0,
            escalation_count INTEGER NOT NULL DEFAULT 0,
            last_planner_tier TEXT,
            last_planner_model TEXT,
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

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_debug_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            trigger TEXT NOT NULL,
            failure_fingerprint TEXT,
            planner_tier TEXT NOT NULL,
            planner_model TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            next_repair_index INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_debug_plans_run
        ON agent_debug_plans(
            run_id,
            status,
            id
        )
        """
    )

    conn.commit()
    conn.close()
    _STORAGE_READY = True


def _safe_json(value, default=None):
    if isinstance(value, (dict, list)):
        return value

    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def _signature(node, drop_first=False):
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    positional_names = [item.arg for item in positional]

    if drop_first and positional_names:
        positional_names = positional_names[1:]

    defaults = list(args.defaults)
    default_count = len(defaults)
    required_positional = max(
        0,
        len(positional_names) - default_count,
    )

    keyword_only = [item.arg for item in args.kwonlyargs]
    required_kwonly = [
        item.arg
        for item, default
        in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    ]

    return {
        "parameters": positional_names + keyword_only,
        "required_positional": required_positional,
        "max_positional": (
            None
            if args.vararg
            else len(positional_names)
        ),
        "vararg": bool(args.vararg),
        "kwarg": bool(args.kwarg),
        "required_kwonly": required_kwonly,
        "display": (
            "("
            + ", ".join(positional_names + keyword_only)
            + (
                ", ..."
                if args.vararg or args.kwarg
                else ""
            )
            + ")"
        ),
    }


def _module_name(filename):
    return Path(filename).stem


def _analyze_python_file(filename, source):
    item = {
        "filename": filename,
        "module": _module_name(filename),
        "parse_error": None,
        "imports": [],
        "functions": {},
        "classes": {},
        "calls": [],
    }

    try:
        tree = ast.parse(
            source,
            filename=filename,
        )
    except SyntaxError as error:
        item["parse_error"] = (
            f"{error.msg} at line {error.lineno}"
        )
        return item

    alias_map = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = str(node.module or "")

            for imported in node.names:
                alias = imported.asname or imported.name
                alias_map[alias] = {
                    "kind": "symbol",
                    "module": module,
                    "symbol": imported.name,
                }
                item["imports"].append({
                    "kind": "from",
                    "module": module,
                    "symbol": imported.name,
                    "alias": alias,
                    "line": getattr(node, "lineno", None),
                })

        elif isinstance(node, ast.Import):
            for imported in node.names:
                alias = imported.asname or imported.name.split(".")[0]
                alias_map[alias] = {
                    "kind": "module",
                    "module": imported.name,
                    "symbol": None,
                }
                item["imports"].append({
                    "kind": "import",
                    "module": imported.name,
                    "symbol": None,
                    "alias": alias,
                    "line": getattr(node, "lineno", None),
                })

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            item["functions"][node.name] = _signature(node)

        elif isinstance(node, ast.ClassDef):
            methods = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[child.name] = _signature(
                        child,
                        drop_first=True,
                    )

            item["classes"][node.name] = {
                "methods": methods,
                "constructor": methods.get(
                    "__init__",
                    {
                        "parameters": [],
                        "required_positional": 0,
                        "max_positional": 0,
                        "vararg": False,
                        "kwarg": False,
                        "required_kwonly": [],
                        "display": "()",
                    },
                ),
            }

    local_classes = set(item["classes"].keys())
    instance_types = {}

    # Very small static type inference is enough to connect common generated
    # tests such as manager = TaskManager(...); manager.add_task(...).
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = getattr(node, "value", None)
            target_nodes = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )

            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                called = value.func.id
                resolved_type = None

                if called in local_classes:
                    resolved_type = {
                        "module": item["module"],
                        "class": called,
                    }
                elif (
                    called in alias_map
                    and alias_map[called]["kind"] == "symbol"
                ):
                    resolved_type = {
                        "module": alias_map[called]["module"],
                        "class": alias_map[called]["symbol"],
                    }

                if resolved_type:
                    for target in target_nodes:
                        if isinstance(target, ast.Name):
                            instance_types[target.id] = resolved_type

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call = {
            "line": getattr(node, "lineno", None),
            "positional_count": len(node.args),
            "keywords": [
                keyword.arg
                for keyword in node.keywords
                if keyword.arg
            ],
            "kind": "unknown",
            "name": None,
            "module": None,
            "symbol": None,
            "class": None,
        }

        if isinstance(node.func, ast.Name):
            name = node.func.id
            call["name"] = name

            if name in alias_map:
                target = alias_map[name]
                call["module"] = target["module"]
                call["symbol"] = target["symbol"]
                call["kind"] = (
                    "imported_symbol"
                    if target["kind"] == "symbol"
                    else "module"
                )
            elif name in item["functions"]:
                call["module"] = item["module"]
                call["symbol"] = name
                call["kind"] = "local_symbol"
            elif name in item["classes"]:
                call["module"] = item["module"]
                call["symbol"] = name
                call["kind"] = "local_class"

        elif isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            call["name"] = attr

            if isinstance(node.func.value, ast.Name):
                base = node.func.value.id

                if (
                    base in alias_map
                    and alias_map[base]["kind"] == "module"
                ):
                    call["kind"] = "module_attribute"
                    call["module"] = alias_map[base]["module"]
                    call["symbol"] = attr

                elif base in instance_types:
                    resolved = instance_types[base]
                    call["kind"] = "instance_method"
                    call["module"] = resolved["module"]
                    call["class"] = resolved["class"]
                    call["symbol"] = attr

        item["calls"].append(call)

    return item


def _arity_issue(call, signature, label):
    if not signature:
        return None

    positional = int(call.get("positional_count") or 0)
    keywords = set(call.get("keywords") or [])

    required = int(signature.get("required_positional") or 0)
    maximum = signature.get("max_positional")

    if positional < required:
        return (
            f"{label} is called with {positional} positional argument(s) but "
            f"requires at least {required}."
        )

    if maximum is not None and positional > int(maximum):
        return (
            f"{label} is called with {positional} positional argument(s) but "
            f"accepts at most {maximum}."
        )

    if not signature.get("kwarg"):
        allowed = set(signature.get("parameters") or [])
        unexpected = sorted(
            keyword
            for keyword in keywords
            if keyword not in allowed
        )
        if unexpected:
            return (
                f"{label} receives unexpected keyword argument(s): "
                + ", ".join(unexpected)
                + "."
            )

    return None


def build_project_contract(run):
    files = list_workspace_files(
        run["user_id"],
        run["id"],
    )

    python_files = [
        item
        for item in files
        if str(item.get("filename") or "").lower().endswith(".py")
    ]

    analyzed = []
    sources = {}

    for item in python_files:
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue

        try:
            source = read_workspace_file(
                run["user_id"],
                run["id"],
                filename,
                max_chars=256000,
            )
        except Exception:
            source = ""

        sources[filename] = source
        analyzed.append(
            _analyze_python_file(
                filename,
                source,
            )
        )

    modules = {
        item["module"]: item
        for item in analyzed
    }

    issues = []

    for item in analyzed:
        if item.get("parse_error"):
            issues.append({
                "type": "syntax_error",
                "file": item["filename"],
                "line": None,
                "message": item["parse_error"],
                "severity": "high",
            })

        for imported in item["imports"]:
            module_name = str(imported.get("module") or "").split(".")[0]
            target_module = modules.get(module_name)

            if not target_module:
                continue

            symbol = imported.get("symbol")
            if not symbol or symbol == "*":
                continue

            if (
                symbol not in target_module["functions"]
                and symbol not in target_module["classes"]
            ):
                issues.append({
                    "type": "missing_imported_symbol",
                    "file": item["filename"],
                    "line": imported.get("line"),
                    "module": module_name,
                    "symbol": symbol,
                    "message": (
                        f"{item['filename']} imports {symbol} from {module_name}.py, "
                        "but that symbol is not currently defined there."
                    ),
                    "severity": "high",
                })

        for call in item["calls"]:
            module_name = str(call.get("module") or "").split(".")[0]
            target_module = modules.get(module_name)
            if not target_module:
                continue

            signature = None
            label = None

            if call["kind"] in {
                "imported_symbol",
                "local_symbol",
                "module_attribute",
            }:
                symbol = call.get("symbol")
                if symbol in target_module["functions"]:
                    signature = target_module["functions"][symbol]
                    label = f"{module_name}.{symbol}"
                elif symbol in target_module["classes"]:
                    signature = target_module["classes"][symbol]["constructor"]
                    label = f"{module_name}.{symbol}"

            elif call["kind"] in {
                "local_class",
            }:
                symbol = call.get("symbol")
                if symbol in target_module["classes"]:
                    signature = target_module["classes"][symbol]["constructor"]
                    label = f"{module_name}.{symbol}"

            elif call["kind"] == "instance_method":
                class_name = call.get("class")
                symbol = call.get("symbol")
                class_info = target_module["classes"].get(class_name)
                if class_info:
                    signature = class_info["methods"].get(symbol)
                    label = f"{module_name}.{class_name}.{symbol}"

            issue_message = _arity_issue(
                call,
                signature,
                label,
            )

            if issue_message:
                issues.append({
                    "type": "signature_mismatch",
                    "file": item["filename"],
                    "line": call.get("line"),
                    "message": issue_message,
                    "severity": "medium",
                })

    # Collapse exact static duplicates.
    deduped = []
    seen = set()
    for issue in issues:
        key = (
            issue.get("type"),
            issue.get("file"),
            issue.get("line"),
            issue.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)

    return {
        "kind": "python",
        "file_count": len(python_files),
        "files": analyzed,
        "issues": deduped[:60],
        "source_files": list(sources.keys()),
    }


def _failure_signature(execution):
    if not execution:
        return {
            "fingerprint": None,
            "type": None,
            "message": None,
            "filename": None,
            "status": None,
            "exit_code": None,
            "environment_dependency": None,
        }

    stderr = str(execution.get("stderr") or "")
    filename = str(execution.get("filename") or "")

    exception_type = None
    message = None

    exception_re = re.compile(
        r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Failure)):\s*(.*)$"
    )

    for line in reversed(stderr.splitlines()):
        stripped = line.strip()
        match = exception_re.match(stripped)
        if match:
            exception_type = match.group(1)
            message = match.group(2).strip()
            break

    if not exception_type:
        for line in reversed(stderr.splitlines()):
            stripped = line.strip()
            if stripped:
                exception_type = "sandbox_failure"
                message = stripped
                break

    normalized = str(message or "")
    normalized = re.sub(r"/workspace/", "", normalized)
    normalized = re.sub(r"line\s+\d+", "line #", normalized, flags=re.I)
    normalized = re.sub(r"0x[0-9a-fA-F]+", "0x#", normalized)
    normalized = " ".join(normalized.split())[:1000]

    raw = "|".join([
        filename,
        str(exception_type or ""),
        normalized,
    ])

    fingerprint = hashlib.sha1(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()[:16]

    environment_dependency = None
    if exception_type == "ModuleNotFoundError":
        dependency = re.search(
            r"No module named ['\"]([^'\"]+)['\"]",
            normalized,
        )
        if dependency:
            environment_dependency = dependency.group(1).split(".")[0]

    return {
        "fingerprint": fingerprint,
        "type": exception_type,
        "message": normalized,
        "filename": filename,
        "status": execution.get("status"),
        "exit_code": execution.get("exit_code"),
        "environment_dependency": environment_dependency,
    }


def _execution_analysis(run):
    rows = list_agent_sandbox_executions(
        run["user_id"],
        run["id"],
        limit=100,
    )

    if not rows:
        return {
            "latest": None,
            "failure": _failure_signature(None),
            "repeated_failure_count": 0,
            "progress_state": "untested",
            "executions": [],
        }

    latest = rows[-1]
    latest_status = str(latest.get("status") or "")
    latest_exit = int(latest.get("exit_code") or 0)

    if latest_status == "success" and latest_exit == 0:
        return {
            "latest": latest,
            "failure": _failure_signature(latest),
            "repeated_failure_count": 0,
            "progress_state": "verified_execution",
            "executions": rows,
        }

    latest_failure = _failure_signature(latest)
    repeated = 0

    for item in reversed(rows):
        status = str(item.get("status") or "")
        exit_code = int(item.get("exit_code") or 0)
        if status == "success" and exit_code == 0:
            break

        signature = _failure_signature(item)
        if signature["fingerprint"] == latest_failure["fingerprint"]:
            repeated += 1
        else:
            break

    progress_state = (
        "stalled"
        if repeated >= 2
        else "new_failure"
    )

    if len(rows) >= 2:
        previous = rows[-2]
        previous_status = str(previous.get("status") or "")
        previous_exit = int(previous.get("exit_code") or 0)
        if not (
            previous_status == "success"
            and previous_exit == 0
        ):
            previous_failure = _failure_signature(previous)
            if (
                previous_failure["fingerprint"]
                and previous_failure["fingerprint"]
                != latest_failure["fingerprint"]
            ):
                progress_state = "failure_changed_progress"

    return {
        "latest": latest,
        "failure": latest_failure,
        "repeated_failure_count": repeated,
        "progress_state": progress_state,
        "executions": rows,
    }


def _repair_churn(run):
    steps = list_agent_steps(
        run["user_id"],
        run["id"],
    )[-14:]

    names = []
    pattern = re.compile(
        r"(?:Created|Updated) workspace file:\s*([^\s(]+)"
    )

    for step in steps:
        if str(step.get("action") or "") != "write_file":
            continue
        output = str(step.get("output") or "")
        match = pattern.search(output)
        if match:
            names.append(match.group(1))

    if not names:
        return 0

    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1

    return max(counts.values())


def _state_from_row(row):
    if not row:
        return None

    return {
        "run_id": row[0],
        "user_id": row[1],
        "project_kind": row[2],
        "contract": _safe_json(row[3], {}),
        "latest_failure_fingerprint": row[4],
        "repeated_failure_count": int(row[5] or 0),
        "progress_state": row[6],
        "contract_issue_count": int(row[7] or 0),
        "repair_churn_count": int(row[8] or 0),
        "escalation_count": int(row[9] or 0),
        "last_planner_tier": row[10],
        "last_planner_model": row[11],
        "updated_at": row[12],
    }


def analyze_project_state(run):
    initialize_agent_project_planner_storage()

    contract = build_project_contract(run)
    execution = _execution_analysis(run)
    churn = _repair_churn(run)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT escalation_count
        FROM agent_project_states
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
        ),
    )

    existing = cursor.fetchone()
    escalation_count = int(existing[0] or 0) if existing else 0

    cursor.execute(
        """
        INSERT INTO agent_project_states (
            run_id,
            user_id,
            project_kind,
            contract_json,
            latest_failure_fingerprint,
            repeated_failure_count,
            progress_state,
            contract_issue_count,
            repair_churn_count,
            escalation_count,
            last_planner_tier,
            last_planner_model,
            updated_at
        )
        VALUES (?, ?, 'python', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            contract_json = excluded.contract_json,
            latest_failure_fingerprint = excluded.latest_failure_fingerprint,
            repeated_failure_count = excluded.repeated_failure_count,
            progress_state = excluded.progress_state,
            contract_issue_count = excluded.contract_issue_count,
            repair_churn_count = excluded.repair_churn_count,
            updated_at = excluded.updated_at
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            json.dumps(contract, ensure_ascii=False),
            execution["failure"]["fingerprint"],
            int(execution["repeated_failure_count"]),
            execution["progress_state"],
            len(contract["issues"]),
            int(churn),
            escalation_count,
            utc_iso(),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "contract": contract,
        "execution": execution,
        "repair_churn_count": churn,
        "escalation_count": escalation_count,
    }


def get_project_state(user_id, run_id):
    initialize_agent_project_planner_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            run_id,
            user_id,
            project_kind,
            contract_json,
            latest_failure_fingerprint,
            repeated_failure_count,
            progress_state,
            contract_issue_count,
            repair_churn_count,
            escalation_count,
            last_planner_tier,
            last_planner_model,
            updated_at
        FROM agent_project_states
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )

    result = _state_from_row(cursor.fetchone())
    conn.close()
    return result


def _plan_from_row(row):
    if not row:
        return None

    return {
        "id": row[0],
        "run_id": row[1],
        "user_id": row[2],
        "trigger": row[3],
        "failure_fingerprint": row[4],
        "planner_tier": row[5],
        "planner_model": row[6],
        "plan": _safe_json(row[7], {}),
        "next_repair_index": int(row[8] or 0),
        "status": row[9],
        "created_at": row[10],
        "completed_at": row[11],
    }


def get_active_debug_plan(user_id, run_id):
    initialize_agent_project_planner_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            run_id,
            user_id,
            trigger,
            failure_fingerprint,
            planner_tier,
            planner_model,
            plan_json,
            next_repair_index,
            status,
            created_at,
            completed_at
        FROM agent_debug_plans
        WHERE
            run_id = ?
            AND user_id = ?
            AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )

    plan = _plan_from_row(cursor.fetchone())
    conn.close()
    return plan


def _debug_plan_count(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM agent_debug_plans
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )
    count = int(cursor.fetchone()[0] or 0)
    conn.close()
    return count


def _debug_plan_count_for_failure(
    user_id,
    run_id,
    failure_fingerprint,
):
    if not failure_fingerprint:
        return 0

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM agent_debug_plans
        WHERE
            run_id = ?
            AND user_id = ?
            AND failure_fingerprint = ?
        """,
        (
            str(run_id),
            int(user_id),
            str(failure_fingerprint),
        ),
    )

    count = int(
        cursor.fetchone()[0]
        or 0
    )

    conn.close()
    return count


def active_plan_matches_current_failure(
    user_id,
    run_id,
    current_failure_fingerprint,
):
    """
    An active repair plan is valid only for the failure state that produced it.

    After every project repair ATLAS re-runs the test. If the failure changes,
    that is progress and the old plan becomes stale. Continuing the remaining
    repairs from the old plan can reintroduce incompatibilities or chase a
    problem that no longer exists.
    """
    plan = get_active_debug_plan(
        user_id,
        run_id,
    )

    if not plan:
        return True

    planned = str(
        plan.get(
            "failure_fingerprint"
        )
        or ""
    ).strip()

    current = str(
        current_failure_fingerprint
        or ""
    ).strip()

    if not planned or not current:
        return True

    return planned == current


def mark_active_plan_superseded(
    user_id,
    run_id,
):
    initialize_agent_project_planner_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET
            status = 'superseded',
            completed_at = ?
        WHERE
            run_id = ?
            AND user_id = ?
            AND status = 'active'
        """,
        (
            utc_iso(),
            str(run_id),
            int(user_id),
        ),
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def structured_planner_exhausted_for_current_failure(
    run,
    analysis=None,
):
    """
    Stop burning steps on repeated senior-planner cycles that reproduce the
    exact same failure.

    If an expert tier is configured, it still gets a chance. Without one,
    two completed/superseded plans for the same exact fingerprint are enough
    evidence that the current deterministic + reasoning toolset is stuck.
    Continue/Revise or a future richer debugger can take over without spending
    dozens more steps on the same loop.
    """
    analysis = analysis or analyze_project_state(
        run
    )

    fingerprint = (
        analysis[
            "execution"
        ][
            "failure"
        ].get(
            "fingerprint"
        )
    )

    if not fingerprint:
        return False

    if AGENT_EXPERT_MODEL:
        # The expert tier can still be selected on a later plan.
        return False

    plan_count = _debug_plan_count_for_failure(
        run[
            "user_id"
        ],
        run[
            "id"
        ],
        fingerprint,
    )

    repeated = int(
        analysis[
            "execution"
        ].get(
            "repeated_failure_count"
        )
        or 0
    )

    return (
        plan_count >= 2
        and repeated >= 2
    )


def _choose_planner_model(run, analysis):
    mode = str(run.get("model_mode") or "auto").strip().lower()

    if mode != "auto":
        selected_mode, selected_model = base_runner._select_agent_model(run)
        return {
            "tier": f"manual_{selected_mode}",
            "model": selected_model,
            "escalated": False,
        }

    execution = analysis["execution"]
    contract = analysis["contract"]
    repeated = int(execution["repeated_failure_count"] or 0)
    issues = len(contract["issues"])
    churn = int(analysis["repair_churn_count"] or 0)
    current_failure_fingerprint = (
        execution["failure"].get("fingerprint")
    )

    plan_count = _debug_plan_count_for_failure(
        run["user_id"],
        run["id"],
        current_failure_fingerprint,
    )

    failure_type = str(execution["failure"].get("type") or "")
    cross_file_failure = (
        contract["file_count"] >= 2
        and (
            issues > 0
            or failure_type in {
                "ImportError",
                "AttributeError",
                "TypeError",
                "NameError",
            }
        )
    )

    use_expert = bool(
        AUTO_ESCALATION_ENABLED
        and AGENT_EXPERT_MODEL
        and (
            repeated >= 4
            or plan_count >= 2
            or issues >= 6
        )
    )

    if use_expert:
        return {
            "tier": "expert",
            "model": AGENT_EXPERT_MODEL,
            "escalated": True,
        }

    use_reasoning = bool(
        AUTO_ESCALATION_ENABLED
        and (
            repeated >= 2
            or cross_file_failure
            or churn >= 3
            or plan_count >= 1
        )
    )

    if use_reasoning:
        return {
            "tier": "reasoning",
            "model": AGENT_REASONING_MODEL,
            "escalated": True,
        }

    return {
        "tier": "worker",
        "model": AGENT_WORKER_MODEL,
        "escalated": False,
    }


def _environment_dependency_block(analysis):
    dependency = analysis["execution"]["failure"].get(
        "environment_dependency"
    )
    if not dependency:
        return None

    local_modules = {
        item["module"]
        for item in analysis["contract"]["files"]
    }

    if dependency in local_modules:
        return None

    return dependency


def _filesystem_environment_block(analysis):
    """
    Evidence-based filesystem blocker classification.

    Important:
    - Directory-not-empty is a cleanup/test bug, not a sandbox permission error.
    - Normal writes under /runtime and /tmp are supported.
    - A true read-only/permission failure is environmental only when stderr
      actually says so.
    """
    latest = analysis[
        "execution"
    ].get(
        "latest"
    )

    if not latest:
        return None

    stderr = str(
        latest.get(
            "stderr"
        )
        or ""
    )

    lowered = stderr.lower()

    if (
        "directory not empty" in lowered
        or "errno 39" in lowered
    ):
        return None

    true_fs_markers = (
        "read-only file system",
        "permission denied",
        "operation not permitted",
    )

    if not any(
        marker in lowered
        for marker in true_fs_markers
    ):
        return None

    return (
        "The latest sandbox stderr contains an actual filesystem permission/"
        "read-only error. The sandbox normally provides writable /runtime and "
        "/tmp, while /workspace is intentionally source-only."
    )


def _environment_profile_text():
    profile = sandbox_runtime_profile()

    return (
        "Sandbox capability profile:\\n"
        f"- durable source mount: {profile['source_mount']} (read-only)\\n"
        f"- execution working directory: {profile['runtime_workdir']} "
        f"(writable, ephemeral tmpfs {profile['runtime_tmpfs']})\\n"
        f"- temp directory: {profile['tmp_dir']} (writable)\\n"
        f"- network: {'enabled' if profile['network'] else 'disabled'}\\n"
        f"- runs as root: {'yes' if profile['runs_as_root'] else 'no'}\\n"
        f"- dependency installation during execution: "
        f"{'enabled' if profile['dependency_installation'] else 'disabled'}\\n"
        f"- dependency note: {profile['dependency_note']}\\n"
        "Therefore, do NOT classify tempfile usage, os imports, JSON/SQLite "
        "runtime writes, or 'Directory not empty' cleanup errors as sandbox "
        "read-only limitations merely because the durable source mount is read-only."
    )


def should_create_debug_plan(run, analysis=None):
    analysis = analysis or analyze_project_state(run)
    latest = analysis["execution"]["latest"]

    if not latest:
        return False

    if (
        str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
    ):
        return False

    active = get_active_debug_plan(
        run["user_id"],
        run["id"],
    )

    if active:
        repairs = list(active["plan"].get("repair_sequence") or [])
        if int(active["next_repair_index"] or 0) < len(repairs):
            return False

    contract = analysis["contract"]
    repeated = int(analysis["execution"]["repeated_failure_count"] or 0)
    churn = int(analysis["repair_churn_count"] or 0)
    failure_type = str(analysis["execution"]["failure"].get("type") or "")

    if _environment_dependency_block(analysis):
        return True

    if contract["issues"]:
        return True

    if repeated >= 2 or churn >= 3:
        return True

    if (
        contract["file_count"] >= 2
        and failure_type in {
            "ImportError",
            "AttributeError",
            "TypeError",
            "NameError",
        }
    ):
        return True

    return False


def _contract_summary(analysis):
    contract = analysis["contract"]
    execution = analysis["execution"]

    lines = [
        f"Python files: {contract['file_count']}",
        f"Progress state: {execution['progress_state']}",
        f"Repeated latest failure: {execution['repeated_failure_count']}",
        f"Recent repair churn: {analysis['repair_churn_count']}",
    ]

    failure = execution["failure"]
    if failure.get("fingerprint"):
        lines.append(
            "Latest failure: "
            + str(failure.get("type") or "failure")
            + ": "
            + str(failure.get("message") or "")
        )

    dependency = _environment_dependency_block(analysis)
    if dependency:
        lines.append(
            f"Environment dependency unavailable: {dependency}"
        )

    filesystem_block = _filesystem_environment_block(
        analysis
    )

    if filesystem_block:
        lines.append(
            "Environment filesystem blocker: "
            + filesystem_block
        )

    if contract["issues"]:
        lines.append("Static contract issues:")
        for issue in contract["issues"][:20]:
            location = issue.get("file") or "workspace"
            if issue.get("line"):
                location += f":{issue['line']}"
            lines.append(
                f"- [{issue.get('type')}] {location}: {issue.get('message')}"
            )
    else:
        lines.append("Static contract issues: none detected.")

    lines.append("Module API map:")
    for item in contract["files"][:20]:
        defs = []
        defs.extend(
            name + info.get("display", "")
            for name, info in item["functions"].items()
        )
        for class_name, class_info in item["classes"].items():
            methods = ", ".join(
                name + info.get("display", "")
                for name, info in class_info["methods"].items()
                if name != "__init__"
            )
            defs.append(
                f"class {class_name}{class_info['constructor'].get('display', '()')}"
                + (f" methods[{methods}]" if methods else "")
            )
        lines.append(
            f"- {item['filename']}: "
            + ("; ".join(defs) if defs else "no top-level API detected")
        )

    return "\n".join(lines)[:16000]


def _workspace_source_bundle(run, analysis):
    priority = []
    failure = analysis["execution"]["failure"]
    latest_filename = str(failure.get("filename") or "")

    def add(name):
        if name and name not in priority:
            priority.append(name)

    add(latest_filename)

    for issue in analysis["contract"]["issues"]:
        add(str(issue.get("file") or ""))
        module = str(issue.get("module") or "")
        if module:
            add(module + ".py")

    for item in analysis["contract"]["files"]:
        if item["filename"].lower().startswith("test_"):
            add(item["filename"])

    for item in analysis["contract"]["files"]:
        add(item["filename"])

    available = {
        item["filename"]
        for item in analysis["contract"]["files"]
    }

    blocks = []
    used = 0

    for filename in priority:
        if filename not in available or used >= PROJECT_SOURCE_BUDGET:
            continue

        try:
            content = read_workspace_file(
                run["user_id"],
                run["id"],
                filename,
                max_chars=PROJECT_FILE_BUDGET,
            )
        except Exception:
            continue

        remaining = PROJECT_SOURCE_BUDGET - used
        block = (
            f"--- {filename} ---\n"
            + str(content or "")[:remaining]
        )
        blocks.append(block)
        used += len(block)

    return "\n\n".join(blocks)


def _run_with_fallback(run, system_prompt, user_prompt, model):
    try:
        return base_runner._run_model(
            run,
            system_prompt,
            user_prompt,
            response_format="json",
            model_override=model,
        )
    except Exception:
        if model == AGENT_WORKER_MODEL:
            raise

        raw, fallback_model = base_runner._run_model(
            run,
            system_prompt,
            user_prompt,
            response_format="json",
            model_override=AGENT_WORKER_MODEL,
        )
        return raw, fallback_model


def create_debug_plan(run, analysis=None):
    initialize_agent_project_planner_storage()
    analysis = analysis or analyze_project_state(run)

    model_choice = _choose_planner_model(
        run,
        analysis,
    )

    environment_dependency = _environment_dependency_block(analysis)

    system_prompt = (
        "You are the senior project-contract/debug planner for a persistent local "
        "software Agent. Do NOT rewrite code in this response. Produce a small, explicit "
        "repair plan grounded in the deterministic project contract and current source. "
        "Treat imports, definitions, signatures, callers and tests as one contract. "
        "Prefer coherent minimal repairs over ping-pong renaming. The requested user "
        "architecture is authoritative: never remove a requested framework/dependency "
        "merely because the current sandbox lacks it. Distinguish environment limitations "
        "from code defects using the deterministic SANDBOX CAPABILITY PROFILE below. "
        "The durable /workspace source mount being read-only does NOT mean the executed "
        "program cannot write files: it runs from writable ephemeral /runtime and also has "
        "writable /tmp. 'Directory not empty' is a cleanup/code issue, not permission denial. "
        "If a third-party dependency is absent from the sandbox, preserve "
        "the intended architecture and set blocked_by_environment=true unless legitimate "
        "dependency-independent testing remains.\n\n"
        "Return ONLY JSON with keys:\n"
        "summary (string), root_cause (string), confidence (0-1), "
        "blocked_by_environment (boolean), environment_note (string), "
        "contract_decisions (array of short strings), "
        "repair_sequence (array of objects with file, objective, reason), "
        "verification_target (existing .py test/runnable filename), "
        "stop_condition (string).\n\n"
        "Repair_sequence should normally contain 1-4 EXISTING workspace files in the "
        "order they should be repaired. Do not include a file unless changing it is "
        "actually necessary. Tests are specifications when they reflect the user goal; "
        "do not change tests merely to force green results."
    )

    user_prompt = (
        "USER GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nUSER REVISION/INPUT HISTORY:\n"
        + base_runner._inputs_text(run)
        + "\n\nDETERMINISTIC PROJECT CONTRACT:\n"
        + _contract_summary(analysis)
        + "\n\nCURRENT SOURCE SNAPSHOT:\n"
        + (_workspace_source_bundle(run, analysis) or "No source available.")
        + "\n\nSANDBOX CAPABILITY PROFILE:\n"
        + _environment_profile_text()
        + "\n\nRECENT SANDBOX HISTORY:\n"
        + _execution_history_text(analysis)
        + (
            "\n\nKNOWN SANDBOX DEPENDENCY LIMITATION:\n"
            + environment_dependency
            if environment_dependency
            else ""
        )
    )

    raw, actual_model = _run_with_fallback(
        run,
        system_prompt,
        user_prompt,
        model_choice["model"],
    )

    data = base_runner._safe_json_object(
        raw,
        "project debug planner",
    )

    existing_files = {
        item["filename"]
        for item in analysis["contract"]["files"]
    }

    repairs = []
    for item in list(data.get("repair_sequence") or [])[:6]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("file") or "").strip()
        if filename not in existing_files:
            continue
        repairs.append({
            "file": filename,
            "objective": str(item.get("objective") or "").strip()[:1200],
            "reason": str(item.get("reason") or "").strip()[:1200],
        })

    verification_target = str(
        data.get("verification_target")
        or ""
    ).strip()

    if verification_target not in existing_files:
        verification_target = _preferred_test_target(analysis)

    filesystem_block = _filesystem_environment_block(
        analysis
    )

    # Environment blocking is evidence-based. The planner model may suggest a
    # blocker, but it cannot turn a normal code/test failure into an environment
    # limitation without deterministic support.
    blocked = bool(
        environment_dependency
        or filesystem_block
    )

    plan = {
        "summary": str(data.get("summary") or "").strip()[:2000],
        "root_cause": str(data.get("root_cause") or "").strip()[:3000],
        "confidence": _clamp_float(data.get("confidence"), 0.0, 1.0, 0.7),
        "blocked_by_environment": blocked,
        "environment_note": str(data.get("environment_note") or "").strip()[:2000],
        "contract_decisions": [
            str(item).strip()[:1000]
            for item in list(data.get("contract_decisions") or [])[:10]
            if str(item).strip()
        ],
        "repair_sequence": repairs,
        "verification_target": verification_target,
        "stop_condition": str(data.get("stop_condition") or "").strip()[:1600],
        "analysis": {
            "progress_state": analysis["execution"]["progress_state"],
            "repeated_failure_count": analysis["execution"]["repeated_failure_count"],
            "contract_issue_count": len(analysis["contract"]["issues"]),
            "repair_churn_count": analysis["repair_churn_count"],
        },
    }

    if blocked and not plan["environment_note"]:
        if environment_dependency:
            plan["environment_note"] = (
                "The latest failure requires a third-party dependency unavailable in "
                "the current sandbox image. Preserve the requested architecture rather "
                "than rewriting it away."
            )
        elif filesystem_block:
            plan["environment_note"] = filesystem_block

    trigger = _planner_trigger(analysis)
    failure_fingerprint = analysis["execution"]["failure"].get("fingerprint")
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET status = 'superseded', completed_at = ?
        WHERE run_id = ? AND user_id = ? AND status = 'active'
        """,
        (
            timestamp,
            str(run["id"]),
            int(run["user_id"]),
        ),
    )

    cursor.execute(
        """
        INSERT INTO agent_debug_plans (
            run_id,
            user_id,
            trigger,
            failure_fingerprint,
            planner_tier,
            planner_model,
            plan_json,
            next_repair_index,
            status,
            created_at,
            completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, NULL)
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            trigger,
            failure_fingerprint,
            model_choice["tier"],
            actual_model,
            json.dumps(plan, ensure_ascii=False),
            timestamp,
        ),
    )

    plan_id = cursor.lastrowid

    cursor.execute(
        """
        UPDATE agent_project_states
        SET
            escalation_count = escalation_count + ?,
            last_planner_tier = ?,
            last_planner_model = ?,
            updated_at = ?
        WHERE run_id = ? AND user_id = ?
        """,
        (
            1 if model_choice["escalated"] else 0,
            model_choice["tier"],
            actual_model,
            timestamp,
            str(run["id"]),
            int(run["user_id"]),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "id": plan_id,
        "run_id": run["id"],
        "user_id": run["user_id"],
        "trigger": trigger,
        "failure_fingerprint": failure_fingerprint,
        "planner_tier": model_choice["tier"],
        "planner_model": actual_model,
        "plan": plan,
        "next_repair_index": 0,
        "status": "active",
        "created_at": timestamp,
        "completed_at": None,
    }


def _execution_history_text(analysis):
    rows = analysis["execution"].get("executions") or []
    blocks = []
    for item in rows[-6:]:
        failure = _failure_signature(item)
        blocks.append(
            (
                f"{item.get('filename')} | {item.get('status')} | "
                f"exit={item.get('exit_code')} | fp={failure.get('fingerprint')}\n"
                f"{str(item.get('stderr') or '')[-1800:]}"
            )
        )
    return "\n\n".join(blocks)[-10000:]


def _planner_trigger(analysis):
    dependency = _environment_dependency_block(analysis)
    if dependency:
        return "environment_dependency"

    if analysis["contract"]["issues"]:
        return "project_contract_mismatch"

    if analysis["execution"]["repeated_failure_count"] >= 2:
        return "repeated_failure"

    if analysis["repair_churn_count"] >= 3:
        return "repair_churn"

    return "cross_file_failure"


def _preferred_test_target(analysis):
    names = [
        item["filename"]
        for item in analysis["contract"]["files"]
    ]
    lower = {name.lower(): name for name in names}
    for preferred in (
        "test_task_manager.py",
        "test_main.py",
        "test_app.py",
        "tests.py",
        "test.py",
    ):
        if preferred in lower:
            return lower[preferred]
    tests = [
        name
        for name in names
        if name.lower().startswith("test_")
    ]
    return tests[0] if tests else (names[0] if names else None)


def _clamp_float(value, low, high, fallback):
    try:
        number = float(value)
    except Exception:
        return fallback
    return max(low, min(high, number))


def format_debug_plan(plan):
    data = plan["plan"]
    lines = [
        "Project contract/debug plan created.",
        f"Trigger: {plan['trigger']}",
        f"Planner tier: {plan['planner_tier']}",
        f"Planner model: {plan['planner_model']}",
        f"Failure fingerprint: {plan.get('failure_fingerprint') or 'none'}",
        f"Root cause: {data.get('root_cause') or data.get('summary') or 'Not specified'}",
    ]

    if data.get("blocked_by_environment"):
        lines.append(
            "Environment blocker: "
            + (data.get("environment_note") or "Dependency unavailable in sandbox.")
        )

    repairs = list(data.get("repair_sequence") or [])
    if repairs:
        lines.append("Repair sequence:")
        for index, item in enumerate(repairs, start=1):
            lines.append(
                f"{index}. {item.get('file')}: {item.get('objective') or item.get('reason')}"
            )
    else:
        lines.append("Repair sequence: no code rewrite recommended by planner.")

    if data.get("verification_target"):
        lines.append(
            "Verification target: "
            + str(data["verification_target"])
        )

    return "\n".join(lines)[:6500]


def get_next_project_repair(user_id, run_id):
    plan = get_active_debug_plan(user_id, run_id)
    if not plan:
        return None

    repairs = list(plan["plan"].get("repair_sequence") or [])
    index = int(plan.get("next_repair_index") or 0)

    if index >= len(repairs):
        return None

    return {
        "plan": plan,
        "repair": repairs[index],
        "repair_index": index,
    }


def active_plan_blocks_on_environment(user_id, run_id):
    plan = get_active_debug_plan(user_id, run_id)
    return bool(
        plan
        and plan["plan"].get("blocked_by_environment")
        and not list(plan["plan"].get("repair_sequence") or [])
    )


def project_planner_context(run, analysis=None):
    analysis = analysis or analyze_project_state(run)
    active = get_active_debug_plan(
        run["user_id"],
        run["id"],
    )

    text = _contract_summary(analysis)

    if active:
        text += (
            "\n\nACTIVE DEBUG PLAN:\n"
            + format_debug_plan(active)
            + f"\nNext repair index: {active['next_repair_index']}"
        )

    return text[:18000]


def _repair_prompt_context(run, analysis, plan, repair):
    target = repair["file"]
    current = read_workspace_file(
        run["user_id"],
        run["id"],
        target,
        max_chars=PROJECT_FILE_BUDGET,
    )

    return (
        "USER GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nUSER REVISION/INPUT HISTORY:\n"
        + base_runner._inputs_text(run)
        + "\n\nPROJECT CONTRACT:\n"
        + _contract_summary(analysis)
        + "\n\nACTIVE DEBUG PLAN:\n"
        + json.dumps(plan["plan"], ensure_ascii=False, indent=2)[:10000]
        + "\n\nTARGET REPAIR:\n"
        + json.dumps(repair, ensure_ascii=False, indent=2)
        + "\n\nCURRENT TARGET FILE:\n--- "
        + target
        + " ---\n"
        + str(current or "")
        + "\n\nRELATED CURRENT WORKSPACE:\n"
        + _workspace_source_bundle(run, analysis)
    )


def execute_project_repair(run):
    next_item = get_next_project_repair(
        run["user_id"],
        run["id"],
    )

    if not next_item:
        raise AgentProjectPlannerError(
            "The active project plan has no remaining repair step."
        )

    plan = next_item["plan"]
    repair = next_item["repair"]
    target = str(repair.get("file") or "").strip()

    analysis = analyze_project_state(run)
    existing = {
        item["filename"]
        for item in analysis["contract"]["files"]
    }

    if target not in existing:
        _advance_plan(plan["id"], run["user_id"], exhausted=False)
        raise AgentProjectPlannerError(
            f"Planned repair target no longer exists: {target}"
        )

    system_prompt = (
        "You are the implementation specialist executing ONE approved repair from a "
        "persistent project debug plan. Rewrite exactly the TARGET FILE and no other file. "
        "Return the COMPLETE file content, not a patch. Preserve the user-requested "
        "architecture. Reconcile the target with the current imports, APIs, callers and tests "
        "shown in the project contract. Do not weaken/delete tests merely to get green. Do "
        "not remove a requested third-party framework merely because the sandbox lacks it. "
        "Make the smallest coherent change that satisfies this repair objective.\n\n"
        "Return ONLY JSON with keys: filename, content, summary."
    )

    raw, actual_model = _run_with_fallback(
        run,
        system_prompt,
        _repair_prompt_context(
            run,
            analysis,
            plan,
            repair,
        ),
        plan["planner_model"],
    )

    data = base_runner._safe_json_object(
        raw,
        "project repair specialist",
    )

    returned_filename = str(data.get("filename") or "").strip()
    if returned_filename and returned_filename != target:
        raise AgentProjectPlannerError(
            f"Repair specialist attempted to change {returned_filename}; expected {target}."
        )

    content = data.get("content")
    if content is None:
        raise AgentProjectPlannerError(
            "Repair specialist returned no complete file content."
        )

    previous = read_workspace_file(
        run["user_id"],
        run["id"],
        target,
        max_chars=256000,
    )

    if str(previous) == str(content):
        _advance_plan(
            plan["id"],
            run["user_id"],
            exhausted=False,
        )
        return (
            f"Planner-guided repair inspected {target} using {actual_model}, but the "
            "proposed content was unchanged. Advanced to the next planned repair."
        )

    result = write_workspace_file(
        run["user_id"],
        run["id"],
        target,
        str(content),
    )

    _advance_plan(
        plan["id"],
        run["user_id"],
        exhausted=False,
    )

    return (
        f"Planner-guided repair updated {result['filename']} ({result['size_bytes']} bytes).\n"
        f"Planner model: {actual_model}\n"
        f"Objective: {repair.get('objective') or repair.get('reason') or 'Repair project contract'}\n"
        f"Summary: {str(data.get('summary') or '').strip()}\n"
        "The deterministic execution loop will re-test the current workspace revision before "
        "another speculative repair."
    )[:6500]


def _advance_plan(plan_id, user_id, exhausted=False):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET next_repair_index = next_repair_index + 1
        WHERE id = ? AND user_id = ? AND status = 'active'
        """,
        (
            int(plan_id),
            int(user_id),
        ),
    )

    if exhausted:
        cursor.execute(
            """
            UPDATE agent_debug_plans
            SET status = 'exhausted', completed_at = ?
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (
                utc_iso(),
                int(plan_id),
                int(user_id),
            ),
        )

    conn.commit()
    conn.close()


def mark_active_plan_resolved(user_id, run_id):
    initialize_agent_project_planner_storage()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET status = 'resolved', completed_at = ?
        WHERE run_id = ? AND user_id = ? AND status = 'active'
        """,
        (
            utc_iso(),
            str(run_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()


def mark_active_plan_exhausted(user_id, run_id):
    initialize_agent_project_planner_storage()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET status = 'exhausted', completed_at = ?
        WHERE run_id = ? AND user_id = ? AND status = 'active'
        """,
        (
            utc_iso(),
            str(run_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()
