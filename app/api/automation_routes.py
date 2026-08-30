from flask import (
    Blueprint,
    jsonify,
    request,
)

from app.auth import (
    get_current_user_id,
    permission_required,
)
from app.database import (
    user_has_permission,
)
from app.services.automation_engine import (
    engine_status,
    wake_automation_engine,
)
from app.services.automation_preflight import (
    AutomationPreflightError,
    review_task_definition,
)
from app.services.automation_store import (
    AutomationStoreError,
    create_task,
    delete_task,
    get_task,
    list_runs,
    list_tasks,
    request_manual_run,
    request_task_cancel,
    set_task_enabled,
    update_task,
)
from app.services.notifications import (
    count_unread_notifications,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)
from app.services.rag import (
    has_indexed_documents,
)


automation_api_bp = Blueprint(
    "automation_api",
    __name__,
    url_prefix="/api/automations",
)


def _payload():
    return (
        request.get_json(
            silent=True
        )
        or {}
    )


def _validate_external_capabilities(
    user_id,
    payload,
):
    if bool(
        payload.get("allow_web")
    ) and not user_has_permission(
        user_id,
        "web_search.use",
    ):
        raise AutomationStoreError(
            "This account does not have web-search permission."
        )

    if bool(
        payload.get("allow_memory")
    ) and not user_has_permission(
        user_id,
        "memory.manage_self",
    ):
        raise AutomationStoreError(
            "This account does not have memory permission."
        )


def _error_response(error, status=400):
    return (
        jsonify({
            "error": str(error)
        }),
        status,
    )


def _review_payload(payload):
    try:
        return review_task_definition(
            payload
        )
    except AutomationPreflightError as error:
        raise AutomationStoreError(
            str(error)
        ) from error


def _preflight_block_response(preflight):
    status = str(
        preflight.get("status")
        or "ready"
    )

    if status == "ready":
        return None

    message = (
        preflight.get("clarification")
        or preflight.get("summary")
        or "This automation needs attention before it can be activated."
    )

    return (
        jsonify({
            "error": str(message),
            "preflight": preflight,
        }),
        409,
    )


@automation_api_bp.post("/preflight")
@permission_required("automation.use")
def automation_preflight():
    user_id = get_current_user_id()
    payload = _payload()

    try:
        _validate_external_capabilities(
            user_id,
            payload,
        )
        preflight = _review_payload(
            payload
        )
    except AutomationStoreError as error:
        return _error_response(error)

    return jsonify({
        "preflight": preflight
    })


@automation_api_bp.get("/status")
@permission_required("automation.use")
def automation_status():
    user_id = get_current_user_id()

    return jsonify({
        "engine": engine_status(),
        "capabilities": {
            "web": user_has_permission(
                user_id,
                "web_search.use",
            ),
            "rag": has_indexed_documents(
                user_id
            ),
            "memory": user_has_permission(
                user_id,
                "memory.manage_self",
            ),
        },
        "unread_notifications": (
            count_unread_notifications(
                user_id
            )
        ),
    })


@automation_api_bp.get("/tasks")
@permission_required("automation.use")
def automation_tasks():
    return jsonify({
        "tasks": list_tasks(
            get_current_user_id()
        )
    })


@automation_api_bp.post("/tasks")
@permission_required("automation.use")
def create_automation_task():
    user_id = get_current_user_id()
    payload = _payload()

    try:
        _validate_external_capabilities(
            user_id,
            payload,
        )
        preflight = _review_payload(
            payload
        )
        blocked = _preflight_block_response(
            preflight
        )
        if blocked:
            return blocked

        task = create_task(
            user_id,
            payload,
            preflight=preflight,
        )
    except AutomationStoreError as error:
        return _error_response(error)

    wake_automation_engine()

    return (
        jsonify({
            "task": task,
            "preflight": preflight,
        }),
        201,
    )


@automation_api_bp.patch(
    "/tasks/<int:task_id>"
)
@permission_required("automation.use")
def edit_automation_task(task_id):
    user_id = get_current_user_id()
    payload = _payload()

    try:
        _validate_external_capabilities(
            user_id,
            payload,
        )
        preflight = _review_payload(
            payload
        )
        blocked = _preflight_block_response(
            preflight
        )
        if blocked:
            return blocked

        task = update_task(
            user_id,
            task_id,
            payload,
            preflight=preflight,
        )
    except AutomationStoreError as error:
        return _error_response(error)

    wake_automation_engine()
    return jsonify({
        "task": task,
        "preflight": preflight,
    })


