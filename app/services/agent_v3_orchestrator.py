"""ATLAS v3 unified coding-agent orchestrator.

This file is intentionally the single state machine for native v3 coding runs.
It replaces version-stacked execution wrappers with explicit lifecycle phases:

    STARTING -> PROJECT_SPEC -> BUILD -> ENVIRONMENT -> VERIFY
             -> REPAIR (bounded) -> ACCEPTANCE -> FINALIZE

The first native adapter is Node/JavaScript.  Other agent/research workloads and
Python coding remain on the v2.3 engine until their v3 adapters are migrated.
"""

import json
import time

from app.config import AGENT_MAX_RUNTIME_SECONDS
from app.services import agent_runner as legacy_runner
from app.services.agents import (
    AgentStoreError,
    begin_agent_step,
    finish_agent_step,
    get_agent_run,
    list_agent_steps,
    mark_agent_cancelled,
    mark_agent_failed,
    mark_agent_paused,
)
from app.services.agent_runtime import RUNTIME_NODE, effective_runtime
from app.services.agent_environment import AgentEnvironmentError
from app.services.agent_sandbox import (
    AgentSandboxError,
    agent_run_allows_code,
    list_agent_sandbox_executions,
    list_workspace_files,
)
from app.services.agent_v3_node_adapter import (
    V3NodeError,
    acceptance_summary,
    bootstrap_project,
    ensure_environment,
    evaluate_acceptance,
    execution_passed,
    failure_fingerprint,
    inject_intentional_defect,
    repair_project,
    verify_project,
)
from app.services.agent_v3_spec import build_project_spec, spec_summary
from app.services.agent_v3_repair_governor import (
    ABSOLUTE_MAX_COMMITTED_REPAIRS,
    BASE_REPAIR_TRANCHE,
    compare_evidence,
    progress_summary,
    repair_permission,
)
from app.services.agent_v3_execution_governor import maybe_grant_tail_budget
from app.services.agent_v3_revision_governance import close_open_revision_for_terminal_run
from app.services.agent_v3_storage import (
    CORE_VERSION,
    ensure_v3_run,
    get_v3_run,
    list_repair_outcomes,
    record_acceptance_evaluation,
    record_phase_event,
    record_repair_outcome,
    update_v3_run,
)


class V3OrchestratorError(Exception):
    pass


def can_handle_v3(run):
    if not run:
        return False
    if not agent_run_allows_code(run["user_id"], run["id"]):
        return False
    try:
        return effective_runtime(run) == RUNTIME_NODE
    except Exception:
        return False


def _control(run):
    signal = legacy_runner._control_probe(run, include_pause=True, force=True)
    if signal == "pause":
        mark_agent_paused(run["user_id"], run["id"])
        return "pause"
    return None


def _fresh_run(run):
    return get_agent_run(run["user_id"], run["id"]) or run


def _budget_available(run):
    return int(run.get("current_step") or 0) < int(run.get("max_steps") or 6)


