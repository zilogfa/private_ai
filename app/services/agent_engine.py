import threading

from app.services.agent_execution_v231 import execute_agent_run
from app.services.agents import get_agent_run, mark_agent_running
from app.services.agent_stop_integrity import (
    REPEATED_INTERNAL_FAILURE_THRESHOLD,
    finalize_run_stop_integrity,
    watch_agent_run_for_internal_failures,
)

_AGENT_THREADS = {}
_AGENT_THREADS_LOCK = threading.Lock()
_AGENT_CONCURRENCY = threading.Semaphore(1)


def _cleanup_thread(run_id):
    with _AGENT_THREADS_LOCK:
        _AGENT_THREADS.pop(str(run_id), None)


def _run_agent_thread(user_id, run_id):
    watchdog_stop = None
    watchdog = None

    try:
        with _AGENT_CONCURRENCY:
            run = get_agent_run(user_id, run_id)
            if not run or run.get("state") != "queued":
                return
            if not mark_agent_running(user_id, run_id):
                return

            watchdog_stop = threading.Event()
            watchdog = threading.Thread(
                target=watch_agent_run_for_internal_failures,
                args=(int(user_id), str(run_id), watchdog_stop),
                name=f"atlas-agent-watchdog-{str(run_id)[:8]}",
                daemon=True,
            )
            watchdog.start()

            try:
                execute_agent_run(user_id, run_id)
            finally:
                watchdog_stop.set()
                watchdog.join(timeout=1.0)
                try:
                    finalize_run_stop_integrity(user_id, run_id)
                except Exception:
                    # Stop-integrity metadata must never invalidate completed Agent work.
                    pass
    finally:
        if watchdog_stop is not None:
            watchdog_stop.set()
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
            name=f"private-ai-agent-{key[:8]}",
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
        "control_plane_stop_integrity": True,
        "repeated_internal_failure_threshold": REPEATED_INTERNAL_FAILURE_THRESHOLD,
        "project_intelligence": "v2.3.1",
        "node_deterministic_debug_planner": True,
    }
