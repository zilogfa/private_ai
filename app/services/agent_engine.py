import threading

from app.services.agent_runner import execute_agent_run
from app.services.agents import (
    get_agent_run,
    mark_agent_running,
)


_AGENT_THREADS = {}
_AGENT_THREADS_LOCK = threading.Lock()
_AGENT_CONCURRENCY = threading.Semaphore(1)


def _cleanup_thread(run_id):
    with _AGENT_THREADS_LOCK:
        _AGENT_THREADS.pop(str(run_id), None)


def _run_agent_thread(user_id, run_id):
    try:
        with _AGENT_CONCURRENCY:
            run = get_agent_run(user_id, run_id)
            if not run or run.get("state") != "queued":
                return

            if not mark_agent_running(user_id, run_id):
                return

            execute_agent_run(user_id, run_id)
    finally:
        _cleanup_thread(run_id)


def start_agent_run(user_id, run_id):
    """
    Start one persistent agent in a daemon thread.

    Only one heavy agent executes at a time in v1.9 to protect small unified-
    memory systems. Additional runs remain queued until the semaphore is free.
    """

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
    }