@automation_api_bp.delete(
    "/tasks/<int:task_id>"
)
@permission_required("automation.use")
def remove_automation_task(task_id):
    user_id = get_current_user_id()

    try:
        deleted = delete_task(
            user_id,
            task_id,
        )
    except AutomationStoreError as error:
        return _error_response(error)

    if not deleted:
        return _error_response(
            "Automation task was not found.",
            404,
        )

    return jsonify({
        "deleted": True,
        "task_id": task_id,
    })


@automation_api_bp.post(
    "/tasks/<int:task_id>/enabled"
)
@permission_required("automation.use")
def toggle_automation_task(task_id):
    user_id = get_current_user_id()
    payload = _payload()

    if "enabled" not in payload:
        return _error_response(
            "enabled is required."
        )

    try:
        task = set_task_enabled(
            user_id,
            task_id,
            bool(payload.get("enabled")),
        )
    except AutomationStoreError as error:
        return _error_response(error)

    wake_automation_engine()
    return jsonify({
        "task": task
    })


@automation_api_bp.post(
    "/tasks/<int:task_id>/run"
)
@permission_required("automation.use")
def run_automation_task(task_id):
    user_id = get_current_user_id()

    try:
        task = request_manual_run(
            user_id,
            task_id,
        )
    except AutomationStoreError as error:
        return _error_response(error)

    wake_automation_engine()

    return (
        jsonify({
            "queued": True,
            "task": task,
        }),
        202,
    )


@automation_api_bp.post(
    "/tasks/<int:task_id>/cancel"
)
@permission_required("automation.use")
def cancel_automation_task(task_id):
    user_id = get_current_user_id()
    payload = _payload()

    try:
        task = request_task_cancel(
            user_id,
            task_id,
            pause_after=bool(
                payload.get("pause_after")
            ),
        )
    except AutomationStoreError as error:
        return _error_response(error)

    wake_automation_engine()

    return (
        jsonify({
            "cancel_requested": True,
            "task": task,
        }),
        202,
    )


@automation_api_bp.get("/runs")
@permission_required("automation.use")
def automation_runs():
    user_id = get_current_user_id()
    task_id = request.args.get(
        "task_id"
    )

    try:
        task_id = (
            int(task_id)
            if task_id
            else None
        )
        limit = int(
            request.args.get(
                "limit",
                50,
            )
        )
    except ValueError:
        return _error_response(
            "Invalid run query."
        )

    if (
        task_id is not None
        and not get_task(
            user_id,
            task_id,
        )
    ):
        return _error_response(
            "Automation task was not found.",
            404,
        )

    return jsonify({
        "runs": list_runs(
            user_id,
            task_id=task_id,
            limit=limit,
        )
    })


@automation_api_bp.get("/notifications")
@permission_required("automation.use")
def automation_notifications():
    user_id = get_current_user_id()
    unread_only = (
        str(
            request.args.get(
                "unread",
                "0",
            )
        ).lower()
        in {"1", "true", "yes"}
    )

    try:
        limit = int(
            request.args.get(
                "limit",
                50,
            )
        )
    except ValueError:
        limit = 50

    return jsonify({
        "notifications": (
            list_notifications(
                user_id,
                unread_only=
                    unread_only,
                limit=limit,
            )
        ),
        "unread_count": (
            count_unread_notifications(
                user_id
            )
        ),
    })


@automation_api_bp.post(
    "/notifications/<int:notification_id>/read"
)
@permission_required("automation.use")
def read_automation_notification(
    notification_id,
):
    user_id = get_current_user_id()
    payload = _payload()
    is_read = bool(
        payload.get(
            "is_read",
            True,
        )
    )

    changed = mark_notification_read(
        user_id,
        notification_id,
        is_read=is_read,
    )

    if not changed:
        return _error_response(
            "Notification was not found.",
            404,
        )

    return jsonify({
        "updated": True,
        "notification_id":
            notification_id,
        "is_read": is_read,
    })


@automation_api_bp.post(
    "/notifications/read-all"
)
@permission_required("automation.use")
def read_all_automation_notifications():
    count = mark_all_notifications_read(
        get_current_user_id()
    )

    return jsonify({
        "updated": count,
    })
