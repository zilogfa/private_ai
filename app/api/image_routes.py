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
    get_current_user_id,
    permission_required,
)

from app.config import (
    IMAGE_GENERATION_HEIGHT,
    IMAGE_GENERATION_MODEL,
    IMAGE_GENERATION_MODEL_LABEL,
    IMAGE_GENERATION_STEPS,
    IMAGE_GENERATION_WIDTH,
    SHOW_IMAGE_GENERATION_ACTIVITY,
)

from app.database import (
    conversation_belongs_to_user,
    create_conversation,
    save_message,
    user_has_permission,
)

from app.services.conversations import (
    maybe_generate_conversation_title,
)

from app.services.image_generation import (
    ImageGenerationError,
    format_generated_image_markdown,
    generate_local_image,
    get_generated_image_path,
    parse_image_command,
)

from app.services.markdown import (
    render_markdown,
)

from app.sessions import (
    maybe_update_session_summary,
)


image_api_bp = Blueprint(
    "image_api",
    __name__,
    url_prefix="/api/images",
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
# GENERATED IMAGE CONTENT
# =========================================================


@image_api_bp.get(
    "/<image_id>/content"
)
@permission_required(
    "image_generation.use"
)
def generated_image_content(
    image_id,
):
    path = get_generated_image_path(
        get_current_user_id(),
        image_id,
    )

    if not path:
        return (
            jsonify({
                "error":
                    "generated_image_not_found"
            }),
            404,
        )

    response = send_file(
        path,
        mimetype="image/png",
        download_name=(
            f"private_ai_{image_id}.png"
        ),
        as_attachment=False,
        conditional=True,
    )

    response.headers[
        "Cache-Control"
    ] = "private, max-age=86400"

    return response


# =========================================================
# CHAT INTERCEPTOR
# =========================================================


def register_image_chat_interceptor(app):
    """
    Intercept explicit /image chat requests before the normal LLM pipeline.

    Keeping this as a separate application extension lets image generation
    reuse the existing chat UI and conversation stream contract without
    coupling the diffusion runtime to app.services.chat.
    """

    @app.before_request
    def private_ai_image_chat_interceptor():
        if (
            request.method != "POST"
            or request.path
            != "/api/chat/stream"
        ):
            return None

        payload = (
            request.get_json(
                silent=True
            )
            or {}
        )

        user_message = str(
            payload.get(
                "message",
                "",
            )
        ).strip()

        try:
            image_prompt = (
                parse_image_command(
                    user_message
                )
            )

        except ImageGenerationError as error:
            return (
                jsonify({
                    "error": str(error)
                }),
                400,
            )

        if image_prompt is None:
            return None

        user_id = (
            get_current_user_id()
        )

        if not user_id:
            return (
                jsonify({
                    "error":
                        "authentication_required"
                }),
                401,
            )

        if not user_has_permission(
            user_id,
            "image_generation.use",
        ):
            return (
                jsonify({
                    "error":
                        "permission_denied",
                    "permission":
                        "image_generation.use",
                }),
                403,
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

        if attachment_ids:
            return (
                jsonify({
                    "error": (
                        "Image-to-image is not connected yet. "
                        "Use /image with a text prompt only in v1.6."
                    )
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

        @stream_with_context
        def generate():
            yield _ndjson({
                "type": "conversation",
                "conversation_id":
                    conversation_id,
            })

            yield _ndjson({
                "type": "status",
                "status": "preparing",
                "label": "Preparing image generation...",
            })

            save_message(
                conversation_id,
                user_id,
                "user",
                user_message,
            )

            maybe_update_session_summary(
                user_id,
                conversation_id,
            )

            yield _ndjson({
                "type": "route",
                "mode": "image",
                "model":
                    IMAGE_GENERATION_MODEL_LABEL,
            })

            if SHOW_IMAGE_GENERATION_ACTIVITY:
                yield _ndjson({
                    "type": "activity",
                    "phase": "generating_image",
                    "label": "Generating image locally...",
                    "detail": (
                        f"{IMAGE_GENERATION_WIDTH}×"
                        f"{IMAGE_GENERATION_HEIGHT} · "
                        f"{IMAGE_GENERATION_STEPS} steps"
                    ),
                })

            yield _ndjson({
                "type": "tool",
                "tool": "image.generate",
                "state": "start",
                "provider": "mflux",
                "model": IMAGE_GENERATION_MODEL,
            })

            try:
                generated = (
                    generate_local_image(
                        user_id,
                        image_prompt,
                    )
                )

            except ImageGenerationError as error:
                yield _ndjson({
                    "type": "error",
                    "kind":
                        "image_generation",
                    "message": str(error),
                })
                return

            response_markdown = (
                format_generated_image_markdown(
                    generated
                )
            )

            save_message(
                conversation_id,
                user_id,
                "assistant",
                response_markdown,
            )

            yield _ndjson({
                "type": "tool",
                "tool": "image.generate",
                "state": "done",
                "image_id":
                    generated["image_id"],
                "seed":
                    generated["seed"],
                "width":
                    generated["width"],
                "height":
                    generated["height"],
                "content_url":
                    generated["content_url"],
            })

            yield _ndjson({
                "type":
                    "response_complete",
                "mode": "image",
                "model":
                    IMAGE_GENERATION_MODEL_LABEL,
                "response":
                    response_markdown,
            })

            yield _ndjson({
                "type": "rendered_content",
                "html": render_markdown(
                    response_markdown
                ),
            })

            yield _ndjson({
                "type": "status",
                "status": "naming",
                "label": "Naming chat...",
            })

            title = (
                maybe_generate_conversation_title(
                    user_id=user_id,
                    conversation_id=
                        conversation_id,
                    user_message=
                        user_message,
                    assistant_response=(
                        "Generated a local image "
                        "from the requested prompt."
                    ),
                )
            )

            if title:
                yield _ndjson({
                    "type":
                        "conversation_title",
                    "conversation_id":
                        conversation_id,
                    "title": title,
                })

            yield _ndjson({
                "type": "done",
                "mode": "image",
                "model":
                    IMAGE_GENERATION_MODEL_LABEL,
                "response":
                    response_markdown,
            })

        response = Response(
            generate(),
            content_type=(
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
