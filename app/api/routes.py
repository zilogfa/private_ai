import json

from flask import (
    Blueprint,
    Response,
    jsonify,
    request,
    stream_with_context,
)

from app.auth import (
    get_current_user,
    get_current_user_id,
    login_required,
    permission_required,
)

from app.config import (
    VALID_MODEL_MODES,
)

from app.database import (
    conversation_belongs_to_user,
    create_conversation,
    delete_conversation,
    get_user_roles,
    get_user_settings,
    list_conversations,
    load_messages,
    update_conversation_title,
    update_user_settings,
)

from app.services.chat import (
    stream_chat,
)

from app.services.conversations import (
    clean_conversation_title,
    maybe_generate_conversation_title,
)


api_bp = Blueprint(
    "api",
    __name__,
    url_prefix="/api",
)


def _ndjson(event):
    return (
        json.dumps(
            event,
            ensure_ascii=False,
        )
        + "\n"
    )


# =========================================================
# HEALTH / CURRENT USER
# =========================================================

@api_bp.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "private_ai",
    })


@api_bp.get("/me")
@login_required
def me():
    user = get_current_user()

    roles = get_user_roles(
        user[0]
    )

    settings = get_user_settings(
        user[0]
    )

    return jsonify({
        "id": user[0],
        "username": user[1],
        "display_name": user[2],
        "primary_role": user[3],
        "status": user[4],
        "roles": [
            role[1]
            for role in roles
        ],
        "settings": settings,
    })


# =========================================================
# CONVERSATIONS
# =========================================================

