"""
ATLAS v2.4.1 - evidence-driven transactional Node/JavaScript execution.

This layer keeps v2.4's bounded multi-file transaction architecture, but adds
three controls that the v2.4 acceptance run proved were still missing:

1. deterministic extraction of test evidence and measurable progress;
2. a small high-confidence verifier-repair lane before another model call;
3. hypothesis memory so the same unchanged failure is not attacked with the
   same repair idea again.

It intentionally composes the v2.4 transaction primitives instead of creating a
second sandbox/runtime implementation.
"""

import hashlib
import json
import os
import re
import time

from app.config import DEFAULT_MODEL, DEEP_MODEL
from app.database import get_connection
from app.services import agent_runner as base_runner
from app.services.agents import get_agent_run
from app.services.agent_acceptance_contract import acceptance_summary
from app.services.agent_revision import latest_open_revision
from app.services.agent_sandbox import (
    format_execution_observation,
    read_workspace_file,
    run_node_sandbox,
    run_npm_script_sandbox,
    write_workspace_file,
)
from app.services import agent_node_project_planner as node_planner
from app.services import agent_node_transaction as tx
from app.services.agent_node_recovery import (
    evidence_summary,
    extract_execution_evidence,
    find_uncaptured_expected_throw,
    progress_between,
)


# The M1 Pro / 16 GB development machine can legitimately need more than three
# minutes for a local 8B/14B structured generation.  These are still hard
# wall-clock ceilings, not idle timeouts, and remain environment-configurable.
WORKER_TOTAL_TIMEOUT_SECONDS = max(
    60,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_WORKER_TOTAL_TIMEOUT_SECONDS", "300")),
)
REASONING_TOTAL_TIMEOUT_SECONDS = max(
    120,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_REASONING_TOTAL_TIMEOUT_SECONDS", "600")),
)
WORKER_RETRY_TIMEOUT_SECONDS = max(
    60,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_WORKER_RETRY_TIMEOUT_SECONDS", "240")),
)
REASONING_RETRY_TIMEOUT_SECONDS = max(
    120,
    int(os.environ.get("PRIVATE_AI_AGENT_TX_REASONING_RETRY_TIMEOUT_SECONDS", "420")),
)


class AgentNodeRecoveryError(tx.AgentNodeTransactionError):
    pass


def _revision_number(run):
    try:
        revision = latest_open_revision(run["user_id"], run["id"])
    except Exception:
        revision = None
    return int((revision or {}).get("revision_number") or 0)


def _history(run, limit=12):
    """Read current-revision transaction history without adding another schema."""
    tx.initialize_node_transaction_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT cycle_number, baseline_fingerprint, model_tier, model_name,
               changed_files_json, verification_json, acceptance_json, status
        FROM agent_project_transactions
        WHERE run_id = ? AND user_id = ? AND revision_number = ?
        ORDER BY cycle_number ASC
        LIMIT ?
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            _revision_number(run),
            max(1, min(50, int(limit))),
        ),
    )
    rows = cursor.fetchall()
    conn.close()
    result = []
    for row in rows:
        try:
            changed = json.loads(row[4] or "[]")
        except Exception:
            changed = []
        try:
            verification = json.loads(row[5] or "{}")
        except Exception:
            verification = {}
        try:
            acceptance = json.loads(row[6] or "{}")
        except Exception:
            acceptance = {}
        result.append(
            {
                "cycle_number": int(row[0] or 0),
                "baseline_fingerprint": row[1],
                "model_tier": row[2],
                "model_name": row[3],
                "changed_files": changed if isinstance(changed, list) else [],
                "verification": verification if isinstance(verification, dict) else {},
                "acceptance": acceptance if isinstance(acceptance, dict) else {},
                "status": row[7],
            }
        )
    return result


def _history_progress(item):
    changed = item.get("changed_files") or []
    for entry in changed:
        if isinstance(entry, dict) and entry.get("_progress_classification"):
            return {
                "classification": entry.get("_progress_classification"),
                "score": entry.get("_progress_score"),
                "reason": entry.get("_progress_reason"),
            }
    return {}


