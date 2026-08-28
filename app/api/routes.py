import json

from flask import (
    Blueprint,
    Response,
    jsonify,
    request,
    send_file,
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
    load_message_records,
    update_conversation_title,
    update_user_settings,
)

from app.services.attachments import (
    cleanup_conversation_files,
    create_attachment,
    delete_pending_attachment,
    get_attachment_path,
    get_attachments_by_ids,
    list_attachments_for_conversation,
    public_attachment_data,
)

from app.services.chat import (
    stream_chat,
)

from app.services.conversations import (
    clean_conversation_title,
    maybe_generate_conversation_title,
)

from app.services.markdown import (
    render_markdown,
)

from app.ui_preferences import (
    merge_accent_setting,
    normalize_accent_color,
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

    cleanup_conversation_files(
        user_id,
        conversation_id,
    )

    if not delete_conversation(
        conversation_id,
        user_id,
    ):
        return (
            jsonify({
                "error":
                    "conversation_delete_failed"
            }),
            500,
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

    messages = load_message_records(
        conversation_id,
        user_id,
        after_id=0,
    )

    attachments_by_message = (
        list_attachments_for_conversation(
            user_id,
            conversation_id,
        )
    )

    rendered_messages = []

    for message in messages:
        message_attachments = [
            public_attachment_data(
                attachment
            )
            for attachment in (
                attachments_by_message.get(
                    message["id"],
                    [],
                )
            )
        ]

        item = {
            "id":
                message["id"],
            "role":
                message["role"],
            "content":
                message["content"],
            "attachments":
                message_attachments,
        }

        if (
            message["role"]
            == "assistant"
        ):
            item[
                "rendered_html"
            ] = render_markdown(
                message["content"]
            )

        rendered_messages.append(
            item
        )

    return jsonify({
        "conversation_id":
            conversation_id,
        "messages":
            rendered_messages,
    })


# =========================================================
# ATTACHMENTS
# =========================================================

@api_bp.post("/attachments")
@permission_required("chat.use")
def upload_attachment():
    user_id = (
        get_current_user_id()
    )

    file_storage = (
        request.files.get("file")
    )

    if not file_storage:
        return (
            jsonify({
                "error": "file_required"
            }),
            400,
        )

    conversation_id = (
        request.form.get(
            "conversation_id"
        )
    )

    if conversation_id:
        try:
            conversation_id = int(
                conversation_id
            )

        except (ValueError, TypeError):
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

    else:
        conversation_id = None

    try:
        attachment = create_attachment(
            user_id=
                user_id,
            file_storage=
                file_storage,
            conversation_id=
                conversation_id,
        )

    except ValueError as error:
        return (
            jsonify({
                "error": str(error)
            }),
            400,
        )

    return (
        jsonify({
            "attachment":
                public_attachment_data(
                    attachment
                )
        }),
        201,
    )


@api_bp.delete(
    "/attachments/<attachment_id>"
)
@permission_required("chat.use")
def remove_pending_attachment(
    attachment_id,
):
    deleted = delete_pending_attachment(
        attachment_id,
        get_current_user_id(),
    )

    if not deleted:
        return (
            jsonify({
                "error":
                    "attachment_not_found_or_in_use"
            }),
            404,
        )

    return jsonify({
        "deleted": True,
        "attachment_id":
            attachment_id,
    })


@api_bp.get(
    "/attachments/<attachment_id>/content"
)
@permission_required("chat.use")
def attachment_content(
    attachment_id,
):
    attachment, path = (
        get_attachment_path(
            attachment_id,
            get_current_user_id(),
        )
    )

    if not attachment or not path:
        return (
            jsonify({
                "error":
                    "attachment_not_found"
            }),
            404,
        )

    response = send_file(
        path,
        mimetype=
            attachment["mime_type"],
        download_name=
            attachment["original_name"],
        as_attachment=False,
        conditional=True,
    )

    response.headers[
        "Cache-Control"
    ] = "private, max-age=3600"

    return response


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

    if "accent_color" in payload:
        accent_color = (
            normalize_accent_color(
                payload.get(
                    "accent_color"
                )
            )
        )

        if not accent_color:
            return (
                jsonify({
                    "error":
                        "invalid_accent_color"
                }),
                400,
            )

        current_settings = (
            get_user_settings(
                user_id
            )
            or {}
        )

        kwargs["extra"] = (
            merge_accent_setting(
                current_settings,
                accent_color,
            )
        )

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

    attachment_ids = (
        payload.get(
            "attachment_ids",
            [],
        )
        or []
    )

    if not isinstance(
        attachment_ids,
        list,
    ):
        return (
            jsonify({
                "error":
                    "invalid_attachment_ids"
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

    try:
        attachments = get_attachments_by_ids(
            user_id,
            attachment_ids,
            conversation_id=
                conversation_id,
        )

    except ValueError as error:
        return (
            jsonify({
                "error": str(error)
            }),
            400,
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
            attachments=
                attachments,
        ):
            yield _ndjson(
                event
            )

            if (
                event.get("type")
                == "response_complete"
            ):
                yield _ndjson({
                    "type":
                        "rendered_content",
                    "html":
                        render_markdown(
                            event.get(
                                "response",
                                "",
                            )
                        ),
                })

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
