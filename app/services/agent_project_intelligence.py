"""
ATLAS v2.3.1a - Shared Project Intelligence + Acceptance facade.

The execution controller should not care whether a project is Python, Node,
TypeScript, or a future language/runtime. It asks this layer for project state,
planning, repair, and planner lifecycle operations; language-specific analyzers
remain behind the facade.
"""

from app.services.agent_runtime import (
    RUNTIME_NODE,
    effective_runtime,
)
from app.services import agent_project_planner as python_planner
from app.services import agent_node_project_planner as node_planner
from app.services.agent_acceptance_contract import (
    acceptance_summary,
    evaluate_acceptance_contract,
)


def _is_node(run):
    return effective_runtime(run) == RUNTIME_NODE


def analyze_project_state(run):
    if _is_node(run):
        return node_planner.analyze_node_project_state(run)
    return python_planner.analyze_project_state(run)


def should_create_debug_plan(run, analysis=None):
    if _is_node(run):
        return node_planner.should_create_node_debug_plan(
            run,
            analysis,
        )
    return python_planner.should_create_debug_plan(
        run,
        analysis,
    )


def create_debug_plan(run, analysis=None):
    if _is_node(run):
        return node_planner.create_node_debug_plan(
            run,
            analysis,
        )
    return python_planner.create_debug_plan(
        run,
        analysis,
    )


def execute_project_repair(run):
    if _is_node(run):
        return node_planner.execute_node_project_repair(
            run
        )
    return python_planner.execute_project_repair(
        run
    )


def format_debug_plan(plan):
    project_kind = str(
        (plan.get("plan") or {})
        .get("project_kind")
        or ""
    ).lower()

    if project_kind == "node":
        return node_planner.format_node_debug_plan(
            plan
        )

    # Older Node plans do not carry project_kind. Trigger prefix is a safe
    # compatibility discriminator.
    if str(plan.get("trigger") or "").startswith("node_"):
        return node_planner.format_node_debug_plan(
            plan
        )

    return python_planner.format_debug_plan(
        plan
    )


def project_planner_context(run, analysis=None):
    if _is_node(run):
        return node_planner.node_project_planner_context(
            run,
            analysis,
        )
    return python_planner.project_planner_context(
        run,
        analysis,
    )


def get_active_debug_plan(user_id, run_id):
    return python_planner.get_active_debug_plan(
        user_id,
        run_id,
    )


def get_next_project_repair(user_id, run_id):
    return python_planner.get_next_project_repair(
        user_id,
        run_id,
    )


def active_plan_blocks_on_environment(user_id, run_id):
    return python_planner.active_plan_blocks_on_environment(
        user_id,
        run_id,
    )


def mark_active_plan_resolved(user_id, run_id):
    return python_planner.mark_active_plan_resolved(
        user_id,
        run_id,
    )


def mark_active_plan_exhausted(user_id, run_id):
    return python_planner.mark_active_plan_exhausted(
        user_id,
        run_id,
    )


def mark_active_plan_superseded(user_id, run_id):
    return python_planner.mark_active_plan_superseded(
        user_id,
        run_id,
    )


def active_plan_matches_current_failure(
    run,
    current_failure_fingerprint,
):
    if _is_node(run):
        return node_planner.node_active_plan_matches_current_failure(
            run["user_id"],
            run["id"],
            current_failure_fingerprint,
        )
    return python_planner.active_plan_matches_current_failure(
        run["user_id"],
        run["id"],
        current_failure_fingerprint,
    )


def structured_planner_exhausted_for_current_failure(
    run,
    analysis=None,
):
    if _is_node(run):
        return node_planner.structured_node_planner_exhausted_for_current_failure(
            run,
            analysis,
        )
    return python_planner.structured_planner_exhausted_for_current_failure(
        run,
        analysis,
    )


def acceptance_state(run, analysis=None):
    analysis = analysis or analyze_project_state(run)
    existing = analysis.get("acceptance")
    if existing is not None:
        return existing
    return evaluate_acceptance_contract(
        run,
        analysis.get("contract") or {},
        sandbox_verified=None,
    )


def acceptance_context(run, analysis=None):
    return acceptance_summary(
        acceptance_state(run, analysis)
    )