def _attempted_hypotheses(history, baseline_fingerprint=None):
    items = []
    for record in history:
        if baseline_fingerprint and str(record.get("baseline_fingerprint") or "") != str(baseline_fingerprint):
            continue
        for entry in record.get("changed_files") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("_hypothesis_key") or "").strip()
            text = str(entry.get("_hypothesis") or "").strip()
            if key and not any(item["key"] == key for item in items):
                items.append({"key": key, "text": text})
                break
    return items


def _model_choice(run, status, history):
    mode = str(run.get("model_mode") or "auto").strip().lower()
    if mode != "auto":
        selected_mode, selected_model = base_runner._select_agent_model(run)
        return {
            "tier": f"manual_{selected_mode}",
            "model": selected_model,
            "timeout": REASONING_TOTAL_TIMEOUT_SECONDS,
        }

    if not history:
        return {
            "tier": "worker",
            "model": DEFAULT_MODEL,
            "timeout": WORKER_TOTAL_TIMEOUT_SECONDS,
        }

    previous = history[-1]
    progress = _history_progress(previous)
    classification = str(progress.get("classification") or "")

    # Keep inexpensive work on the worker while it is making measurable
    # progress. Escalate only on stall/regression or the final allowed cycle.
    if (
        classification in {"strong_progress", "changed_failure", "verified_execution"}
        and int(status.get("remaining") or 0) > 1
    ):
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


def _hypothesis_key(baseline_fingerprint, hypothesis, staged):
    normalized = re.sub(r"\s+", " ", str(hypothesis or "").strip().lower())[:2000]
    filenames = ",".join(sorted(str(item.get("filename") or "") for item in staged))
    raw = "|".join([str(baseline_fingerprint or ""), normalized, filenames])
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:18]


def _reject_repeated_hypothesis(history, baseline_fingerprint, hypothesis_key):
    for item in reversed(history):
        if str(item.get("baseline_fingerprint") or "") != str(baseline_fingerprint or ""):
            continue
        for changed in item.get("changed_files") or []:
            if not isinstance(changed, dict):
                continue
            if str(changed.get("_hypothesis_key") or "") == str(hypothesis_key):
                raise AgentNodeRecoveryError(
                    "This exact repair hypothesis was already applied to the same unchanged failure. "
                    "Inspect the evidence and choose a different explanation/change-set instead of repeating it."
                )


def _deterministic_repair(run, analysis, evidence):
    """Return one high-confidence test-mechanics repair, or None."""
    for filename in analysis.get("contract", {}).get("test_files") or []:
        try:
            source = read_workspace_file(
                run["user_id"],
                run["id"],
                filename,
                max_chars=256000,
            )
        except Exception:
            continue
        repair = find_uncaptured_expected_throw(source, evidence)
        if not repair:
            continue
        return {
            "diagnosis": (
                "The failing test throws the same expected exception before assert.throws() "
                "has a chance to capture it. This is a verifier-mechanics defect, not a request to weaken coverage."
            ),
            "hypothesis": "uncaptured expected exception in test harness",
            "changes": [
                {
                    "filename": filename,
                    "content": repair["content"],
                    "reason": repair["reason"],
                }
            ],
            "verification_note": "Re-run the existing authoritative Node test command once.",
            "deterministic": True,
        }
    return None


def _verification(run, analysis):
    target = tx._preferred_verification(run, analysis)
    step_id = tx._current_running_step_id(run)
    if target.get("kind") == "npm_script":
        return run_npm_script_sandbox(
            run["user_id"],
            run["id"],
            target["script"],
            step_id=step_id,
            cancel_check=lambda: base_runner._control_probe(run),
        )
    if target.get("kind") == "node_file":
        return run_node_sandbox(
            run["user_id"],
            run["id"],
            target["filename"],
            step_id=step_id,
            cancel_check=lambda: base_runner._control_probe(run),
        )
    raise AgentNodeRecoveryError(
        "The transaction could not determine an authoritative Node verification target."
    )