@api_bp.get("/conversations")
@permission_required("chat.use")
def conversations():
    user_id = (
        get_current_user_id()
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

    limit = max(
        1,
        min(
            200,
            limit,
        ),
    )

    rows = list_conversations(
        user_id,
        limit=limit,
    )

    return jsonify({
        "conversations": [
            {
                "id": row[0],
                "title": row[1],
                "created_at": row[2],
            }
            for row in rows
        ]
    })


@api_bp.post("/conversations")
@permission_required("chat.use")
def create_chat():
    user_id = (
        get_current_user_id()
    )

    conversation_id = (
        create_conversation(
            user_id
        )
    )

    return (
        jsonify({
            "id":
                conversation_id,
            "title":
                "New Chat",
        }),
        201,
    )


@api_bp.patch(
    "/conversations/"
    "<int:conversation_id>"
)
@permission_required("chat.use")
def rename_conversation(
    conversation_id,
):
    user_id = (
        get_current_user_id()
    )

    if not conversation_belongs_to_user(
        conversation_id,
        user_id,
    ):
        return (
            jsonify({
                "error":
                    "conversation_not_found"
            }),
            404,
        )

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    title = (
        clean_conversation_title(
            payload.get(
                "title"
            )
        )
    )

    if not title:
        return (
            jsonify({
                "error":
                    "title_required"
            }),
            400,
        )

    if not update_conversation_title(
        conversation_id=
            conversation_id,
        user_id=
            user_id,
        title=
            title,
    ):
        return (
            jsonify({
                "error":
                    "conversation_not_found"
            }),
            404,
        )

    return jsonify({
        "id": conversation_id,
        "title": title,
    })


@api_bp.delete(
    "/conversations/"
    "<int:conversation_id>"
)
@permission_required("chat.use")
def remove_conversation(
    conversation_id,
):
    user_id = (
        get_current_user_id()
    )

    if not delete_conversation(
        conversation_id,
        user_id,
    ):
        return (
            jsonify({
                "error":
                    "conversation_not_found"
            }),
            404,
        )

    return jsonify({
        "deleted": True,
        "conversation_id":
            conversation_id,
    })


@api_bp.get(
    "/conversations/"
    "<int:conversation_id>/messages"
)
@permission_required("chat.use")
def conversation_messages(
    conversation_id,
):
    user_id = (
        get_current_user_id()
    )

    if not conversation_belongs_to_user(
        conversation_id,
        user_id,
    ):
        return (
            jsonify({
                "error":
                    "conversation_not_found"
            }),
            404,
        )

    messages = load_messages(
        conversation_id,
        user_id,
    )

    return jsonify({
        "conversation_id":
            conversation_id,
        "messages":
            messages,
    })


# =========================================================
# USER SETTINGS API
# =========================================================

@api_bp.get("/settings")
@login_required
def settings():
    return jsonify(
        get_user_settings(
            get_current_user_id()
        )
    )


@api_bp.post("/settings")
@login_required
def save_settings():
    user_id = (
        get_current_user_id()
    )

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    kwargs = {}

    if "default_model_mode" in payload:
        mode = (
            str(
                payload[
                    "default_model_mode"
                ]
            )
            .lower()
            .strip()
        )

        if mode not in VALID_MODEL_MODES:
            return (
                jsonify({
                    "error":
                        "invalid_model_mode"
                }),
                400,
            )

        kwargs[
            "default_model_mode"
        ] = mode

    if "show_thinking" in payload:
        kwargs[
            "show_thinking"
        ] = bool(
            payload[
                "show_thinking"
            ]
        )

    if "theme" in payload:
        theme = (
            str(
                payload["theme"]
            )
            .lower()
            .strip()
        )

        if theme not in {
            "system",
            "light",
            "dark",
        }:
            return (
                jsonify({
                    "error":
                        "invalid_theme"
                }),
                400,
            )

        kwargs["theme"] = theme

    if kwargs:
        update_user_settings(
            user_id,
            **kwargs,
        )

    return jsonify(
        get_user_settings(
            user_id
        )
    )


# =========================================================
# CHAT STREAM
# =========================================================

@api_bp.post("/chat/stream")
@permission_required("chat.use")
def chat_stream_api():
    user_id = (
        get_current_user_id()
    )

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    user_message = (
        str(
            payload.get(
                "message",
                "",
            )
        )
        .strip()
    )

    if not user_message:
        return (
            jsonify({
                "error":
                    "message_required"
            }),
            400,
        )

    conversation_id = (
        payload.get(
            "conversation_id"
        )
    )

    if conversation_id is None:
        conversation_id = (
            create_conversation(
                user_id
            )
        )

    else:
        try:
            conversation_id = int(
                conversation_id
            )

        except (
            ValueError,
            TypeError,
        ):
            return (
                jsonify({
                    "error":
                        "invalid_conversation"
                }),
                400,
            )

        if not conversation_belongs_to_user(
            conversation_id,
            user_id,
        ):
            return (
                jsonify({
                    "error":
                        "conversation_not_found"
                }),
                404,
            )

    user_settings = (
        get_user_settings(
            user_id
        )
        or {}
    )

    model_mode = (
        payload.get(
            "model_mode"
        )
        or user_settings.get(
            "default_model_mode",
            "auto",
        )
    )

    model_mode = (
        str(model_mode)
        .lower()
        .strip()
    )

    if model_mode not in VALID_MODEL_MODES:
        return (
            jsonify({
                "error":
                    "invalid_model_mode"
            }),
            400,
        )

    include_thinking = bool(
        user_settings.get(
            "show_thinking",
            True,
        )
    )

    @stream_with_context
    def generate():
        yield _ndjson({
            "type": "conversation",
            "conversation_id":
                conversation_id,
        })

        for event in stream_chat(
            user_id=
                user_id,
            conversation_id=
                conversation_id,
            user_message=
                user_message,
            model_mode=
                model_mode,
            include_thinking=
                include_thinking,
        ):
            yield _ndjson(
                event
            )

            if (
                event.get("type")
                == "response_complete"
            ):
                yield _ndjson({
                    "type": "status",
                    "status": "naming",
                    "label": "Naming chat...",
                })

                title = (
                    maybe_generate_conversation_title(
                        user_id=
                            user_id,
                        conversation_id=
                            conversation_id,
                        user_message=
                            user_message,
                        assistant_response=
                            event.get(
                                "response",
                                "",
                            ),
                    )
                )

                if title:
                    yield _ndjson({
                        "type":
                            "conversation_title",
                        "conversation_id":
                            conversation_id,
                        "title":
                            title,
                    })

    response = Response(
        generate(),
        content_type=
            (
                "application/x-ndjson; "
                "charset=utf-8"
            ),
    )

    response.headers[
        "Cache-Control"
    ] = "no-cache, no-transform"

    response.headers[
        "X-Accel-Buffering"
    ] = "no"

    return response
