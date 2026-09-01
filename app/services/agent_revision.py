"""
ATLAS v2.1.1 - Continue / Revise + feedback foundation.

A completed Agent run is no longer a dead end. A revision keeps the SAME:
- run id
- Agent identity
- workspace/files
- web/document sources
- evidence
- sandbox execution history
- interaction history

The previous final result is snapshotted before continuation.

Feedback events are actor-aware from day one so the same foundation can later
accept review/corrections from:
- user
- another Agent
- regular Chat
- system/orchestrator

Only user feedback is accepted through the v2.1.1 UI/API.
"""

import json
import os

from app.database import get_connection
from app.services.agents import (
    AgentStoreError,
    get_agent_run,
    utc_iso,
)


REVISION_STEP_OPTIONS = {
    6,
    12,
    25,
}

REVISION_DEFAULT_STEPS = 12

REVISION_LIFETIME_STEP_CEILING = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_REVISION_MAX_TOTAL_STEPS",
        "100",
    )
)

_STORAGE_READY = False


class AgentRevisionError(Exception):
    pass


def initialize_agent_revision_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_feedback_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            actor_type TEXT NOT NULL DEFAULT 'user',
            actor_id TEXT,
            direction TEXT NOT NULL DEFAULT 'to_agent',
            feedback_type TEXT NOT NULL DEFAULT 'revision',
            content TEXT NOT NULL,
            learn_opt_in INTEGER NOT NULL DEFAULT 0,
            learning_status TEXT NOT NULL DEFAULT 'not_requested',
            learned_memory_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            processed_at TEXT,
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
        CREATE INDEX IF NOT EXISTS idx_agent_feedback_run
        ON agent_feedback_events(
            run_id,
            id
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            feedback_event_id INTEGER NOT NULL,
            previous_result TEXT,
            previous_error TEXT,
            requested_extra_steps INTEGER NOT NULL,
            start_step INTEGER NOT NULL,
            end_step INTEGER,
            resulting_result TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (feedback_event_id)
                REFERENCES agent_feedback_events(id)
                ON DELETE CASCADE,
            UNIQUE(run_id, revision_number)
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_revisions_run
        ON agent_run_revisions(
            run_id,
            revision_number
        )
        """
    )

    conn.commit()
    conn.close()

    _STORAGE_READY = True


def _json_list(value):
    if isinstance(value, list):
        return value

    try:
        parsed = json.loads(
            value
            or "[]"
        )
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return []

    return (
        parsed
        if isinstance(parsed, list)
        else []
    )


def _feedback_from_row(row):
    if not row:
        return None

    return {
        "id": row[0],
        "run_id": row[1],
        "user_id": row[2],
        "actor_type": row[3],
        "actor_id": row[4],
        "direction": row[5],
        "feedback_type": row[6],
        "content": row[7],
        "learn_opt_in": bool(row[8]),
        "learning_status": row[9],
        "learned_memory_ids": _json_list(row[10]),
        "created_at": row[11],
        "processed_at": row[12],
    }


def _revision_from_row(row):
    if not row:
        return None

    return {
        "id": row[0],
        "run_id": row[1],
        "user_id": row[2],
        "revision_number": int(row[3]),
        "feedback_event_id": row[4],
        "previous_result": row[5],
        "previous_error": row[6],
        "requested_extra_steps": int(row[7]),
        "start_step": int(row[8]),
        "end_step": (
            int(row[9])
            if row[9] is not None
            else None
        ),
        "resulting_result": row[10],
        "status": row[11],
        "created_at": row[12],
        "completed_at": row[13],
    }


def _normalize_extra_steps(value):
    try:
        steps = int(
            value
            if value is not None
            else REVISION_DEFAULT_STEPS
        )
    except (
        TypeError,
        ValueError,
    ) as error:
        raise AgentRevisionError(
            "Invalid revision step budget."
        ) from error

    if steps not in REVISION_STEP_OPTIONS:
        raise AgentRevisionError(
            "Revision step budget must be 6, 12, or 25."
        )

    return steps


def _normalize_feedback(value):
    text = str(
        value
        or ""
    ).strip()

    if not text:
        raise AgentRevisionError(
            "Tell the Agent what you want changed, corrected, or continued."
        )

    if len(text) > 8000:
        raise AgentRevisionError(
            "Revision feedback is too long."
        )

    return text


def list_run_revisions(
    user_id,
    run_id,
):
    initialize_agent_revision_storage()

    run = get_agent_run(
        user_id,
        run_id,
    )

    if not run:
        raise AgentRevisionError(
            "Agent run was not found."
        )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            r.id,
            r.run_id,
            r.user_id,
            r.revision_number,
            r.feedback_event_id,
            r.previous_result,
            r.previous_error,
            r.requested_extra_steps,
            r.start_step,
            r.end_step,
            r.resulting_result,
            r.status,
            r.created_at,
            r.completed_at,
            f.actor_type,
            f.actor_id,
            f.direction,
            f.feedback_type,
            f.content,
            f.learn_opt_in,
            f.learning_status,
            f.learned_memory_ids_json,
            f.created_at,
            f.processed_at
        FROM agent_run_revisions r
        JOIN agent_feedback_events f
            ON f.id = r.feedback_event_id
        WHERE
            r.run_id = ?
            AND r.user_id = ?
        ORDER BY r.revision_number ASC
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )

    items = []

    for row in cursor.fetchall():
        revision = _revision_from_row(
            row[:14]
        )

        feedback = _feedback_from_row(
            (
                row[4],
                row[1],
                row[2],
                row[14],
                row[15],
                row[16],
                row[17],
                row[18],
                row[19],
                row[20],
                row[21],
                row[22],
                row[23],
            )
        )

        revision[
            "feedback"
        ] = feedback

        items.append(
            revision
        )

    conn.close()

    return items


def latest_open_revision(
    user_id,
    run_id,
):
    initialize_agent_revision_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            run_id,
            user_id,
            revision_number,
            feedback_event_id,
            previous_result,
            previous_error,
            requested_extra_steps,
            start_step,
            end_step,
            resulting_result,
            status,
            created_at,
            completed_at
        FROM agent_run_revisions
        WHERE
            run_id = ?
            AND user_id = ?
            AND status = 'running'
        ORDER BY revision_number DESC
        LIMIT 1
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )

    revision = _revision_from_row(
        cursor.fetchone()
    )

    conn.close()

    return revision


def get_feedback_event(
    user_id,
    event_id,
):
    initialize_agent_revision_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            run_id,
            user_id,
            actor_type,
            actor_id,
            direction,
            feedback_type,
            content,
            learn_opt_in,
            learning_status,
            learned_memory_ids_json,
            created_at,
            processed_at
        FROM agent_feedback_events
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            int(event_id),
            int(user_id),
        ),
    )

    event = _feedback_from_row(
        cursor.fetchone()
    )

    conn.close()

    return event


def begin_user_revision(
    user_id,
    run_id,
    feedback,
    *,
    extra_steps=REVISION_DEFAULT_STEPS,
    learn_from_feedback=True,
):
    """
    Re-open the SAME persistent run.

    Completed is the normal path. Failed/cancelled/interrupted runs are also
    accepted because explicit user feedback may explain how to recover.

    Existing result/error is snapshotted into the revision row before the run
    is re-queued.
    """

    initialize_agent_revision_storage()

    run = get_agent_run(
        user_id,
        run_id,
    )

    if not run:
        raise AgentRevisionError(
            "Agent run was not found."
        )

    allowed_states = {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "paused",
    }

    if run[
        "state"
    ] not in allowed_states:
        raise AgentRevisionError(
            "This Agent run cannot be revised from its current state."
        )

    if latest_open_revision(
        user_id,
        run_id,
    ):
        raise AgentRevisionError(
            "This Agent already has an unfinished revision."
        )

    text = _normalize_feedback(
        feedback
    )

    requested_steps = _normalize_extra_steps(
        extra_steps
    )

    current_step = int(
        run.get(
            "current_step"
        )
        or 0
    )

    remaining_lifetime = max(
        0,
        REVISION_LIFETIME_STEP_CEILING
        - current_step,
    )

    if remaining_lifetime <= 0:
        raise AgentRevisionError(
            "This run reached the revision lifetime step ceiling. "
            "Start a new run from the current workspace/export instead."
        )

    granted_steps = min(
        requested_steps,
        remaining_lifetime,
    )

    new_max_steps = (
        current_step
        + granted_steps
    )

    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COALESCE(
            MAX(revision_number),
            0
        )
        FROM agent_run_revisions
        WHERE run_id = ?
        """,
        (
            str(run_id),
        ),
    )

    revision_number = (
        int(
            cursor.fetchone()[0]
            or 0
        )
        + 1
    )

    cursor.execute(
        """
        INSERT INTO agent_feedback_events (
            run_id,
            user_id,
            actor_type,
            actor_id,
            direction,
            feedback_type,
            content,
            learn_opt_in,
            learning_status,
            learned_memory_ids_json,
            created_at,
            processed_at
        )
        VALUES (
            ?, ?, 'user', ?, 'to_agent',
            'revision', ?, ?,
            ?, '[]', ?, NULL
        )
        """,
        (
            str(run_id),
            int(user_id),
            str(user_id),
            text,
            int(
                bool(
                    learn_from_feedback
                )
            ),
            (
                "pending"
                if learn_from_feedback
                else "not_requested"
            ),
            timestamp,
        ),
    )

    feedback_event_id = (
        cursor.lastrowid
    )

    cursor.execute(
        """
        INSERT INTO agent_run_revisions (
            run_id,
            user_id,
            revision_number,
            feedback_event_id,
            previous_result,
            previous_error,
            requested_extra_steps,
            start_step,
            end_step,
            resulting_result,
            status,
            created_at,
            completed_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?,
            NULL, NULL, 'running', ?, NULL
        )
        """,
        (
            str(run_id),
            int(user_id),
            revision_number,
            feedback_event_id,
            run.get(
                "result"
            ),
            run.get(
                "error"
            ),
            granted_steps,
            current_step + 1,
            timestamp,
        ),
    )

    # Agent inputs are already part of every controller's local run ledger.
    # Prefixing the feedback makes the new intent unambiguous while retaining
    # the original goal and all prior interactions.
    cursor.execute(
        """
        INSERT INTO agent_inputs (
            run_id,
            user_id,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            str(run_id),
            int(user_id),
            (
                f"[REVISION #{revision_number} - USER FEEDBACK]\n"
                f"{text}\n\n"
                "Continue the SAME project/run. Preserve useful existing work. "
                "Inspect the current workspace/evidence before changing it. "
                "Address this feedback directly, verify changes when possible, "
                "and produce a new final result."
            ),
            timestamp,
        ),
    )

    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'queued',
            max_steps = ?,
            result = NULL,
            error = NULL,
            pending_question = NULL,
            cancel_requested = 0,
            pause_requested = 0,
            finished_at = NULL,
            updated_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            new_max_steps,
            timestamp,
            str(run_id),
            int(user_id),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "run":
            get_agent_run(
                user_id,
                run_id,
            ),
        "revision_number":
            revision_number,
        "feedback_event_id":
            feedback_event_id,
        "granted_extra_steps":
            granted_steps,
        "lifetime_step_ceiling":
            REVISION_LIFETIME_STEP_CEILING,
    }


def complete_latest_revision(
    user_id,
    run_id,
    result,
):
    revision = latest_open_revision(
        user_id,
        run_id,
    )

    if not revision:
        return None

    run = get_agent_run(
        user_id,
        run_id,
    )

    if not run:
        return None

    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_run_revisions
        SET
            end_step = ?,
            resulting_result = ?,
            status = 'completed',
            completed_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            int(
                run.get(
                    "current_step"
                )
                or 0
            ),
            str(
                result
                or ""
            ),
            timestamp,
            int(
                revision[
                    "id"
                ]
            ),
            int(
                user_id
            ),
        ),
    )

    conn.commit()
    conn.close()

    return {
        **revision,
        "end_step":
            int(
                run.get(
                    "current_step"
                )
                or 0
            ),
        "resulting_result":
            str(
                result
                or ""
            ),
        "status":
            "completed",
        "completed_at":
            timestamp,
    }


def update_feedback_learning(
    user_id,
    event_id,
    *,
    status,
    memory_ids=None,
):
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_feedback_events
        SET
            learning_status = ?,
            learned_memory_ids_json = ?,
            processed_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            str(
                status
            )[:40],
            json.dumps(
                memory_ids
                or []
            ),
            timestamp,
            int(
                event_id
            ),
            int(
                user_id
            ),
        ),
    )

    conn.commit()
    conn.close()
