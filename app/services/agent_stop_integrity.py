"""
ATLAS v2.3.0d - control-plane stop integrity.

This module keeps *why an Agent execution cycle stopped* separate from the
workspace/project result itself.  The separation is intentional: future
multi-agent workers, schedulers, evaluators and resource-governance layers need
machine-readable termination reasons that are not inferred from prose.

It also provides a runtime-independent circuit breaker for repeated internal
ATLAS action failures.  Genuine project test failures are sandbox observations
with completed Agent steps and therefore do not trigger this guard.
"""

import hashlib
import json
import re
import threading
import time

from app.database import get_connection
from app.services.agents import (
    AgentStoreError,
    get_agent_run,
    list_agent_steps,
    request_agent_pause,
    utc_iso,
)


REPEATED_INTERNAL_FAILURE_THRESHOLD = 2
WATCHDOG_POLL_SECONDS = 0.5
_STORAGE_READY = False
_STORAGE_LOCK = threading.Lock()


def initialize_agent_stop_integrity_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    with _STORAGE_LOCK:
        if _STORAGE_READY:
            return

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_run_stop_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                step_index INTEGER,
                code TEXT NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_agent_run_stop_events_run
            ON agent_run_stop_events(run_id, id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_run_stop_events_active
            ON agent_run_stop_events(run_id, active, id)
            """
        )
        conn.commit()
        conn.close()
        _STORAGE_READY = True


def _json(value):
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _safe_json(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def clear_active_run_stop(user_id, run_id):
    initialize_agent_stop_integrity_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_run_stop_events
        SET active = 0
        WHERE run_id = ? AND user_id = ? AND active = 1
        """,
        (str(run_id), int(user_id)),
    )
    conn.commit()
    conn.close()


