"""
ATLAS v2.3.0c - continuation budget governance.

This module deliberately keeps continuation budgeting separate from the core
agent store and runtime implementations.

Design goals:
- preserve lifetime step numbering/history
- grant a fresh execution tranche when a user explicitly resumes a run
- never reset current_step or delete historical final/error steps
- clear stale top-level result text when new work begins
- expose budget metadata for current/future UI and multi-agent governance
- give waiting-input continuations enough room to act on the user's answer
"""

import app.config as config

from app.database import get_connection
from app.services.agent_stop_integrity import clear_active_run_stop
from app.services.agents import (
    AgentStoreError,
    add_agent_input,
    get_agent_run,
    queue_agent_resume,
    utc_iso,
)


_STORAGE_READY = False
_RESUMABLE_STATES = {
    "paused",
    "interrupted",
    "cancelled",
    "failed",
}


def initialize_agent_run_governance_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_governance (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            initial_step_budget INTEGER NOT NULL,
            resume_count INTEGER NOT NULL DEFAULT 0,
            input_budget_grants INTEGER NOT NULL DEFAULT 0,
            total_additional_steps INTEGER NOT NULL DEFAULT 0,
            last_grant_steps INTEGER NOT NULL DEFAULT 0,
            last_grant_reason TEXT,
            last_granted_at TEXT,
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

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_run_governance_user
        ON agent_run_governance(user_id, updated_at)
        """
    )

    conn.commit()
    conn.close()
    _STORAGE_READY = True


def _configured_max_grant():
    value = getattr(
        config,
        "AGENT_CONTINUATION_MAX_STEPS",
        getattr(config, "AGENT_MAX_STEPS", 25),
    )
    try:
        return max(2, int(value))
    except (TypeError, ValueError):
        return 25


def _normalize_grant(value, default):
    try:
        grant = int(default if value is None else value)
    except (TypeError, ValueError) as error:
        raise AgentStoreError("Invalid continuation step budget.") from error

    return max(2, min(_configured_max_grant(), grant))


def _ensure_governance_row(user_id, run_id, run=None):
    initialize_agent_run_governance_storage()

    run = run or get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            initial_step_budget,
            resume_count,
            input_budget_grants,
            total_additional_steps,
            last_grant_steps,
            last_grant_reason,
            last_granted_at,
            created_at,
            updated_at
        FROM agent_run_governance
        WHERE run_id = ? AND user_id = ?
        """,
        (str(run_id), int(user_id)),
    )
    row = cursor.fetchone()

    if not row:
        timestamp = utc_iso()
        initial_budget = max(2, int(run.get("max_steps") or 6))
        cursor.execute(
            """
            INSERT INTO agent_run_governance (
                run_id,
                user_id,
                initial_step_budget,
                resume_count,
                input_budget_grants,
                total_additional_steps,
                last_grant_steps,
                last_grant_reason,
                last_granted_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 0, 0, 0, 0, NULL, NULL, ?, ?)
            """,
            (str(run_id), int(user_id), initial_budget, timestamp, timestamp),
        )
        conn.commit()
        row = (initial_budget, 0, 0, 0, 0, None, None, timestamp, timestamp)

    conn.close()
    return {
        "initial_step_budget": int(row[0] or 6),
        "resume_count": int(row[1] or 0),
        "input_budget_grants": int(row[2] or 0),
        "total_additional_steps": int(row[3] or 0),
        "last_grant_steps": int(row[4] or 0),
        "last_grant_reason": row[5],
        "last_granted_at": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


def agent_budget_status(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    governance = _ensure_governance_row(user_id, run_id, run=run)
    used = int(run.get("current_step") or 0)
    ceiling = int(run.get("max_steps") or governance["initial_step_budget"])

    return {
        **governance,
        "lifetime_steps_used": used,
        "current_step_ceiling": ceiling,
        "steps_remaining": max(0, ceiling - used),
        "grant_max": _configured_max_grant(),
    }


def _grant_continuation_budget(
    user_id,
    run_id,
    *,
    requested_steps=None,
    reason,
    clear_result=False,
):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    governance = _ensure_governance_row(user_id, run_id, run=run)
    grant = _normalize_grant(requested_steps, governance["initial_step_budget"])
    current_step = int(run.get("current_step") or 0)
    current_ceiling = int(run.get("max_steps") or governance["initial_step_budget"])

    # Verification tails and legacy resumes can leave lifetime steps beyond an
    # old ceiling. Grant from whichever is greater so the continuation always
    # starts with real usable capacity.
    new_ceiling = max(current_step, current_ceiling) + grant

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()

    if clear_result:
        cursor.execute(
            """
            UPDATE agent_runs
            SET max_steps = ?, result = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_ceiling, timestamp, str(run_id), int(user_id)),
        )
    else:
        cursor.execute(
            """
            UPDATE agent_runs
            SET max_steps = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_ceiling, timestamp, str(run_id), int(user_id)),
        )

    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        raise AgentStoreError("Agent run could not receive continuation budget.")

    cursor.execute(
        """
        UPDATE agent_run_governance
        SET
            resume_count = resume_count + CASE WHEN ? = 'resume' THEN 1 ELSE 0 END,
            input_budget_grants = input_budget_grants + CASE WHEN ? = 'input' THEN 1 ELSE 0 END,
            total_additional_steps = total_additional_steps + ?,
            last_grant_steps = ?,
            last_grant_reason = ?,
            last_granted_at = ?,
            updated_at = ?
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(reason),
            str(reason),
            grant,
            grant,
            str(reason),
            timestamp,
            timestamp,
            str(run_id),
            int(user_id),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "granted_steps": grant,
        "previous_ceiling": current_ceiling,
        "new_ceiling": new_ceiling,
        "lifetime_steps_used": current_step,
        "steps_remaining_after_grant": new_ceiling - current_step,
        "reason": str(reason),
    }


def resume_agent_run_with_budget(user_id, run_id, additional_steps=None):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    if run.get("state") not in _RESUMABLE_STATES:
        raise AgentStoreError("This agent run is not resumable from its current state.")

    grant = _grant_continuation_budget(
        user_id,
        run_id,
        requested_steps=additional_steps,
        reason="resume",
        clear_result=True,
    )
    clear_active_run_stop(user_id, run_id)
    resumed = queue_agent_resume(user_id, run_id)

    return resumed, {
        **agent_budget_status(user_id, run_id),
        "grant": grant,
    }


def provide_agent_input_with_budget(user_id, run_id, content):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")
    if run.get("state") != "waiting_input":
        raise AgentStoreError("This agent is not waiting for user input.")

    status = agent_budget_status(user_id, run_id)
    grant = None

    # A user answer must never resume into an exhausted ceiling. Preserve useful
    # remaining budget; only top it up when the next action would otherwise have
    # no room.
    if int(status.get("steps_remaining") or 0) < 2:
        grant = _grant_continuation_budget(
            user_id,
            run_id,
            reason="input",
            clear_result=True,
        )

    clear_active_run_stop(user_id, run_id)
    resumed = add_agent_input(user_id, run_id, content)
    return resumed, {
        **agent_budget_status(user_id, run_id),
        "grant": grant,
    }
