from flask import (
    Blueprint,
    jsonify,
    request,
)

from app.auth import (
    get_current_user_id,
    permission_required,
)
from app.services.agent_identity import (
    AGENT_MEMORY_PERMISSION,
    AgentIdentityError,
    add_agent_memory,
    archive_agent_memory,
    archive_agent_profile,
    create_agent_profile,
    ensure_default_agent_profile,
    get_agent_profile,
    list_agent_memories,
    list_agent_profiles,
    list_agent_reflections,
    update_agent_profile,
)


agent_identity_api_bp = Blueprint(
    "agent_identity_api",
    __name__,
    url_prefix="/api/agent-identities",
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


@agent_identity_api_bp.get(
    ""
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def identities():
    user_id = (
        get_current_user_id()
    )

    default_profile = (
        ensure_default_agent_profile(
            user_id
        )
    )

    return jsonify({
        "identities":
            list_agent_profiles(
                user_id
            ),
        "default_id":
            (
                default_profile[
                    "id"
                ]
                if default_profile
                else None
            ),
    })


@agent_identity_api_bp.post(
    ""
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def create_identity():
    try:
        profile = create_agent_profile(
            get_current_user_id(),
            _payload(),
        )
    except AgentIdentityError as error:
        return _error(
            error
        )

    return (
        jsonify({
            "identity":
                profile
        }),
        201,
    )


@agent_identity_api_bp.get(
    "/<profile_id>"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def identity_detail(
    profile_id,
):
    user_id = (
        get_current_user_id()
    )

    profile = get_agent_profile(
        user_id,
        profile_id,
    )

    if not profile:
        return _error(
            "Agent identity was not found.",
            404,
        )

    return jsonify({
        "identity":
            profile,
        "memories":
            list_agent_memories(
                user_id,
                profile_id,
                include_archived=True,
            ),
        "reflections":
            list_agent_reflections(
                user_id,
                profile_id,
            ),
    })


@agent_identity_api_bp.patch(
    "/<profile_id>"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def update_identity(
    profile_id,
):
    try:
        profile = update_agent_profile(
            get_current_user_id(),
            profile_id,
            _payload(),
        )
    except AgentIdentityError as error:
        return _error(
            error
        )

    return jsonify({
        "identity":
            profile
    })


@agent_identity_api_bp.delete(
    "/<profile_id>"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def archive_identity(
    profile_id,
):
    try:
        changed = archive_agent_profile(
            get_current_user_id(),
            profile_id,
        )
    except AgentIdentityError as error:
        return _error(
            error
        )

    if not changed:
        return _error(
            "Agent identity was not found.",
            404,
        )

    return jsonify({
        "archived":
            True,
        "id":
            profile_id,
    })


@agent_identity_api_bp.get(
    "/<profile_id>/memories"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def memories(
    profile_id,
):
    try:
        items = list_agent_memories(
            get_current_user_id(),
            profile_id,
            include_archived=(
                request.args.get(
                    "archived",
                    "0",
                )
                == "1"
            ),
        )
    except AgentIdentityError as error:
        return _error(
            error,
            404,
        )

    return jsonify({
        "memories":
            items
    })


@agent_identity_api_bp.post(
    "/<profile_id>/memories"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def add_memory(
    profile_id,
):
    payload = _payload()

    try:
        memory = add_agent_memory(
            get_current_user_id(),
            profile_id,
            payload.get(
                "content"
            ),
            category=
                payload.get(
                    "category",
                    "general",
                ),
            importance=
                payload.get(
                    "importance",
                    5,
                ),
            confidence=
                payload.get(
                    "confidence",
                    0.95,
                ),
            source=
                "manual",
        )
    except AgentIdentityError as error:
        return _error(
            error
        )

    return (
        jsonify({
            "memory":
                memory
        }),
        201,
    )


@agent_identity_api_bp.delete(
    "/memories/<int:memory_id>"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def archive_memory(
    memory_id,
):
    changed = archive_agent_memory(
        get_current_user_id(),
        memory_id,
    )

    if not changed:
        return _error(
            "Agent memory was not found.",
            404,
        )

    return jsonify({
        "archived":
            True,
        "memory_id":
            memory_id,
    })
