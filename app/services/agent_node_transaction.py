"""
ATLAS v2.4.0 - Bounded transactional Node/JavaScript engineering cycle.

The v2.3.x Node planner proved the value of explicit project intelligence, but
its one-file-at-a-time repair loop created excessive plan -> mutate -> retest ->
replan churn.  v2.4 changes the unit of work from one file to one coherent
project transaction:

    analyze once
      -> stage a bounded multi-file change-set
      -> validate all candidates before mutation
      -> commit the change-set
      -> verify once
      -> evaluate goal acceptance

A transaction is also a resource-governance boundary.  Model calls have a hard
wall-clock deadline and each revision gets only a small number of project
transactions before ATLAS stops with a truthful blocker.
"""

import json
import os
import time

import requests

from app.config import (
    AGENT_MAX_RUNTIME_SECONDS,
    DEFAULT_MODEL,
    DEEP_MODEL,
    OLLAMA_CHAT_URL,
)
from app.database import get_connection
from app.services import agent_runner as base_runner
from app.services.agents import (
    get_agent_run,
    list_agent_steps,
    utc_iso,
)
from app.services.agent_acceptance_contract import (
    acceptance_summary,
    get_or_create_acceptance_contract,
)
from app.services.agent_revision import latest_open_revision
from app.services.agent_sandbox import (
    format_execution_observation,
    list_npm_scripts,
    list_workspace_files,
    read_workspace_file,
    run_node_sandbox,
    run_npm_script_sandbox,
    write_workspace_file,
)
from app.services import agent_node_project_planner as node_planner


TRANSACTION_MAX_PER_REVISION = max(
    1,
    int(os.environ.get("PRIVATE_AI_AGENT_PROJECT_TRANSACTIONS_PER_REVISION", "3")),
)
TRANSACTION_MAX_FILES = max(
    1,
    min(10, int(os.environ.get("PRIVATE_AI_AGENT_TRANSACTION_MAX_FILES", "6"))),
)
MODEL_CONNECT_TIMEOUT_SECONDS = max(
    3,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_MODEL_CONNECT_TIMEOUT_SECONDS", "10")),
)
MODEL_IDLE_TIMEOUT_SECONDS = max(
    20,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_MODEL_IDLE_TIMEOUT_SECONDS", "180")),
)
WORKER_TOTAL_TIMEOUT_SECONDS = max(
    30,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_WORKER_TOTAL_TIMEOUT_SECONDS", "300")),
)
REASONING_TOTAL_TIMEOUT_SECONDS = max(
    60,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_REASONING_TOTAL_TIMEOUT_SECONDS", "600")),
)
TRANSACTION_CONTEXT_SIZE = max(
    4096,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_CONTEXT_SIZE", "8192")),
)
TRANSACTION_SOURCE_BUDGET = max(
    8000,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_SOURCE_BUDGET", "26000")),
)
TRANSACTION_FILE_BUDGET = max(
    3000,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_FILE_BUDGET", "8000")),
)

_STORAGE_READY = False


class AgentNodeTransactionError(Exception):
    pass


class AgentNodeTransactionTimeout(AgentNodeTransactionError):
    pass


def initialize_node_transaction_storage():
    global _STORAGE_READY
    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_project_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL DEFAULT 0,
            cycle_number INTEGER NOT NULL,
            start_step INTEGER NOT NULL,
            baseline_fingerprint TEXT,
            model_tier TEXT,
            model_name TEXT,
            model_duration_ms INTEGER,
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            verification_json TEXT NOT NULL DEFAULT '{}',
            acceptance_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
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
        CREATE INDEX IF NOT EXISTS idx_agent_project_transactions_run
        ON agent_project_transactions(run_id, revision_number, id)
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_model_call_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            input_chars INTEGER NOT NULL DEFAULT 0,
            output_chars INTEGER NOT NULL DEFAULT 0,
            total_timeout_seconds INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_model_call_metrics_run
        ON agent_model_call_metrics(run_id, id)
        """
    )
    conn.commit()
    conn.close()
    _STORAGE_READY = True


def _revision_number(run):
    try:
        revision = latest_open_revision(
            run["user_id"],
            run["id"],
        )
    except Exception:
        revision = None
    return int((revision or {}).get("revision_number") or 0)


def transaction_status(run):
    initialize_node_transaction_storage()
    revision_number = _revision_number(run)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*), COALESCE(MAX(cycle_number), 0)
        FROM agent_project_transactions
        WHERE run_id = ? AND user_id = ? AND revision_number = ?
        """,
        (str(run["id"]), int(run["user_id"]), revision_number),
    )
    row = cursor.fetchone() or (0, 0)
    conn.close()
    used = int(row[0] or 0)
    return {
        "revision_number": revision_number,
        "used": used,
        "remaining": max(0, TRANSACTION_MAX_PER_REVISION - used),
        "limit": TRANSACTION_MAX_PER_REVISION,
        "next_cycle": int(row[1] or 0) + 1,
    }