def record_run_stop(
    user_id,
    run_id,
    *,
    code,
    category,
    message,
    step_index=None,
    details=None,
    active=True,
):
    initialize_agent_stop_integrity_storage()
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()

    if active:
        cursor.execute(
            """
            UPDATE agent_run_stop_events
            SET active = 0
            WHERE run_id = ? AND user_id = ? AND active = 1
            """,
            (str(run_id), int(user_id)),
        )

    cursor.execute(
        """
        INSERT INTO agent_run_stop_events (
            run_id,
            user_id,
            step_index,
            code,
            category,
            message,
            details_json,
            active,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run_id),
            int(user_id),
            None if step_index is None else int(step_index),
            str(code)[:120],
            str(category)[:120],
            str(message)[:4000],
            _json(details or {}),
            1 if active else 0,
            timestamp,
        ),
    )
    event_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return event_id


def _event_from_row(row):
    if not row:
        return None
    return {
        "id": int(row[0]),
        "step_index": row[1],
        "code": row[2],
        "category": row[3],
        "message": row[4],
        "details": _safe_json(row[5]),
        "active": bool(row[6]),
        "created_at": row[7],
    }


def agent_stop_status(user_id, run_id, history_limit=10):
    initialize_agent_stop_integrity_storage()
    limit = max(1, min(50, int(history_limit)))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            id, step_index, code, category, message,
            details_json, active, created_at
        FROM agent_run_stop_events
        WHERE run_id = ? AND user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(run_id), int(user_id), limit),
    )
    rows = cursor.fetchall()
    conn.close()
    history = [_event_from_row(row) for row in rows]
    active = next((item for item in history if item.get("active")), None)
    return {
        "active_stop": active,
        "latest_stop": history[0] if history else None,
        "history": history,
    }


def _normalize_internal_error(text):
    value = " ".join(str(text or "").split())
    value = re.sub(r"0x[0-9a-f]+", "0x#", value, flags=re.I)
    value = re.sub(r":\d+:\d+\b", ":#:#", value)
    value = re.sub(r"\bline\s+\d+\b", "line #", value, flags=re.I)
    value = re.sub(r"\b\d+\s*ms\b", "# ms", value, flags=re.I)
    return value[-2200:]


def _internal_failure_fingerprint(step):
    if str(step.get("status") or "").lower() != "error":
        return None

    output = _normalize_internal_error(step.get("output"))
    if not output:
        return None

    raw = "|".join(
        [
            str(step.get("action") or ""),
            str(step.get("tool_name") or ""),
            output,
        ]
    )
    return hashlib.sha1(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()[:20]


def repeated_internal_failure(steps, threshold=REPEATED_INTERNAL_FAILURE_THRESHOLD):
    if not steps:
        return None

    threshold = max(2, int(threshold))
    latest = steps[-1]
    fingerprint = _internal_failure_fingerprint(latest)
    if not fingerprint:
        return None

    matching = []
    for step in reversed(steps):
        current = _internal_failure_fingerprint(step)
        if current != fingerprint:
            break
        matching.append(step)

    if len(matching) < threshold:
        return None

    matching.reverse()
    return {
        "fingerprint": fingerprint,
        "count": len(matching),
        "action": str(latest.get("action") or ""),
        "tool_name": str(latest.get("tool_name") or ""),
        "output": str(latest.get("output") or "")[-3000:],
        "first_step": int(matching[0].get("step_index") or 0),
        "last_step": int(matching[-1].get("step_index") or 0),
    }


def watch_agent_run_for_internal_failures(
    user_id,
    run_id,
    stop_event,
    poll_seconds=WATCHDOG_POLL_SECONDS,
):
    """Pause a run after the same internal action error repeats twice."""
    initialize_agent_stop_integrity_storage()

    while not stop_event.wait(max(0.05, float(poll_seconds))):
        run = get_agent_run(user_id, run_id)
        if not run:
            return

        if str(run.get("state") or "") not in {"running", "pausing"}:
            if str(run.get("state") or "") in {
                "paused",
                "waiting_input",
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return
            continue

        status = agent_stop_status(user_id, run_id, history_limit=1)
        active = status.get("active_stop")
        if active and active.get("code") == "repeated_internal_action_failure":
            return

        failure = repeated_internal_failure(
            list_agent_steps(user_id, run_id),
        )
        if not failure:
            continue

        message = (
            "ATLAS paused this run because the same internal action failure "
            f"repeated {failure['count']} times without a successful intervening step."
        )
        record_run_stop(
            user_id,
            run_id,
            code="repeated_internal_action_failure",
            category="control_plane",
            message=message,
            step_index=failure["last_step"],
            details=failure,
            active=True,
        )

        try:
            request_agent_pause(user_id, run_id)
        except AgentStoreError:
            pass
        return


def _infer_completed_stop(run, steps):
    current_step = int(run.get("current_step") or 0)
    ceiling = int(run.get("max_steps") or 0)
    remaining = max(0, ceiling - current_step)
    last = steps[-1] if steps else {}
    reason = str(last.get("reason") or "").lower()
    result = str(run.get("result") or "")

    if not result.lstrip().upper().startswith("NOT VERIFIED"):
        return None

    if "controller could not produce a valid next workspace/runtime action" in reason:
        return {
            "code": "controller_stalled",
            "category": "control_plane",
            "message": (
                "The controller could not produce a valid next workspace/runtime action "
                "after re-planning."
            ),
            "step_index": last.get("step_index"),
            "details": {"steps_remaining": remaining},
        }

    if "structured planner already attempted multiple recovery plans" in reason:
        return {
            "code": "planner_exhausted",
            "category": "planner",
            "message": "The structured project planner exhausted its current recovery plans.",
            "step_index": last.get("step_index"),
            "details": {"steps_remaining": remaining},
        }

    if current_step >= ceiling and ceiling > 0:
        return {
            "code": "step_budget_exhausted",
            "category": "budget",
            "message": "The current Agent execution step ceiling was reached.",
            "step_index": current_step,
            "details": {"steps_remaining": 0},
        }

    return {
        "code": "unverified_final",
        "category": "control_plane",
        "message": "The execution cycle ended without a verified current workspace.",
        "step_index": last.get("step_index"),
        "details": {"steps_remaining": remaining},
    }


def _termination_sentence(stop):
    code = str(stop.get("code") or "")
    remaining = int((stop.get("details") or {}).get("steps_remaining") or 0)

    if code == "controller_stalled":
        suffix = (
            f" {remaining} continuation step{'s' if remaining != 1 else ''} remained available."
            if remaining > 0
            else ""
        )
        return (
            "ATLAS stopped this execution cycle because the controller could not produce "
            "a valid next workspace/runtime action after re-planning."
            + suffix
        )

    if code == "planner_exhausted":
        return (
            "ATLAS stopped this execution cycle because the structured project planner "
            "exhausted its current recovery plan for the unchanged failure."
        )

    if code == "step_budget_exhausted":
        return "The run reached its current step budget before a verified pass."

    return "ATLAS ended this execution cycle without a verified pass."


def _rewrite_completed_unverified_result(user_id, run_id, run, stop):
    result = str(run.get("result") or "").strip()
    if not result:
        return

    old = "The run reached its current step budget before a verified pass."
    sentence = _termination_sentence(stop)
    if old in result:
        result = result.replace(old, sentence, 1)
    elif sentence not in result:
        result = result + "\n\n" + sentence

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_runs
        SET result = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            result[:20000],
            utc_iso(),
            str(run_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()


def _apply_internal_failure_stop(user_id, run_id, run, active_stop):
    details = active_stop.get("details") or {}
    blocker = str(details.get("output") or "").strip()
    message = str(active_stop.get("message") or "").strip()

    result = (
        "NOT VERIFIED — ATLAS stopped this execution cycle after detecting a repeated "
        "internal action failure. This is a control/runtime failure and is not evidence "
        "that the project implementation itself is incorrect.\n\n"
        + message
    )
    if blocker:
        result += "\n\nInternal blocker:\n" + blocker[-3000:]
    result += (
        "\n\nUse Continue / Revise after the ATLAS/runtime issue is corrected; the existing "
        "workspace and run history are preserved."
    )

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'failed',
            result = ?,
            error = ?,
            pending_question = NULL,
            cancel_requested = 0,
            pause_requested = 0,
            finished_at = ?,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            result[:20000],
            message[:5000],
            timestamp,
            timestamp,
            str(run_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()


def _record_failed_run_stop(user_id, run_id, run):
    error = str(run.get("error") or "Agent run failed.")
    lower = error.lower()

    if "runtime budget was reached" in lower:
        code, category = "runtime_timeout", "budget"
    elif "local agent model" in lower or "ollama" in lower:
        code, category = "model_failure", "model"
    elif "unavailable" in lower:
        code, category = "tool_unavailable", "infrastructure"
    else:
        code, category = "run_exception", "control_plane"

    record_run_stop(
        user_id,
        run_id,
        code=code,
        category=category,
        message=error,
        step_index=run.get("current_step"),
        details={},
        active=True,
    )


def finalize_run_stop_integrity(user_id, run_id):
    """
    Persist a truthful machine-readable stop reason after an execution thread
    finishes and repair stale final prose when necessary.
    """
    run = get_agent_run(user_id, run_id)
    if not run:
        return None

    status = agent_stop_status(user_id, run_id)
    active = status.get("active_stop")

    # The watchdog normally catches this while the run is active. Re-check at
    # thread completion as well so very fast repeated failures cannot race past
    # the polling interval.
    if not active:
        repeated = repeated_internal_failure(
            list_agent_steps(user_id, run_id),
        )
        if repeated:
            record_run_stop(
                user_id,
                run_id,
                code="repeated_internal_action_failure",
                category="control_plane",
                message=(
                    "ATLAS stopped this run because the same internal action failure "
                    f"repeated {repeated['count']} times without a successful intervening step."
                ),
                step_index=repeated["last_step"],
                details=repeated,
                active=True,
            )
            active = agent_stop_status(user_id, run_id).get("active_stop")

    if active and active.get("code") == "repeated_internal_action_failure":
        _apply_internal_failure_stop(user_id, run_id, run, active)
        return agent_stop_status(user_id, run_id)

    state = str(run.get("state") or "")
    if state == "completed":
        stop = _infer_completed_stop(
            run,
            list_agent_steps(user_id, run_id),
        )
        if stop:
            if not active or active.get("step_index") != stop.get("step_index") or active.get("code") != stop.get("code"):
                record_run_stop(
                    user_id,
                    run_id,
                    code=stop["code"],
                    category=stop["category"],
                    message=stop["message"],
                    step_index=stop.get("step_index"),
                    details=stop.get("details") or {},
                    active=True,
                )
            _rewrite_completed_unverified_result(user_id, run_id, run, stop)

    elif state == "failed" and not active:
        _record_failed_run_stop(user_id, run_id, run)

    return agent_stop_status(user_id, run_id)