def _augment_prompt(run, analysis, status, history, evidence):
    system_prompt, user_prompt = tx._transaction_prompt(run, analysis, status)
    attempted = _attempted_hypotheses(history, analysis.get("planning_fingerprint"))
    previous_progress = _history_progress(history[-1]) if history else {}

    system_prompt += (
        "\n\nEVIDENCE-DRIVEN RECOVERY RULES:\n"
        "Treat failed-test count, pass count, exact failing subtest, error, location and stack as primary evidence. "
        "A changed error is not automatically success. If the same failure/hypothesis was already attempted, choose a genuinely different explanation. "
        "Do not rewrite the same file with the same idea simply because another transaction is available. "
        "When the implementation now throws exactly the error a test intends to assert, inspect whether the test harness is letting that error escape before its assertion."
    )
    user_prompt += (
        "\n\nSTRUCTURED LATEST EXECUTION EVIDENCE:\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)[:7000]
        + "\n\nPREVIOUS TRANSACTION PROGRESS:\n"
        + (json.dumps(previous_progress, ensure_ascii=False, indent=2) if previous_progress else "none in this revision")
        + "\n\nALREADY-ATTEMPTED HYPOTHESES FOR THIS FAILURE:\n"
        + (
            "\n".join("- " + (item.get("text") or item["key"]) for item in attempted)
            if attempted
            else "none"
        )
        + "\n\nReturn an additional top-level JSON key `hypothesis` describing the causal explanation behind the proposed change-set."
    )
    return system_prompt, user_prompt