def _phase(run, phase, reason, function, *, action=None, tool_name=None, input_data=None):
    run = _fresh_run(run)
    if _control(run) == "pause":
        return {"paused": True}
    tail_grant = None
    if not _budget_available(run):
        tail_grant = maybe_grant_tail_budget(run, phase)
        run = _fresh_run(run)
        if not _budget_available(run):
            detail = str((tail_grant or {}).get("reason") or "no bounded progress tail was authorized")
            raise V3OrchestratorError(
                f"Step budget was exhausted before phase {phase}; {detail}. "
                "Resume/Revise can grant more budget without losing the workspace."
            )
        if (tail_grant or {}).get("granted"):
            record_phase_event(
                run,
                "execution_governance",
                "tail_granted",
                (
                    f"Automatic progress tail granted {tail_grant.get('steps')} step(s) "
                    f"for phase {phase}: {tail_grant.get('reason')}. "
                    f"Ceiling {tail_grant.get('ceiling_before')} → {tail_grant.get('ceiling_after')}."
                ),
            )

    update_v3_run(run, status="running", phase=phase, started=True)
    started = time.monotonic()
    record_phase_event(run, phase, "started", reason)
    step = begin_agent_step(
        run["user_id"],
        run["id"],
        phase=phase,
        action=action or phase,
        tool_name=tool_name,
        reason=reason,
        input_data=input_data or {},
    )

    try:
        result = function(step)
        if isinstance(result, dict) and result.get("paused"):
            finish_agent_step(run["user_id"], step["id"], "interrupted", "Agent paused during this phase.")
            return result
        output = result.get("output") if isinstance(result, dict) and "output" in result else result
        finish_agent_step(run["user_id"], step["id"], "completed", str(output or "")[:18000])
        record_phase_event(
            run,
            phase,
            "completed",
            str(output or "")[:4000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result
    except Exception as error:
        finish_agent_step(run["user_id"], step["id"], "error", str(error)[:12000])
        record_phase_event(
            run,
            phase,
            "error",
            str(error)[:4000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        update_v3_run(run, status="error", phase=phase, last_error=str(error))
        raise


def _latest_node_execution(run):
    items = list_agent_sandbox_executions(run["user_id"], run["id"], limit=100)
    node = [item for item in items if str(item.get("runtime") or "").lower() == "node"]
    return node[-1] if node else None


def _committed_repair_count(run):
    """Count only repair steps that actually committed a validated change-set.

    v3.0.x incremented its repair counter before candidate validation, so a
    rejected model candidate could consume budget.  Deriving from completed
    repair steps repairs that historical accounting on resume.
    """
    count = 0
    for step in list_agent_steps(run["user_id"], run["id"]):
        if str(step.get("action") or "") != "repair":
            continue
        if str(step.get("status") or "") != "completed":
            continue
        output = str(step.get("output") or "").lower()
        if "committed" in output or "changed files:" in output:
            count += 1
    return count


def _repair_outcomes_with_legacy_seed(run):
    outcomes = list_repair_outcomes(run["user_id"], run["id"], limit=100)
    if outcomes:
        return outcomes

    # Upgrade path for v3.0.x runs: when no v3.1 outcome rows exist yet, use
    # the latest two authoritative Node executions as a one-item progress seed.
    # This lets a run that was clearly improving continue instead of being
    # blocked merely because it predates the new governor table.
    executions = [
        item
        for item in list_agent_sandbox_executions(run["user_id"], run["id"], limit=100)
        if str(item.get("runtime") or "").lower() == "node"
    ]
    if len(executions) >= 2:
        progress = compare_evidence(executions[-2], executions[-1])
        return [{
            "progress_class": progress.get("classification"),
            "score_delta": progress.get("score_delta"),
            "before": progress.get("before"),
            "after": progress.get("after"),
            "detail": "Derived from pre-v3.1 sandbox execution history.",
        }]
    return []


def _verified_answer(spec, execution, acceptance):
    command = str(execution.get("command") or execution.get("filename") or "sandbox verification")
    tests = acceptance.get("test_names") or []
    return (
        "VERIFIED — The current workspace passed authoritative sandbox verification and ATLAS v3 goal acceptance.\n\n"
        f"Verification: {command}\n"
        f"Detected tests: {len(tests)}\n"
        "Goal acceptance: satisfied\n\n"
        f"ATLAS Agent Core {CORE_VERSION} finalized this result deterministically from stored execution/acceptance evidence."
    )


def _persist_verified_final(run, answer):
    # Calling through agent_runner preserves existing Agent Identity reflection
    # and Revision completion hooks installed by create_app(). No model finalizer.
    return legacy_runner._finish_with_final(
        run,
        {"answer": answer, "evidence": [], "artifacts": []},
    )


def _not_verified_failure(run, message):
    update_v3_run(run, status="failed", phase="failed", last_error=message)
    mark_agent_failed(run["user_id"], run["id"], message)
    # Keep revision provenance truthful.  A terminal v3 failure ends the
    # current revision segment; it must not remain "running" and block the
    # user's next Continue / Revise request.
    try:
        close_open_revision_for_terminal_run(
            run["user_id"],
            run["id"],
            error=message,
        )
    except Exception:
        pass


def execute_v3_agent_run(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        return
    ensure_v3_run(run)
    started = time.monotonic()
    runtime_limit = max(120, int(AGENT_MAX_RUNTIME_SECONDS))

    try:
        # ------------------------- STARTING -------------------------
        def starting(_step):
            current = _fresh_run(run)
            runtime = effective_runtime(current)
            files = list_workspace_files(user_id, run_id)
            return {
                "output": (
                    f"ATLAS Agent Core {CORE_VERSION} started.\n"
                    f"Native adapter: {runtime}.\n"
                    f"Existing workspace files: {len(files)}.\n"
                    "Execution lifecycle: spec → build → environment → verify → bounded repair → acceptance → deterministic final."
                )
            }

        result = _phase(
            run,
            "starting",
            "Establish the v3 execution lifecycle and inspect the current run/workspace before any model or mutation.",
            starting,
            action="starting",
        )
        if result.get("paused"):
            return

        run = _fresh_run(run)
        if time.monotonic() - started > runtime_limit:
            raise V3OrchestratorError("v3 runtime budget reached during startup.")

        # ---------------------- PROJECT SPEC ------------------------
        state = get_v3_run(user_id, run_id) or {}
        spec = state.get("spec") or {}
        if not spec:
            holder = {}

            def make_spec(_step):
                holder["spec"] = build_project_spec(_fresh_run(run), "node")
                update_v3_run(_fresh_run(run), spec=holder["spec"], phase="project_spec")
                return {"output": spec_summary(holder["spec"])}

            result = _phase(
                run,
                "project_spec",
                "Convert the user goal into one compact persistent execution specification before writing code.",
                make_spec,
                action="project_spec",
            )
            if result.get("paused"):
                return
            spec = holder["spec"]
        else:
            spec = dict(spec)

        # -------------------------- BUILD ---------------------------
        run = _fresh_run(run)
        if not list_workspace_files(user_id, run_id):
            holder = {}

            def do_build(_step):
                holder["build"] = bootstrap_project(_fresh_run(run), spec)
                changed = holder["build"].get("changed") or []
                preflight = holder["build"].get("preflight") or {}
                preflight_exec = preflight.get("execution") or {}
                lines = [
                    "Fresh project built as one coherent staged change-set.",
                    "Model: " + str(holder["build"].get("model") or "unknown"),
                    "Files created: " + ", ".join(item.get("filename") for item in changed),
                    "Candidate preflight: " + str(preflight_exec.get("status") or "structural-only")
                    + (" · " + str(preflight.get("detail")) if preflight.get("detail") else ""),
                ]
                return {"output": "\n".join(lines)}

            result = _phase(
                run,
                "build",
                "Construct the fresh Node/JavaScript project as one coherent initial change-set; BUILD is not final acceptance.",
                do_build,
                action="project_build",
            )
            if result.get("paused"):
                return

        # ----------------------- ENVIRONMENT ------------------------
        holder = {}

        def do_environment(_step):
            holder["env"] = ensure_environment(_fresh_run(run))
            status = holder["env"].get("status") or {}
            return {
                "output": (
                    "Node project environment ready.\n"
                    f"Setup performed: {'yes' if holder['env'].get('setup') else 'no/cache or base ready'}.\n"
                    f"Status: {status.get('status') or 'ready'}.\n"
                    f"Image: {status.get('execution_image') or status.get('image_tag') or 'base Node image'}."
                )
            }

        result = _phase(
            run,
            "environment",
            "Resolve the project's declared npm dependencies in the isolated setup boundary before execution.",
            do_environment,
            action="environment_setup",
            tool_name="docker/npm",
        )
        if result.get("paused"):
            return

        # -------------------------- VERIFY --------------------------
        verification = {}

        def do_verify(step):
            verification["execution"] = verify_project(_fresh_run(run), step_id=step["id"])
            from app.services.agent_sandbox import format_execution_observation
            return {"output": format_execution_observation(verification["execution"])}

        result = _phase(
            run,
            "verify",
            "Run one authoritative sandbox verification against the current coherent workspace.",
            do_verify,
            action="verify",
            tool_name="docker/node",
        )
        if result.get("paused"):
            return
        execution = verification["execution"]

        # If the user explicitly requested fail→repair demonstration and the
        # first project unexpectedly passed, create exactly one controlled
        # implementation defect then observe the requested real failure.
        if spec.get("requires_fail_then_repair"):
            executions = list_agent_sandbox_executions(user_id, run_id, limit=100)
            had_failure = any(
                str(item.get("runtime") or "").lower() == "node"
                and str(item.get("status") or "") in {"failed", "timeout"}
                for item in executions
            )
            if execution_passed(execution) and not had_failure:
                defect_holder = {}

                def do_defect(_step):
                    defect_holder["result"] = inject_intentional_defect(_fresh_run(run), spec)
                    return {
                        "output": (
                            "User-requested controlled fail→repair demonstration prepared.\n"
                            "Changed file: "
                            + ", ".join(item.get("filename") for item in defect_holder["result"].get("changed") or [])
                        )
                    }

                result = _phase(
                    run,
                    "intentional_defect",
                    "The goal explicitly requires one legitimate failing verification before repair; introduce one controlled implementation-only defect.",
                    do_defect,
                    action="intentional_defect",
                )
                if result.get("paused"):
                    return

                verification = {}
                result = _phase(
                    run,
                    "verify",
                    "Record the user-requested real sandbox failure before repair.",
                    do_verify,
                    action="verify",
                    tool_name="docker/node",
                )
                if result.get("paused"):
                    return
                execution = verification["execution"]
                if execution_passed(execution):
                    raise V3OrchestratorError(
                        "The goal required a real fail→repair demonstration, but the controlled defect did not produce a failing sandbox verification."
                    )

        # ---------------------- REPAIR / ACCEPT ---------------------
        acceptance = None
        committed_repairs = _committed_repair_count(_fresh_run(run))
        # Normalize the old v3.0.x counter to committed workspace mutations.
        update_v3_run(_fresh_run(run), repair_cycle=committed_repairs)

        while True:
            run = _fresh_run(run)
            if time.monotonic() - started > runtime_limit:
                raise V3OrchestratorError(
                    "ATLAS v3 runtime budget was reached. Resume the same run to continue from its stored spec/workspace/evidence."
                )

            if execution_passed(execution):
                acceptance_holder = {}

                def do_acceptance(_step):
                    acceptance_holder["value"] = evaluate_acceptance(_fresh_run(run), spec, execution)
                    current_run = _fresh_run(run)
                    update_v3_run(current_run, acceptance=acceptance_holder["value"], phase="acceptance")
                    record_acceptance_evaluation(current_run, acceptance_holder["value"])
                    return {"output": acceptance_summary(acceptance_holder["value"])}

                result = _phase(
                    run,
                    "acceptance",
                    "Evaluate the complete user goal only after authoritative execution succeeds; final acceptance is separate from BUILD validity.",
                    do_acceptance,
                    action="acceptance",
                )
                if result.get("paused"):
                    return
                acceptance = acceptance_holder["value"]

                if acceptance.get("satisfied"):
                    run = _fresh_run(run)
                    answer = _verified_answer(spec, execution, acceptance)

                    def do_final(_step):
                        return {"output": answer}

                    _phase(
                        run,
                        "finalize",
                        "Authoritative execution and all three acceptance layers passed; finalize deterministically without a model synthesis call.",
                        do_final,
                        action="final",
                    )
                    _persist_verified_final(_fresh_run(run), answer)
                    update_v3_run(_fresh_run(run), status="completed", phase="completed", acceptance=acceptance)
                    return

                # Acceptance issues owned by ATLAS runtime/platform policy or by
                # the acceptance service itself must never be routed into source
                # repair. Only project-owned repairable issues may authorize a
                # workspace mutation.
                if not (acceptance.get("repairable_issues") or []):
                    raise V3OrchestratorError(
                        "ATLAS v3 acceptance stopped without mutating the project because the remaining blocker is not project-repairable. "
                        + acceptance_summary(acceptance)
                    )

            outcomes = _repair_outcomes_with_legacy_seed(run)
            permission = repair_permission(committed_repairs, outcomes)
            if not permission.get("allowed"):
                issue_text = acceptance_summary(acceptance) if acceptance else "Latest sandbox verification is still failing."
                raise V3OrchestratorError(
                    "ATLAS v3 repair governor stopped this execution safely. "
                    + str(permission.get("reason") or "No further bounded repair was authorized.")
                    + " "
                    + issue_text
                )

            next_repair = committed_repairs + 1
            repair_holder = {}
            issues = []
            if acceptance:
                issues = list(acceptance.get("repairable_issues") or [])

            before_execution = execution

            def do_repair(_step):
                repair_holder["result"] = repair_project(
                    _fresh_run(run),
                    spec,
                    before_execution,
                    next_repair,
                    acceptance_issues=issues,
                    repair_history=outcomes,
                )
                changed = repair_holder["result"].get("changed") or []
                preflight = repair_holder["result"].get("preflight") or {}
                preflight_exec = preflight.get("execution") or {}
                return {
                    "output": (
                        f"Committed repair {next_repair}.\n"
                        f"Governor lane: {permission.get('lane')}.\n"
                        f"Model: {repair_holder['result'].get('model')}\n"
                        f"Hypothesis: {repair_holder['result'].get('hypothesis') or 'not supplied'}\n"
                        f"Staged candidate preflight: {preflight_exec.get('status') or 'structural-only'}"
                        + (f" · {preflight.get('detail')}" if preflight.get("detail") else "")
                        + "\nChanged files: " + ", ".join(item.get("filename") for item in changed)
                    )
                }

            result = _phase(
                run,
                "repair",
                (
                    f"Apply evidence-driven committed repair {next_repair}. "
                    f"Base tranche: {BASE_REPAIR_TRANCHE}; absolute safety ceiling: {ABSOLUTE_MAX_COMMITTED_REPAIRS}. "
                    "Rejected model candidates are internal retries and do not consume committed-repair budget."
                ),
                do_repair,
                action="repair",
            )
            if result.get("paused"):
                return

            # Only a successfully committed validated change-set consumes the
            # engineering repair budget.
            committed_repairs = next_repair
            update_v3_run(
                _fresh_run(run),
                status="running",
                phase="repair",
                repair_cycle=committed_repairs,
                latest_failure_fingerprint=failure_fingerprint(before_execution),
            )

            changed_names = {
                item.get("filename")
                for item in repair_holder["result"].get("changed") or []
            }
            if "package.json" in changed_names:
                result = _phase(
                    run,
                    "environment",
                    "package.json changed during repair; re-resolve the isolated npm environment before verification.",
                    do_environment,
                    action="environment_setup",
                    tool_name="docker/npm",
                )
                if result.get("paused"):
                    return

            verification = {}
            result = _phase(
                run,
                "verify",
                f"Authoritatively verify the workspace after committed repair {committed_repairs}.",
                do_verify,
                action="verify",
                tool_name="docker/node",
            )
            if result.get("paused"):
                return
            execution = verification["execution"]
            acceptance = None

            progress = compare_evidence(before_execution, execution)
            record_repair_outcome(
                _fresh_run(run),
                repair_number=committed_repairs,
                model=repair_holder["result"].get("model"),
                hypothesis=repair_holder["result"].get("hypothesis"),
                changed_files=[
                    item.get("filename")
                    for item in repair_holder["result"].get("changed") or []
                ],
                progress=progress,
            )
            record_phase_event(
                _fresh_run(run),
                "repair_progress",
                progress.get("classification") or "unknown",
                progress_summary(progress),
            )

            if progress.get("classification") == "regression":
                raise V3OrchestratorError(
                    "The latest committed repair caused a measurable regression. "
                    "ATLAS stopped before spending more repair budget; the file-version history preserves the prior workspace state for review/restore."
                )

    except legacy_runner.AgentCancelled:
        mark_agent_cancelled(user_id, run_id)
        update_v3_run(run, status="cancelled", phase="cancelled")
        try:
            close_open_revision_for_terminal_run(
                user_id,
                run_id,
                status="cancelled",
            )
        except Exception:
            pass
    except (V3NodeError, V3OrchestratorError, AgentSandboxError, AgentEnvironmentError, AgentStoreError) as error:
        _not_verified_failure(_fresh_run(run), str(error))
        return
