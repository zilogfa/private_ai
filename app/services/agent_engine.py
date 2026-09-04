"""ATLAS v3 Agent Engine.

The engine owns concurrency and worker fault containment.  Native v3 coding
workloads use one explicit orchestrator.  Non-native workloads remain on the
stable v2.3 runner until their adapters are migrated, avoiding a flag-day rewrite.
"""

import threading
import traceback

from app.services.agent_execution_runner import execute_agent_run as execute_legacy_agent_run
from app.services.agent_v3_orchestrator import can_handle_v3, execute_v3_agent_run
from app.services.agent_v3_storage import CORE_VERSION
from app.services.agents import (
    finish_agent_step,
    get_agent_run,
    list_agent_steps,
    mark_agent_failed,
    mark_agent_running,
)

_AGENT_THREADS = {}
_AGENT_THREADS_LOCK = threading.Lock()
_AGENT_CONCURRENCY = threading.Semaphore(1)


def _cleanup_thread(run_id):
    with _AGENT_THREADS_LOCK:
        _AGENT_THREADS.pop(str(run_id), None)


def _close_open_step(user_id, run_id, error):
    try:
        steps = list_agent_steps(user_id, run_id)
    except Exception:
        return
    for step in reversed(steps):
        if str(step.get("status") or "") == "running":
            try:
                finish_agent_step(
                    user_id,
                    step["id"],
                    "error",
                    "ATLAS worker fault boundary caught an unexpected exception:\n" + str(error)[:10000],
                )
            except Exception:
                pass
            return


def _run_agent_thread(user_id, run_id):
    try:
        with _AGENT_CONCURRENCY:
            run = get_agent_run(user_id, run_id)
            if not run or run.get("state") != "queued":
                return
            if not mark_agent_running(user_id, run_id):
                return

            run = get_agent_run(user_id, run_id) or run
            try:
                if can_handle_v3(run):
                    execute_v3_agent_run(user_id, run_id)
                else:
                    execute_legacy_agent_run(user_id, run_id)
            except Exception as error:
                # No background thread may silently die while the database still
                # claims the Agent is running.  This is a permanent v3 invariant.
                detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:]
                _close_open_step(user_id, run_id, detail)
                current = get_agent_run(user_id, run_id)
                if current and current.get("state") in {"running", "pausing", "queued"}:
                    mark_agent_failed(
                        user_id,
                        run_id,
                        "ATLAS Agent worker failed unexpectedly.\n\n" + detail,
                    )
    finally:
        _cleanup_thread(run_id)


def start_agent_run(user_id, run_id):
    key = str(run_id)
    with _AGENT_THREADS_LOCK:
        existing = _AGENT_THREADS.get(key)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(
            target=_run_agent_thread,
            args=(int(user_id), key),
            name=f"atlas-agent-{key[:8]}",
            daemon=True,
        )
        _AGENT_THREADS[key] = thread
        thread.start()
        return True


def agent_engine_status():
    with _AGENT_THREADS_LOCK:
        alive = {
            key: thread
            for key, thread in _AGENT_THREADS.items()
            if thread.is_alive()
        }
    return {
        "active_threads": len(alive),
        "max_concurrent_heavy_runs": 1,
        "workspace_isolation": True,
        "host_code_execution": False,
        "sandboxed_python_execution": True,
        "sandboxed_node_execution": True,
        "agent_core": f"v{CORE_VERSION}",
        "native_v3_adapters": ["node"],
        "legacy_fallback": True,
        "worker_fault_boundary": True,
        "context_governor": True,
        "model_call_telemetry": True,
        "bounded_repair_cycles": True,
        "evidence_driven_repair_governor": True,
        "layered_acceptance_semantics": True,
        "platform_evidence_reconciliation": True,
        "repair_progress_extensions": True,
        "staged_candidate_validation": True,
        "precommit_sandbox_preflight": True,
        "automatic_progress_tail_budget": True,
        "unified_execution_governance": True,
        "revision_lifecycle_governance": True,
        "explicit_resume_continuation": True,
        "v3_diagnostics_storage": True,
        "deterministic_verified_finalization": True,
    }