def transaction_budget_exhausted(run):
    return transaction_status(run)["remaining"] <= 0


def _record_metric(
    run,
    *,
    purpose,
    model,
    status,
    duration_ms,
    input_chars,
    output_chars,
    total_timeout_seconds,
    error=None,
):
    try:
        initialize_node_transaction_storage()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_model_call_metrics (
                run_id, user_id, purpose, model, status,
                duration_ms, input_chars, output_chars,
                total_timeout_seconds, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run["id"]),
                int(run["user_id"]),
                str(purpose)[:120],
                str(model)[:255],
                str(status)[:60],
                int(duration_ms),
                int(input_chars),
                int(output_chars),
                int(total_timeout_seconds),
                None if error is None else str(error)[:3000],
                utc_iso(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _bounded_model_call(
    run,
    *,
    system_prompt,
    user_prompt,
    model,
    purpose,
    total_timeout_seconds,
):
    """Run one local model call with a real wall-clock generation deadline."""
    started = time.monotonic()
    parts = []
    status = "error"
    error_text = None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "format": "json",
        "keep_alive": getattr(base_runner, "AGENT_MODEL_KEEP_ALIVE", "10m"),
        "options": {"num_ctx": TRANSACTION_CONTEXT_SIZE},
    }

    try:
        base_runner._control_probe(run, force=True)
        with requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            stream=True,
            timeout=(
                MODEL_CONNECT_TIMEOUT_SECONDS,
                min(MODEL_IDLE_TIMEOUT_SECONDS, total_timeout_seconds),
            ),
        ) as response:
            if not response.ok:
                detail = ""
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        detail = str(body.get("error") or "").strip()
                except Exception:
                    detail = str(response.text or "").strip()[:500]
                raise AgentNodeTransactionError(
                    "Local transaction model failed: Ollama HTTP "
                    + str(response.status_code)
                    + (f": {detail}" if detail else "")
                )

            for line in response.iter_lines():
                base_runner._control_probe(run)
                elapsed = time.monotonic() - started
                if elapsed >= total_timeout_seconds:
                    raise AgentNodeTransactionTimeout(
                        f"Project transaction model exceeded its {total_timeout_seconds}s wall-clock limit."
                    )
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AgentNodeTransactionError(
                        "The project transaction model returned invalid streaming JSON."
                    ) from exc
                message = data.get("message") or {}
                chunk = str(message.get("content") or "")
                if chunk:
                    parts.append(chunk)
                if data.get("done"):
                    break

        raw = "".join(parts).strip()
        if not raw:
            raise AgentNodeTransactionError(
                "The project transaction model returned no structured content."
            )
        status = "success"
        return raw

    except requests.exceptions.ReadTimeout as exc:
        error_text = (
            "Project transaction model stopped producing stream data before completion."
        )
        raise AgentNodeTransactionTimeout(error_text) from exc
    except Exception as exc:
        error_text = str(exc)
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        _record_metric(
            run,
            purpose=purpose,
            model=model,
            status=status,
            duration_ms=duration_ms,
            input_chars=len(system_prompt) + len(user_prompt),
            output_chars=sum(len(part) for part in parts),
            total_timeout_seconds=total_timeout_seconds,
            error=error_text,
        )


def _workspace_bundle(run, analysis):
    priority = []

    def add(name):
        name = str(name or "").strip()
        if name and name not in priority:
            priority.append(name)

    failure = analysis.get("execution", {}).get("failure") or {}
    add(failure.get("filename"))
    for name in analysis.get("contract", {}).get("test_files") or []:
        add(name)
    add("package.json")
    for item in analysis.get("contract", {}).get("files") or []:
        add(item.get("filename"))

    used = 0
    blocks = []
    available = {
        str(item.get("filename") or "")
        for item in list_workspace_files(run["user_id"], run["id"])
    }
    for name in priority:
        if name not in available or used >= TRANSACTION_SOURCE_BUDGET:
            continue
        try:
            content = read_workspace_file(
                run["user_id"],
                run["id"],
                name,
                max_chars=TRANSACTION_FILE_BUDGET,
            )
        except Exception:
            continue
        remaining = TRANSACTION_SOURCE_BUDGET - used
        block = f"--- {name} ---\n" + str(content or "")[:remaining]
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _test_issue_allows_edit(analysis, filename):
    try:
        return bool(node_planner._test_repair_allowed(filename, analysis))
    except Exception:
        return False


