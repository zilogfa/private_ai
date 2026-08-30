import os
import threading

from app.services.automation_runner import (
    AutomationCancelled,
    AutomationNeedsInput,
    execute_automation_task,
)
from app.services.automation_store import (
    claim_next_task,
    finish_task_cancelled,
    finish_task_needs_input,
    finish_task_run,
    is_task_cancel_requested,
    initialize_automation_storage,
    recover_stale_tasks,
)
from app.services.notifications import (
    initialize_notification_storage,
    send_notification,
)


ENGINE_ENABLED = (
    os.environ.get(
        "PRIVATE_AI_AUTOMATIONS_ENABLED",
        "1",
    )
    != "0"
)
ENGINE_POLL_SECONDS = max(
    2,
    int(
        os.environ.get(
            "PRIVATE_AI_AUTOMATION_POLL_SECONDS",
            "10",
        )
    ),
)
MAX_CONSECUTIVE_FAILURES = max(
    1,
    int(
        os.environ.get(
            "PRIVATE_AI_AUTOMATION_MAX_FAILURES",
            "5",
        )
    ),
)


_ENGINE_LOCK = threading.Lock()
_ENGINE_WAKE = threading.Event()
_ENGINE_THREAD = None
_ENGINE_STATE = {
    "started": False,
    "running_task_id": None,
    "last_error": None,
}


def engine_status():
    with _ENGINE_LOCK:
        return {
            "enabled": ENGINE_ENABLED,
            "started": bool(
                _ENGINE_STATE[
                    "started"
                ]
            ),
            "running_task_id": (
                _ENGINE_STATE[
                    "running_task_id"
                ]
            ),
            "last_error": (
                _ENGINE_STATE[
                    "last_error"
                ]
            ),
            "poll_seconds": (
                ENGINE_POLL_SECONDS
            ),
        }


def wake_automation_engine():
    _ENGINE_WAKE.set()


def _set_engine_state(**updates):
    with _ENGINE_LOCK:
        _ENGINE_STATE.update(
            updates
        )


def _notify_task_result(task, execution):
    if not execution.get(
        "should_notify"
    ):
        return False

    send_notification(
        user_id=task["user_id"],
        title=execution.get(
            "notification_title"
        ) or task.get("title")
        or "Automation",
        body=execution.get(
            "notification_body"
        ) or execution.get("result")
        or "Automation completed.",
        source_type="automation_run",
        source_id=str(
            task["run_id"]
        ),
        level="success",
        metadata={
            "task_id": task["id"],
            "run_id": task["run_id"],
            "task_type": task["task_type"],
        },
    )
    return True


def _notify_task_needs_input(task, clarification):
    send_notification(
        user_id=task["user_id"],
        title=(
            f"Automation needs clarification: "
            f"{task.get('title') or 'Task'}"
        ),
        body=(
            str(clarification)[:5000]
            + "\n\nEdit and save the automation to resume future runs."
        ),
        source_type="automation_needs_input",
        source_id=str(
            task["run_id"]
        ),
        level="warning",
        metadata={
            "task_id": task["id"],
            "run_id": task["run_id"],
        },
    )


def _notify_task_failure(task, error):
    send_notification(
        user_id=task["user_id"],
        title=(
            f"Automation failed: "
            f"{task.get('title') or 'Task'}"
        ),
        body=str(error)[:5000],
        source_type="automation_failure",
        source_id=str(
            task["run_id"]
        ),
        level="error",
        metadata={
            "task_id": task["id"],
            "run_id": task["run_id"],
        },
    )


def _execute_claimed_task(task):
    _set_engine_state(
        running_task_id=task["id"],
        last_error=None,
    )

    try:
        execution = execute_automation_task(
            task
        )

        if is_task_cancel_requested(
            task["user_id"],
            task["id"],
            lock_token=task.get("lock_token"),
        ):
            raise AutomationCancelled(
                "Cancelled by user.",
                tool_log=execution.get("tool_log") or [],
            )

        notified = False

        try:
            notified = _notify_task_result(
                task,
                execution,
            )
        except Exception as error:
            # The automation result remains valid even if the notification
            # layer has a transient local database failure.
            _set_engine_state(
                last_error=(
                    "Notification error: "
                    + str(error)
                )
            )

        tool_log = list(
            execution.get("tool_log")
            or []
        )

        if notified:
            tool_log.append({
                "tool": "notification.in_app",
            })

        finish_task_run(
            task,
            success=True,
            result=execution.get(
                "result"
            ),
            condition_met=execution.get(
                "condition_met"
            ),
            condition_key=execution.get(
                "condition_key"
            ),
            notified=notified,
            tool_log=tool_log,
            max_failures=(
                MAX_CONSECUTIVE_FAILURES
            ),
        )

    except AutomationCancelled as error:
        finish_task_cancelled(
            task,
            reason=str(error),
            tool_log=(
                getattr(error, "tool_log", [])
                or []
            ),
        )

        _set_engine_state(
            last_error=None
        )

    except AutomationNeedsInput as error:
        message = str(error)

        try:
            _notify_task_needs_input(
                task,
                message,
            )
            notified = True
        except Exception:
            notified = False

        tool_log = list(
            getattr(error, "tool_log", [])
            or []
        )

        if notified:
            tool_log.append({
                "tool": "notification.in_app"
            })

        finish_task_needs_input(
            task,
            clarification=message,
            notified=notified,
            tool_log=tool_log,
        )

        _set_engine_state(
            last_error=None
        )

    except Exception as error:
        # Catch unexpected executor failures too so one bad task never kills
        # the background scheduler thread.
        message = str(error)

        try:
            _notify_task_failure(
                task,
                message,
            )
            notified = True
        except Exception:
            notified = False

        failure_tools = (
            [{"tool": "notification.in_app"}]
            if notified
            else []
        )

        finish_task_run(
            task,
            success=False,
            error=message,
            notified=notified,
            tool_log=failure_tools,
            max_failures=(
                MAX_CONSECUTIVE_FAILURES
            ),
        )

        _set_engine_state(
            last_error=message[:1000]
        )

    finally:
        _set_engine_state(
            running_task_id=None
        )


def _engine_loop():
    while True:
        try:
            task = claim_next_task()

            if task:
                _execute_claimed_task(
                    task
                )
                continue

        except Exception as error:
            _set_engine_state(
                last_error=str(error)[:1000]
            )

        _ENGINE_WAKE.wait(
            ENGINE_POLL_SECONDS
        )
        _ENGINE_WAKE.clear()


def start_automation_engine():
    global _ENGINE_THREAD

    initialize_automation_storage()
    initialize_notification_storage()

    if not ENGINE_ENABLED:
        return False

    with _ENGINE_LOCK:
        if (
            _ENGINE_THREAD is not None
            and _ENGINE_THREAD.is_alive()
        ):
            return True

        # A fresh worker owns no previous run locks. Recover any persisted
        # running/cancelling state left by a prior process before starting.
        recover_stale_tasks()

        _ENGINE_THREAD = threading.Thread(
            target=_engine_loop,
            name="private-ai-automation-engine",
            daemon=True,
        )
        _ENGINE_THREAD.start()
        _ENGINE_STATE[
            "started"
        ] = True

    return True
