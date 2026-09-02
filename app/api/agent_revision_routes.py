from flask import (
    Blueprint,
    jsonify,
    request,
)

from app.auth import (
    get_current_user_id,
    permission_required,
)
from app.services.agent_engine import (
    start_agent_run,
)
from app.services.agent_revision import (
    AgentRevisionError,
    begin_user_revision,
    list_run_revisions,
)
from app.services.agent_stop_integrity import (
    clear_active_run_stop,
)


agent_revision_api_bp = Blueprint(
    "agent_revision_api",
    __name__,
)


def _payload():
    return (
        request.get_json(
            silent=True
        )
        or {}
    )


def _error(
    error,
    status=400,
):
    return (
        jsonify({
            "error":
                str(
                    error
                )
        }),
        status,
    )


@agent_revision_api_bp.get(
    "/api/agents/runs/<run_id>/revisions"
)
@permission_required(
    "agent.use"
)
def revisions(
    run_id,
):
    try:
        items = list_run_revisions(
            get_current_user_id(),
            run_id,
        )
    except AgentRevisionError as error:
        return _error(
            error,
            404,
        )

    return jsonify({
        "revisions":
            items
    })


@agent_revision_api_bp.post(
    "/api/agents/runs/<run_id>/revise"
)
@permission_required(
    "agent.use"
)
def revise(
    run_id,
):
    user_id = (
        get_current_user_id()
    )

    payload = _payload()

    try:
        result = begin_user_revision(
            user_id,
            run_id,
            payload.get(
                "feedback"
            ),
            extra_steps=
                payload.get(
                    "extra_steps",
                    12,
                ),
            learn_from_feedback=
                bool(
                    payload.get(
                        "learn_from_feedback",
                        True,
                    )
                ),
        )
        clear_active_run_stop(
            user_id,
            run_id,
        )
    except AgentRevisionError as error:
        return _error(
            error
        )

    start_agent_run(
        user_id,
        run_id,
    )

    return (
        jsonify(
            result
        ),
        202,
    )
