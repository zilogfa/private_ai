"""ATLAS v3.2 unified execution-step governance and automatic tail budget.

The legacy Agent UI exposes a simple step ceiling.  v3 keeps that user-visible
budget but may grant a very small automatic *tail* when authoritative evidence
shows that a bounded engineering operation is making progress and needs the
remaining verify/repair/accept/finalize phases to finish safely.

Tail grants are not an escape hatch for loops:
- no tail for startup/spec/build
- no tail after repeated stalls/regression
- only progress/verification/committed-repair evidence can authorize it
- automatic tail is capped per run and by the configured global Agent ceiling
"""

import app.config as config
from app.database import get_connection
from app.services.agents import get_agent_run, list_agent_steps, utc_iso
from app.services.agent_sandbox import list_agent_sandbox_executions
from app.services.agent_v3_storage import (
    demonstration_status,
    list_budget_grants,
    list_repair_outcomes,
    record_budget_grant,
    total_tail_steps,
)
from app.services.agent_v3_revision_governance import (
    V3_LIFETIME_STEP_CEILING,
    explicit_resume_grant_reason,
)

TAIL_GRANT_STEPS = 4
MAX_AUTO_TAIL_STEPS = 8
DEMONSTRATION_REPAIR_RESERVE_STEPS = 6
MAX_DEMONSTRATION_REPAIR_RESERVE_STEPS = 8
EXPLICIT_RESUME_GRANT_STEPS = 6
TAIL_PHASES = {"starting", "environment", "verify", "repair", "acceptance", "finalize"}
PROGRESS_CLASSES = {"strong_progress", "changed_failure", "verified"}


def _latest_node_execution(run):
    items = list_agent_sandbox_executions(run["user_id"], run["id"], limit=100)
    node = [item for item in items if str(item.get("runtime") or "").lower() == "node"]
    return node[-1] if node else None


def _latest_completed_action(run):
    for step in reversed(list_agent_steps(run["user_id"], run["id"])):
        if str(step.get("status") or "") == "completed":
            return str(step.get("action") or "")
    return ""


def _demonstration_reserve_used(run):
    return sum(
        int(item.get("steps") or 0)
        for item in list_budget_grants(run["user_id"], run["id"], limit=300)
        if str(item.get("grant_type") or "") == "demonstration_repair_reserve"
    )


def _demonstration_repair_pending(run):
    demo = demonstration_status(run["user_id"], run["id"])
    return bool(
        demo.get("failure_observed")
        and not demo.get("repair_verified")
    )


def _eligible_reason(run, phase):
    outcomes = list_repair_outcomes(run["user_id"], run["id"], limit=100)
    latest = outcomes[-1] if outcomes else {}
    progress = str(latest.get("progress_class") or "")
    if progress == "regression":
        return None
    if progress == "stalled":
        stalls = 0
        for item in reversed(outcomes):
            if str(item.get("progress_class") or "") == "stalled":
                stalls += 1
            else:
                break
        if stalls >= 2:
            return None

    execution = _latest_node_execution(run)
    if execution and str(execution.get("status") or "") == "success" and int(execution.get("exit_code") or 0) == 0:
        if phase in {"acceptance", "finalize"}:
            return "verified execution needs acceptance/finalization tail"

    latest_action = _latest_completed_action(run)
    if phase in {"verify", "environment"} and latest_action in {"repair", "intentional_defect"}:
        return "a committed workspace mutation still needs authoritative verification"

    if phase == "starting" and progress in {"strong_progress", "changed_failure"}:
        return "resume began at the user step ceiling while the latest repair still showed measurable progress"

    if phase == "repair" and progress in PROGRESS_CLASSES:
        return "latest committed repair made measurable progress"

    if phase in {"acceptance", "finalize"} and progress == "verified":
        return "verified repair needs deterministic completion tail"
    return None