def _allowed_targets(run, analysis):
    existing = {
        str(item.get("filename") or "")
        for item in list_workspace_files(run["user_id"], run["id"])
    }
    contract = get_or_create_acceptance_contract(run)
    missing_required = {
        str(name)
        for name in contract.get("required_files") or []
        if str(name) not in existing
    }
    allowed = set(existing) | missing_required
    return {
        name
        for name in allowed
        if name == "package.json"
        or str(name).lower().endswith(
            (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json")
        )
    }


def _candidate_error(run, analysis, filename, previous, content):
    try:
        return node_planner._candidate_contract_error(
            run,
            analysis,
            filename,
            previous,
            content,
        )
    except Exception as error:
        return f"Candidate integrity check failed internally: {error}"


def _model_choice(run, tx_status):
    mode = str(run.get("model_mode") or "auto").strip().lower()
    if mode != "auto":
        selected_mode, selected_model = base_runner._select_agent_model(run)
        return {
            "tier": f"manual_{selected_mode}",
            "model": selected_model,
            "timeout": REASONING_TOTAL_TIMEOUT_SECONDS,
        }

    # First transaction is deliberately worker-first, even for a long historical
    # run.  Old repair churn must not permanently force every new plan onto the
    # slow reasoning model.  Escalation is local to this bounded transaction era.
    if int(tx_status.get("used") or 0) <= 0:
        return {
            "tier": "worker",
            "model": DEFAULT_MODEL,
            "timeout": WORKER_TOTAL_TIMEOUT_SECONDS,
        }

    return {
        "tier": "reasoning",
        "model": DEEP_MODEL,
        "timeout": REASONING_TOTAL_TIMEOUT_SECONDS,
    }


def _current_running_step_id(run):
    try:
        steps = list_agent_steps(run["user_id"], run["id"])
    except Exception:
        return None
    for step in reversed(steps):
        if str(step.get("status") or "") == "running":
            return step.get("id")
    return None


def _preferred_verification(run, analysis):
    scripts = []
    try:
        scripts = list_npm_scripts(run["user_id"], run["id"])
    except Exception:
        scripts = []
    for name in ("test", "check", "lint", "typecheck", "build"):
        if name in scripts:
            return {"kind": "npm_script", "script": name}

    tests = analysis.get("contract", {}).get("test_files") or []
    if tests:
        return {"kind": "node_file", "filename": tests[0]}

    source = analysis.get("contract", {}).get("source_files") or []
    if source:
        return {"kind": "node_file", "filename": source[0]}
    return {}


def _begin_transaction(run, status, baseline_fingerprint, model_choice):
    initialize_node_transaction_storage()
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_project_transactions (
            run_id, user_id, revision_number, cycle_number,
            start_step, baseline_fingerprint, model_tier, model_name,
            model_duration_ms, changed_files_json, verification_json,
            acceptance_json, status, error, created_at, finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', '{}', '{}', 'running', NULL, ?, NULL)
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            int(status["revision_number"]),
            int(status["next_cycle"]),
            int(run.get("current_step") or 0),
            baseline_fingerprint,
            model_choice["tier"],
            model_choice["model"],
            timestamp,
        ),
    )
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return tx_id


def _finish_transaction(
    tx_id,
    *,
    status,
    changed_files=None,
    verification=None,
    acceptance=None,
    error=None,
    model_duration_ms=None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_project_transactions
        SET status = ?, changed_files_json = ?, verification_json = ?,
            acceptance_json = ?, error = ?, model_duration_ms = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            str(status),
            json.dumps(changed_files or [], ensure_ascii=False),
            json.dumps(verification or {}, ensure_ascii=False, default=str),
            json.dumps(acceptance or {}, ensure_ascii=False, default=str),
            None if error is None else str(error)[:4000],
            model_duration_ms,
            utc_iso(),
            int(tx_id),
        ),
    )
    conn.commit()
    conn.close()


def _parse_transaction_result(raw):
    data = base_runner._safe_json_object(raw, "bounded project transaction")
    changes = data.get("changes")
    if not isinstance(changes, list):
        raise AgentNodeTransactionError("Project transaction response must include a changes array.")
    return data