def execute_node_transaction_cycle(run):
    tx.initialize_node_transaction_storage()
    run = get_agent_run(run["user_id"], run["id"]) or run
    status = tx.transaction_status(run)
    if status["remaining"] <= 0:
        raise AgentNodeRecoveryError(
            "The bounded project transaction budget is exhausted for this revision."
        )

    analysis = node_planner.analyze_node_project_state(run)
    latest_execution = analysis.get("execution", {}).get("latest")
    baseline_evidence = extract_execution_evidence(latest_execution)
    baseline_fingerprint = analysis.get("planning_fingerprint") or baseline_evidence.get("fingerprint")
    history = _history(run)

    deterministic = _deterministic_repair(run, analysis, baseline_evidence)
    if deterministic:
        model_choice = {"tier": "deterministic_evidence", "model": "deterministic", "timeout": 0}
    else:
        model_choice = _model_choice(run, status, history)

    tx_id = tx._begin_transaction(run, status, baseline_fingerprint, model_choice)
    started = time.monotonic()

    try:
        if deterministic:
            data = deterministic
            staged = tx._validate_transaction_result(run, analysis, data)
        else:
            system_prompt, user_prompt = _augment_prompt(
                run, analysis, status, history, baseline_evidence
            )
            raw = tx._bounded_model_call(
                run,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model_choice["model"],
                purpose="node_project_transaction_v241",
                total_timeout_seconds=model_choice["timeout"],
            )
            try:
                data = tx._parse_transaction_result(raw)
                staged = tx._validate_transaction_result(run, analysis, data)
                hypothesis = str(data.get("hypothesis") or data.get("diagnosis") or "").strip()
                key = _hypothesis_key(baseline_fingerprint, hypothesis, staged)
                _reject_repeated_hypothesis(history, baseline_fingerprint, key)
            except tx.AgentNodeTransactionError as first_error:
                retry_model = (
                    DEEP_MODEL if model_choice["tier"] == "worker" else model_choice["model"]
                )
                retry_timeout = (
                    REASONING_RETRY_TIMEOUT_SECONDS
                    if retry_model == DEEP_MODEL
                    else WORKER_RETRY_TIMEOUT_SECONDS
                )
                retry_prompt = (
                    user_prompt
                    + "\n\nBOUNDED STRUCTURED RETRY:\nThe candidate was rejected before workspace mutation: "
                    + str(first_error)
                    + "\nReturn a corrected transaction with a different hypothesis when the previous hypothesis was already attempted."
                )
                raw = tx._bounded_model_call(
                    run,
                    system_prompt=system_prompt,
                    user_prompt=retry_prompt,
                    model=retry_model,
                    purpose="node_project_transaction_v241_retry",
                    total_timeout_seconds=retry_timeout,
                )
                data = tx._parse_transaction_result(raw)
                staged = tx._validate_transaction_result(run, analysis, data)
                hypothesis = str(data.get("hypothesis") or data.get("diagnosis") or "").strip()
                key = _hypothesis_key(baseline_fingerprint, hypothesis, staged)
                _reject_repeated_hypothesis(history, baseline_fingerprint, key)
                model_choice = {
                    **model_choice,
                    "model": retry_model,
                    "tier": (
                        "reasoning_fallback"
                        if retry_model == DEEP_MODEL and model_choice["tier"] == "worker"
                        else model_choice["tier"]
                    ),
                }

        hypothesis = str(data.get("hypothesis") or data.get("diagnosis") or "").strip()
        hypothesis_key = _hypothesis_key(baseline_fingerprint, hypothesis, staged)

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
                    "_hypothesis": hypothesis[:2000],
                    "_hypothesis_key": hypothesis_key,
                }
            )

        post_write_analysis = node_planner.analyze_node_project_state(run)
        execution = _verification(run, post_write_analysis)
        final_analysis = node_planner.analyze_node_project_state(run)
        acceptance = final_analysis.get("acceptance") or {}
        final_evidence = extract_execution_evidence(execution)
        progress = progress_between(
            baseline_evidence,
            final_evidence,
            analysis.get("acceptance") or {},
            acceptance,
        )

        for item in changed:
            item["_progress_classification"] = progress["classification"]
            item["_progress_score"] = progress["score"]
            item["_progress_reason"] = progress["reason"]
            item["_evidence_fingerprint"] = final_evidence.get("fingerprint")

        execution_ok = bool(
            str(execution.get("status") or "") == "success"
            and int(execution.get("exit_code") or 0) == 0
        )
        verified = bool(execution_ok and acceptance.get("satisfied"))

        tx._finish_transaction(
            tx_id,
            status="verified" if verified else "failed",
            changed_files=changed,
            verification=execution,
            acceptance=acceptance,
            model_duration_ms=int((time.monotonic() - started) * 1000),
        )

        lines = [
            f"Evidence-driven project transaction {status['next_cycle']}/{status['limit']} completed.",
            f"Execution lane: {model_choice['tier']}",
            f"Model: {model_choice['model']}",
            "Changed files: " + ", ".join(item["filename"] for item in changed),
            f"Hypothesis: {hypothesis or 'deterministic evidence repair'}",
            f"Progress: {progress['classification']} (score {progress['score']}) — {progress['reason']}",
            "Evidence: " + evidence_summary(final_evidence),
            "",
            format_execution_observation(execution),
            "",
            acceptance_summary(acceptance),
        ]
        if verified:
            lines.append("\nTRANSACTION VERIFIED — sandbox validation and goal acceptance both passed.")
        else:
            remaining = max(0, status["remaining"] - 1)
            if progress["classification"] == "stalled":
                lines.append(
                    "\nNO MEASURABLE PROGRESS — the next transaction must use a different hypothesis; identical recovery strategies are blocked."
                )
            lines.append(
                "\nThe transaction did not yet satisfy both verification gates. "
                f"{remaining} bounded project transaction(s) remain for this revision."
            )
        return "\n".join(lines)[:14000]

    except tx.AgentNodeTransactionTimeout as error:
        tx._finish_transaction(
            tx_id,
            status="timeout",
            error=str(error),
            model_duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    except Exception as error:
        tx._finish_transaction(
            tx_id,
            status="error",
            error=str(error),
            model_duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise


transaction_status = tx.transaction_status
transaction_budget_exhausted = tx.transaction_budget_exhausted
AgentNodeTransactionError = tx.AgentNodeTransactionError