def maybe_grant_tail_budget(run, phase):
    run = get_agent_run(run["user_id"], run["id"]) or run
    current = int(run.get("current_step") or 0)
    ceiling = int(run.get("max_steps") or 0)
    if current < ceiling:
        return {"granted": False, "remaining": ceiling - current}
    if str(phase) not in TAIL_PHASES:
        return {"granted": False, "reason": "phase is not tail-eligible"}

    # A user clicking Resume after a budget-exhausted v3 run is an explicit
    # continuation signal, not another automatic progress tail.  Give it one
    # small tranche bounded by the same lifetime ceiling used by revisions.
    resume_reason = explicit_resume_grant_reason(run, str(phase))
    if resume_reason:
        lifetime_remaining = max(
            0,
            int(V3_LIFETIME_STEP_CEILING) - current,
        )
        grant = min(EXPLICIT_RESUME_GRANT_STEPS, lifetime_remaining)
        if grant > 0:
            base_ceiling = max(current, ceiling)
            new_ceiling = base_ceiling + grant
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE agent_runs
                SET max_steps = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_ceiling, utc_iso(), str(run["id"]), int(run["user_id"])),
            )
            conn.commit()
            conn.close()
            record_budget_grant(
                run,
                grant_type="user_resume",
                phase=str(phase),
                steps=grant,
                reason=resume_reason,
                ceiling_before=ceiling,
                ceiling_after=new_ceiling,
            )
            return {
                "granted": True,
                "steps": grant,
                "ceiling_before": ceiling,
                "ceiling_after": new_ceiling,
                "reason": resume_reason,
                "grant_type": "user_resume",
            }

    # A controlled failure that ATLAS deliberately injected and then
    # authoritatively observed is *successful demonstration evidence*.  It owns
    # a small repair/verification reserve independent of the ordinary progress
    # tail so the lifecycle cannot strand itself immediately after proving the
    # required failure.
    if (
        _demonstration_repair_pending(run)
        and str(phase) in {"starting", "environment", "verify", "repair", "acceptance", "finalize"}
    ):
        reserve_used = _demonstration_reserve_used(run)
        reserve_remaining = max(0, MAX_DEMONSTRATION_REPAIR_RESERVE_STEPS - reserve_used)
        lifetime_remaining = max(0, int(V3_LIFETIME_STEP_CEILING) - current)
        grant = min(DEMONSTRATION_REPAIR_RESERVE_STEPS, reserve_remaining, lifetime_remaining)
        if grant > 0:
            base_ceiling = max(current, ceiling)
            new_ceiling = base_ceiling + grant
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE agent_runs
                SET max_steps = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_ceiling, utc_iso(), str(run["id"]), int(run["user_id"])),
            )
            conn.commit()
            conn.close()
            reason = (
                "authoritative controlled-failure evidence was observed; "
                "reserve bounded repair/verification capacity for the demonstration campaign"
            )
            record_budget_grant(
                run,
                grant_type="demonstration_repair_reserve",
                phase=str(phase),
                steps=grant,
                reason=reason,
                ceiling_before=ceiling,
                ceiling_after=new_ceiling,
            )
            return {
                "granted": True,
                "steps": grant,
                "ceiling_before": ceiling,
                "ceiling_after": new_ceiling,
                "reason": reason,
                "grant_type": "demonstration_repair_reserve",
            }

    used = total_tail_steps(run["user_id"], run["id"])
    if used >= MAX_AUTO_TAIL_STEPS:
        return {"granted": False, "reason": "automatic tail cap reached"}

    reason = _eligible_reason(run, str(phase))
    if not reason:
        return {"granted": False, "reason": "no authoritative progress evidence supports a tail grant"}

    configured_ceiling = max(
        ceiling,
        int(getattr(config, "AGENT_MAX_STEPS", ceiling or 1)),
    )
    allowed_by_config = max(0, configured_ceiling - ceiling)
    remaining_tail = MAX_AUTO_TAIL_STEPS - used
    grant = min(TAIL_GRANT_STEPS, remaining_tail, allowed_by_config)
    if grant <= 0:
        return {"granted": False, "reason": "configured Agent hard ceiling reached"}

    new_ceiling = ceiling + grant
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE agent_runs
        SET max_steps = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (new_ceiling, utc_iso(), str(run["id"]), int(run["user_id"])),
    )
    conn.commit()
    conn.close()
    record_budget_grant(
        run,
        grant_type="progress_tail",
        phase=str(phase),
        steps=grant,
        reason=reason,
        ceiling_before=ceiling,
        ceiling_after=new_ceiling,
    )
    return {
        "granted": True,
        "steps": grant,
        "ceiling_before": ceiling,
        "ceiling_after": new_ceiling,
        "reason": reason,
    }
