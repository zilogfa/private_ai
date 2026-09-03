"""
ATLAS v2.4.0 - transactional Agent execution integration.

Node/JavaScript code work no longer uses the v2.3.x one-file debug-plan loop.
The stable sandbox runner still owns security, lifecycle, pause/cancel, generic
research actions and finalization, while this module replaces the Node coding
control lane with bounded project transactions.
"""

from app.services import agent_execution_runner as runtime_runner
from app.services.agent_acceptance_contract import acceptance_summary
from app.services.agent_environment import project_environment_allowed
from app.services.agent_node_environment import (
    node_environment_needs_setup,
    node_manifest_needs_update,
)
from app.services.agent_runtime import RUNTIME_NODE, effective_runtime
from app.services.agent_project_intelligence import (
    analyze_project_state,
    project_planner_context,
)
from app.services.agent_node_transaction import (
    AgentNodeTransactionError,
    execute_node_transaction_cycle,
    transaction_budget_exhausted,
    transaction_status,
)


_ORIGINAL_AVAILABLE_ACTIONS = runtime_runner._available_actions
_ORIGINAL_PLAN_NEXT_ACTION = runtime_runner._plan_next_action
_ORIGINAL_EXECUTE_PROJECT_REPAIR = runtime_runner._execute_project_repair
_ORIGINAL_PROJECT_CONTEXT = runtime_runner._project_context
_ORIGINAL_SANDBOX_FORCED_FINAL = runtime_runner._sandbox_forced_final
_ORIGINAL_LATEST_EXECUTION_STATE = runtime_runner._latest_execution_state


def _available_actions(run):
    actions = list(_ORIGINAL_AVAILABLE_ACTIONS(run))
    if (
        effective_runtime(run) == RUNTIME_NODE
        and runtime_runner._code_enabled(run)
        and "project_repair" not in actions
    ):
        actions.append("project_repair")
    return actions


def _project_context(run):
    if effective_runtime(run) == RUNTIME_NODE:
        try:
            status = transaction_status(run)
            return (
                project_planner_context(run)
                + "\n\nTRANSACTIONAL EXECUTION GOVERNANCE:\n"
                + f"Bounded project transactions used: {status['used']}/{status['limit']} "
                + f"for revision {status['revision_number']}."
            )[:18000]
        except Exception as error:
            return (
                "Project kind: Node.js\n"
                "Transactional Project Intelligence context unavailable: "
                + str(error)
            )[:5000]
    return _ORIGINAL_PROJECT_CONTEXT(run)


def _execute_project_repair(run):
    if effective_runtime(run) == RUNTIME_NODE:
        return execute_node_transaction_cycle(run)
    return _ORIGINAL_EXECUTE_PROJECT_REPAIR(run)


def _node_transaction_action(run):
    available = _available_actions(run)
    current_step = int(run.get("current_step") or 0)
    remaining_steps = max(
        0,
        int(run.get("max_steps") or 6) - current_step,
    )
    if remaining_steps <= 0:
        return None

    if project_environment_allowed(run["user_id"], run["id"]):
        if node_manifest_needs_update(run["user_id"], run["id"]):
            return {
                "action": "environment_plan",
                "reason": (
                    "The Node dependency manifest must match the current source before "
                    "starting a bounded project transaction."
                ),
                "model": "deterministic",
            }
        if node_environment_needs_setup(run["user_id"], run["id"]):
            return {
                "action": "environment_setup",
                "reason": (
                    "Build or reuse the isolated Node dependency environment before "
                    "starting a bounded project transaction."
                ),
                "model": "deterministic",
            }

    # External/manual workspace mutations still require a normal deterministic
    # re-test before ATLAS creates another transaction.
    retest = runtime_runner._required_retest_action(run)
    if retest and retest.get("action") in available:
        return {
            **retest,
            "reason": (
                "The workspace changed outside the latest project transaction. Re-test "
                "the current revision once before another transactional change-set."
            ),
            "model": "deterministic",
        }

    analysis = analyze_project_state(run)
    execution = analysis.get("execution") or {}
    latest = execution.get("latest")
    execution_verified = bool(
        latest
        and str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
    )
    acceptance = analysis.get("acceptance") or {}
    if execution_verified and acceptance.get("satisfied", True):
        return None

    if transaction_budget_exhausted(run):
        status = transaction_status(run)
        return {
            "action": "final",
            "reason": (
                "The bounded project transaction budget is exhausted for this revision. "
                "Stop instead of repeating plan/repair loops and report the remaining "
                "sandbox or acceptance blocker clearly."
            ),
            "model": "deterministic",
            "transaction_limit": status["limit"],
        }

    if "project_repair" in available:
        status = transaction_status(run)
        return {
            "action": "project_repair",
            "reason": (
                "Execute one bounded transactional Node/JavaScript engineering cycle: "
                "analyze the current project once, stage a coherent multi-file change-set, "
                "validate it before mutation, commit it together, and run authoritative "
                "sandbox verification once."
            ),
            "model": "transactional",
            "transaction_cycle": status["next_cycle"],
            "transaction_limit": status["limit"],
        }

    return None


def _plan_next_action(run):
    if effective_runtime(run) == RUNTIME_NODE:
        deterministic = _node_transaction_action(run)
        if deterministic:
            return deterministic
    return _ORIGINAL_PLAN_NEXT_ACTION(run)


def _sandbox_forced_final(run):
    if effective_runtime(run) == RUNTIME_NODE:
        try:
            state = _ORIGINAL_LATEST_EXECUTION_STATE(run)
            if state.get("verified"):
                analysis = analyze_project_state(run)
                acceptance = analysis.get("acceptance") or {}
                if not acceptance.get("satisfied", True):
                    answer = (
                        "NOT VERIFIED — The latest sandbox validation passed, but the "
                        "goal-level acceptance contract is still incomplete. A green test "
                        "result cannot override missing requested deliverables or coverage.\n\n"
                        + acceptance_summary(acceptance)
                        + "\n\nUse Continue / Revise to continue the same workspace and satisfy the remaining acceptance requirements."
                    )
                    return runtime_runner.base_runner._finish_with_final(
                        run,
                        {"answer": answer, "evidence": [], "artifacts": []},
                    )
        except Exception:
            pass
    return _ORIGINAL_SANDBOX_FORCED_FINAL(run)


runtime_runner._available_actions = _available_actions
runtime_runner._project_context = _project_context
runtime_runner._execute_project_repair = _execute_project_repair
runtime_runner._plan_next_action = _plan_next_action
runtime_runner._sandbox_forced_final = _sandbox_forced_final
runtime_runner.project_planner_context = project_planner_context


def execute_agent_run(user_id, run_id):
    return runtime_runner.execute_agent_run(user_id, run_id)
