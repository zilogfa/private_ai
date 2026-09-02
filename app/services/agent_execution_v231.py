"""
ATLAS v2.3.1 - Project Intelligence execution integration.

This module composes the stable multi-runtime execution runner with the shared
Project Intelligence facade. The legacy runner remains the hardened sandbox/
verification implementation; this layer injects language-aware planning before
the generic model controller is allowed to make another speculative repair.

Keeping this integration thin reduces regression risk while v2.3.1 establishes
the common Project Intelligence boundary. A later cleanup milestone can fold the
hooks into a first-class controller interface once Python + Node behavior has
been validated in production.
"""

from app.services import agent_execution_runner as runtime_runner
from app.services.agent_environment import (
    dependency_manifest_needs_update,
    environment_needs_setup,
    project_environment_allowed,
)
from app.services.agent_node_environment import (
    node_environment_needs_setup,
    node_manifest_needs_update,
)
from app.services.agent_runtime import (
    RUNTIME_NODE,
    RUNTIME_PYTHON,
    effective_runtime,
)
from app.services.agent_project_intelligence import (
    active_plan_blocks_on_environment,
    active_plan_matches_current_failure,
    analyze_project_state,
    create_debug_plan,
    execute_project_repair,
    format_debug_plan,
    get_active_debug_plan,
    get_next_project_repair,
    mark_active_plan_exhausted,
    mark_active_plan_resolved,
    mark_active_plan_superseded,
    project_planner_context,
    should_create_debug_plan,
    structured_planner_exhausted_for_current_failure,
)


_ORIGINAL_AVAILABLE_ACTIONS = runtime_runner._available_actions
_ORIGINAL_PLAN_NEXT_ACTION = runtime_runner._plan_next_action
_ORIGINAL_EXECUTE_PROJECT_PLAN = runtime_runner._execute_project_plan
_ORIGINAL_EXECUTE_PROJECT_REPAIR = runtime_runner._execute_project_repair
_ORIGINAL_PROJECT_CONTEXT = runtime_runner._project_context


def _available_actions(run):
    actions = list(
        _ORIGINAL_AVAILABLE_ACTIONS(
            run
        )
    )

    if (
        effective_runtime(run)
        == RUNTIME_NODE
        and runtime_runner._code_enabled(run)
    ):
        for name in (
            "project_plan",
            "project_repair",
        ):
            if name not in actions:
                actions.append(name)

    return actions


def _project_context(run):
    if effective_runtime(run) == RUNTIME_NODE:
        try:
            return project_planner_context(
                run
            )
        except Exception as error:
            # Project Intelligence should improve the controller, never make the
            # whole controller unavailable because static analysis had a defect.
            return (
                "Project kind: Node.js\n"
                "Project Intelligence context unavailable: "
                + str(error)
            )[:5000]

    return _ORIGINAL_PROJECT_CONTEXT(
        run
    )


def _execute_project_plan(run):
    plan = create_debug_plan(
        run
    )
    return format_debug_plan(
        plan
    )


def _execute_project_repair(run):
    return execute_project_repair(
        run
    )