def _validate_transaction_result(run, analysis, data):
    allowed = _allowed_targets(run, analysis)
    existing = {
        str(item.get("filename") or "")
        for item in list_workspace_files(run["user_id"], run["id"])
    }
    seen = set()
    staged = []

    for item in list(data.get("changes") or [])[:TRANSACTION_MAX_FILES]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        content = item.get("content")
        if not filename or filename in seen:
            continue
        if filename not in allowed:
            raise AgentNodeTransactionError(
                f"Transaction attempted an unapproved file target: {filename}"
            )
        if not isinstance(content, str):
            raise AgentNodeTransactionError(
                f"Transaction content for {filename} must be a complete file string."
            )
        if node_planner.is_test_file(filename, analysis.get("contract")) and not _test_issue_allows_edit(
            analysis, filename
        ):
            raise AgentNodeTransactionError(
                f"Transaction attempted to rewrite protected test specification without deterministic justification: {filename}"
            )
        previous = ""
        if filename in existing:
            previous = read_workspace_file(
                run["user_id"],
                run["id"],
                filename,
                max_chars=256000,
            )
        error = _candidate_error(run, analysis, filename, previous, content)
        if error:
            raise AgentNodeTransactionError(error)
        if str(previous) == str(content):
            continue
        seen.add(filename)
        staged.append(
            {
                "filename": filename,
                "content": content,
                "previous": previous,
                "created": filename not in existing,
                "reason": str(item.get("reason") or "").strip()[:1000],
            }
        )

    if not staged:
        raise AgentNodeTransactionError(
            "The bounded project transaction produced no validated workspace change."
        )
    return staged


