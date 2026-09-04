"""ATLAS v3 revision lifecycle and explicit continuation governance.

This service reconciles the legacy revision ledger with the v3 execution
lifecycle.  A revision may be 'running' only while its execution segment is
actually active.  Terminal v3 outcomes close any open revision so a later
Continue / Revise request cannot be blocked by stale state.

It also owns the small explicit Resume continuation tranche.  Automatic
progress-tail grants remain evidence-driven and capped; an explicit user Resume
is a separate signal and may grant a bounded continuation tranche up to the
lifetime run ceiling.
"""

import os

from app.database import get_connection
from app.services.agents import get_agent_run, utc_iso
from app.services.agent_revision import latest_open_revision
from app.services.agent_v3_storage import get_v3_run


V3_LIFETIME_STEP_CEILING = int(
    os.environ.get("PRIVATE_AI_AGENT_REVISION_MAX_TOTAL_STEPS", "100")
)

_TERMINAL_RUN_STATES = {
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "paused",
}


def _revision_terminal_status(run, error=None):
    state = str((run or {}).get("state") or "").lower()
    text = str(error or (run or {}).get("error") or "").lower()
    if "step budget" in text or "budget" in text:
        return "budget_exhausted"
    if state == "completed":
        return "completed"
    if state == "cancelled":
        return "cancelled"
    if state in {"interrupted", "paused"}:
        return "interrupted"
    return "failed"


def close_open_revision_for_terminal_run(
    user_id,
    run_id,
    *,
    error=None,
    status=None,
):
    """Close a stale/open revision when its execution segment has terminated."""
    run = get_agent_run(user_id, run_id)
    if not run:
        return None

    revision = latest_open_revision(user_id, run_id)
    if not revision:
        return None

    terminal_status = str(status or _revision_terminal_status(run, error))
    if not status and str(run.get("state") or "") not in _TERMINAL_RUN_STATES:
        return None

    timestamp = utc_iso()
    result_text = str(run.get("result") or error or run.get("error") or "")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE agent_run_revisions
        SET
            end_step = ?,
            resulting_result = ?,
            status = ?,
            completed_at = ?
        WHERE
            id = ?
            AND user_id = ?
            AND status = 'running'
        """,
        (
            int(run.get("current_step") or 0),
            result_text[:12000],
            terminal_status[:40],
            timestamp,
            int(revision["id"]),
            int(user_id),
        ),
    )

    # A revision that did not reach a normal final result must not leave a
    # durable-learning request permanently pending.
    cur.execute(
        """
        UPDATE agent_feedback_events
        SET
            learning_status = 'revision_not_completed',
            processed_at = ?
        WHERE
            id = ?
            AND user_id = ?
            AND learning_status = 'pending'
        """,
        (
            timestamp,
            int(revision["feedback_event_id"]),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()

    return {
        **revision,
        "status": terminal_status,
        "end_step": int(run.get("current_step") or 0),
        "completed_at": timestamp,
    }


def reconcile_before_new_revision(user_id, run_id):
    """Repair legacy stale revision state before accepting new user feedback."""
    run = get_agent_run(user_id, run_id)
    if not run:
        return None
    if not get_v3_run(user_id, run_id):
        return None
    if str(run.get("state") or "") not in _TERMINAL_RUN_STATES:
        return None
    return close_open_revision_for_terminal_run(user_id, run_id)


def explicit_resume_grant_reason(run, phase):
    """Return a reason when a user Resume should receive a small v3 tranche.

    There is no separate Resume payload in the legacy API.  The reliable signal
    is: a v3 run was terminally failed for budget exhaustion, the user explicitly
    re-queued it, and the new worker is entering STARTING at an exhausted ceiling.
    """
    if str(phase) != "starting":
        return None
    v3 = get_v3_run(run["user_id"], run["id"])
    if not v3 or str(v3.get("status") or "") != "failed":
        return None
    text = str(v3.get("last_error") or run.get("error") or "").lower()
    if "step budget" not in text and "automatic tail cap" not in text and "runtime budget" not in text:
        return None
    if int(run.get("current_step") or 0) >= V3_LIFETIME_STEP_CEILING:
        return None
    return "explicit Resume action after bounded budget exhaustion"