def _node_project_action(run):
    """
    Deterministic Node planning gate.

    Environment readiness and mandatory post-mutation verification still take
    priority. Only after the current dependency/runtime state is valid do we
    inspect persistent project intelligence and decide whether to repair, plan,
    or let the normal controller continue.
    """
    available = _available_actions(
        run
    )
    current_step = int(
        run.get("current_step")
        or 0
    )
    remaining = max(
        0,
        int(
            run.get("max_steps")
            or 6
        )
        - current_step,
    )

    if remaining <= 0:
        return None

    if (
        project_environment_allowed(
            run["user_id"],
            run["id"],
        )
        and "environment_plan" in available
    ):
        if node_manifest_needs_update(
            run["user_id"],
            run["id"],
        ):
            return {
                "action": "environment_plan",
                "reason": (
                    "The current Node project source shows an undeclared npm "
                    "dependency. Update package.json before project debugging."
                ),
                "model": "deterministic",
            }

    if (
        project_environment_allowed(
            run["user_id"],
            run["id"],
        )
        and "environment_setup" in available
        and node_environment_needs_setup(
            run["user_id"],
            run["id"],
        )
    ):
        return {
            "action": "environment_setup",
            "reason": (
                "Build or reuse the isolated npm dependency image before "
                "analyzing another project repair."
            ),
            "model": "deterministic",
        }

    retest = runtime_runner._required_retest_action(
        run
    )
    if (
        retest
        and retest.get("action")
        in available
    ):
        return {
            **retest,
            "reason": (
                "Re-test the current Node workspace revision before another "
                "repair. Sandbox execution is the authoritative verifier."
            ),
            "model": "deterministic",
        }

    analysis = analyze_project_state(
        run
    )
    execution = analysis[
        "execution"
    ]
    latest = execution.get(
        "latest"
    )

    if (
        latest
        and str(
            latest.get("status")
            or ""
        )
        == "success"
        and int(
            latest.get("exit_code")
            or 0
        )
        == 0
    ):
        mark_active_plan_resolved(
            run["user_id"],
            run["id"],
        )
        return None

    active_plan = get_active_debug_plan(
        run["user_id"],
        run["id"],
    )
    current_failure_fingerprint = (
        execution[
            "failure"
        ].get(
            "fingerprint"
        )
    )

    if (
        active_plan
        and latest
        and not active_plan_matches_current_failure(
            run,
            current_failure_fingerprint,
        )
    ):
        # Failure changed after a repair: that is progress. The remaining repair
        # assumptions from the old fingerprint are stale.
        mark_active_plan_superseded(
            run["user_id"],
            run["id"],
        )
        active_plan = None

    planned_repair = get_next_project_repair(
        run["user_id"],
        run["id"],
    )

    if (
        planned_repair
        and latest
        and "project_repair"
        in available
    ):
        return {
            "action": "project_repair",
            "reason": (
                "Follow the persistent Node/JavaScript project-contract repair "
                "plan instead of making another unstructured rewrite."
            ),
            "model": (
                planned_repair[
                    "plan"
                ][
                    "planner_model"
                ]
            ),
            "plan_id": (
                planned_repair[
                    "plan"
                ][
                    "id"
                ]
            ),
            "repair_index": (
                planned_repair[
                    "repair_index"
                ]
            ),
            "filename": (
                planned_repair[
                    "repair"
                ].get(
                    "file"
                )
            ),
        }

    if (
        active_plan
        and latest
        and not planned_repair
        and not (
            active_plan[
                "plan"
            ].get(
                "blocked_by_environment"
            )
        )
    ):
        mark_active_plan_exhausted(
            run["user_id"],
            run["id"],
        )

    if (
        latest
        and structured_planner_exhausted_for_current_failure(
            run,
            analysis,
        )
    ):
        return {
            "action": "final",
            "reason": (
                "The structured planner already attempted multiple recovery "
                "plans against the same unchanged failure. Report the exact "
                "blocker instead of burning more steps on the same hypothesis."
            ),
            "model": "deterministic",
        }

    if (
        latest
        and should_create_debug_plan(
            run,
            analysis,
        )
        and "project_plan"
        in available
    ):
        return {
            "action": "project_plan",
            "reason": (
                "Build a persistent Node/JavaScript project contract and recovery "
                "plan from current source, test expectations, package.json, and "
                "the actual sandbox failure before another code mutation."
            ),
            "model": "adaptive",
        }

    if (
        active_plan_blocks_on_environment(
            run["user_id"],
            run["id"],
        )
        and not project_environment_allowed(
            run["user_id"],
            run["id"],
        )
    ):
        return {
            "action": "final",
            "reason": (
                "The Node project plan identified a sandbox dependency blocker. "
                "Preserve the requested architecture and report the limitation."
            ),
            "model": "deterministic",
        }

    return None


def _plan_next_action(run):
    if (
        effective_runtime(run)
        == RUNTIME_NODE
    ):
        deterministic = _node_project_action(
            run
        )
        if deterministic:
            return deterministic

    return _ORIGINAL_PLAN_NEXT_ACTION(
        run
    )


# Install the v2.3.1 composition hooks once at import time. The stable execution
# loop continues to own sandboxing, verification tails, pause/cancel behavior,
# environment actions and finalization.
runtime_runner._available_actions = _available_actions
runtime_runner._project_context = _project_context
runtime_runner._execute_project_plan = _execute_project_plan
runtime_runner._execute_project_repair = _execute_project_repair
runtime_runner._plan_next_action = _plan_next_action
runtime_runner.project_planner_context = project_planner_context


def execute_agent_run(user_id, run_id):
    return runtime_runner.execute_agent_run(
        user_id,
        run_id,
    )