def _transaction_prompt(run, analysis, tx_status):
    acceptance = analysis.get("acceptance") or {}
    allowed = sorted(_allowed_targets(run, analysis))
    test_editable = sorted(
        name
        for name in (analysis.get("contract", {}).get("test_files") or [])
        if _test_issue_allows_edit(analysis, name)
    )
    failure = analysis.get("execution", {}).get("failure") or {}

    system_prompt = (
        "You are the bounded transactional software engineer for ATLAS. Solve the CURRENT "
        "project state as one coherent change-set instead of repairing one file at a time. "
        "You may return multiple complete files in one response. Think across the user goal, "
        "acceptance requirements, imports/exports, callers, tests, package.json and the latest "
        "sandbox failure. Preserve existing public APIs unless the goal explicitly requires a "
        "change. Tests are protected specifications; only change a test file when it is listed "
        "as TEST-EDIT-ALLOWED below, and preserve/restore all required coverage. Do not remove "
        "requested dependencies. Prefer a complete coherent fix over partial edits that require "
        "another planning pass.\n\n"
        "Return ONLY one JSON object with keys: diagnosis (string), changes (array), verification_note (string). "
        "Each changes item must contain filename, content, reason. content MUST be the COMPLETE file. "
        f"Return at most {TRANSACTION_MAX_FILES} changed files. Do not use markdown fences."
    )

    user_prompt = (
        "ORIGINAL USER GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nGOAL ACCEPTANCE STATE:\n"
        + acceptance_summary(acceptance)
        + "\n\nDETERMINISTIC PROJECT CONTRACT:\n"
        + node_planner._contract_summary(analysis)
        + "\n\nLATEST FAILURE:\n"
        + json.dumps(
            {
                "type": failure.get("type"),
                "message": failure.get("message"),
                "location": failure.get("location"),
                "fingerprint": failure.get("fingerprint"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nALLOWED FILE TARGETS:\n"
        + "\n".join(f"- {name}" for name in allowed)
        + "\n\nTEST-EDIT-ALLOWED:\n"
        + ("\n".join(f"- {name}" for name in test_editable) if test_editable else "none")
        + "\n\nCURRENT WORKSPACE:\n"
        + (_workspace_bundle(run, analysis) or "No readable source files.")
        + "\n\nTRANSACTION GOVERNANCE:\n"
        + f"This is bounded project transaction {tx_status['next_cycle']} of {tx_status['limit']} for the current revision. "
        "The entire validated change-set will be committed together and sandbox verification will run once afterward."
    )
    return system_prompt, user_prompt


def execute_node_transaction_cycle(run):
    initialize_node_transaction_storage()
    run = get_agent_run(run["user_id"], run["id"]) or run
    status = transaction_status(run)
    if status["remaining"] <= 0:
        raise AgentNodeTransactionError(
            "The bounded project transaction budget is exhausted for this revision."
        )

    analysis = node_planner.analyze_node_project_state(run)
    baseline_fingerprint = analysis.get("planning_fingerprint")
    model_choice = _model_choice(run, status)
    tx_id = _begin_transaction(run, status, baseline_fingerprint, model_choice)
    model_started = time.monotonic()

    try:
        system_prompt, user_prompt = _transaction_prompt(run, analysis, status)

        # One primary call.  A second call is allowed only for fast structural
        # rejection (bad JSON / invalid candidate), never after a timeout.
        raw = _bounded_model_call(
            run,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model_choice["model"],
            purpose="node_project_transaction",
            total_timeout_seconds=model_choice["timeout"],
        )
        try:
            data = _parse_transaction_result(raw)
            staged = _validate_transaction_result(run, analysis, data)
        except AgentNodeTransactionError as first_error:
            # Keep structured recovery bounded.  Worker cycle may escalate once;
            # reasoning cycles get one concise retry with the same model.
            retry_model = (
                DEEP_MODEL
                if model_choice["tier"] == "worker"
                else model_choice["model"]
            )
            retry_timeout = (
                min(REASONING_TOTAL_TIMEOUT_SECONDS, 180)
                if retry_model == DEEP_MODEL
                else min(model_choice["timeout"], 150)
            )
            retry_prompt = (
                user_prompt
                + "\n\nSTRUCTURED TRANSACTION RETRY:\n"
                + "The previous candidate was rejected before any workspace mutation: "
                + str(first_error)
                + "\nReturn a corrected complete transaction. Do not repeat the rejected mistake."
            )
            raw = _bounded_model_call(
                run,
                system_prompt=system_prompt,
                user_prompt=retry_prompt,
                model=retry_model,
                purpose="node_project_transaction_retry",
                total_timeout_seconds=retry_timeout,
            )
            data = _parse_transaction_result(raw)
            staged = _validate_transaction_result(run, analysis, data)
            model_choice = {
                **model_choice,
                "model": retry_model,
                "tier": (
                    "reasoning_fallback"
                    if retry_model == DEEP_MODEL and model_choice["tier"] == "worker"
                    else model_choice["tier"]
                ),
            }

        changed = []
        for item in staged:
            result = write_workspace_file(
                run["user_id"],
                run["id"],
                item["filename"],
                item["content"],
            )
            changed.append(
                {
                    "filename": result["filename"],
                    "size_bytes": result["size_bytes"],
                    "created": bool(item["created"]),
                    "reason": item["reason"],
                }
            )

        # Verify exactly once after the coherent change-set is committed.
        post_write_analysis = node_planner.analyze_node_project_state(run)
        verification_target = _preferred_verification(run, post_write_analysis)
        step_id = _current_running_step_id(run)
        if verification_target.get("kind") == "npm_script":
            execution = run_npm_script_sandbox(
                run["user_id"],
                run["id"],
                verification_target["script"],
                step_id=step_id,
                cancel_check=lambda: base_runner._control_probe(run),
            )
        elif verification_target.get("kind") == "node_file":
            execution = run_node_sandbox(
                run["user_id"],
                run["id"],
                verification_target["filename"],
                step_id=step_id,
                cancel_check=lambda: base_runner._control_probe(run),
            )
        else:
            raise AgentNodeTransactionError(
                "The transaction could not determine an authoritative Node verification target."
            )

        final_analysis = node_planner.analyze_node_project_state(run)
        acceptance = final_analysis.get("acceptance") or {}
        execution_ok = bool(
            str(execution.get("status") or "") == "success"
            and int(execution.get("exit_code") or 0) == 0
        )
        verified = bool(execution_ok and acceptance.get("satisfied"))

        _finish_transaction(
            tx_id,
            status="verified" if verified else "failed",
            changed_files=changed,
            verification=execution,
            acceptance=acceptance,
            model_duration_ms=int((time.monotonic() - model_started) * 1000),
        )

        lines = [
            f"Bounded project transaction {status['next_cycle']}/{status['limit']} completed.",
            f"Model tier: {model_choice['tier']}",
            f"Model: {model_choice['model']}",
            "Changed files: " + ", ".join(item["filename"] for item in changed),
            "",
            format_execution_observation(execution),
            "",
            acceptance_summary(acceptance),
        ]
        if verified:
            lines.append("\nTRANSACTION VERIFIED — sandbox validation and goal acceptance both passed.")
        else:
            remaining = max(0, status["remaining"] - 1)
            lines.append(
                "\nThe transaction did not yet satisfy both verification gates. "
                f"{remaining} bounded project transaction(s) remain for this revision."
            )
        return "\n".join(lines)[:12000]

    except AgentNodeTransactionTimeout as error:
        _finish_transaction(
            tx_id,
            status="timeout",
            error=str(error),
            model_duration_ms=int((time.monotonic() - model_started) * 1000),
        )
        raise
    except Exception as error:
        _finish_transaction(
            tx_id,
            status="error",
            error=str(error),
            model_duration_ms=int((time.monotonic() - model_started) * 1000),
        )
        raise
