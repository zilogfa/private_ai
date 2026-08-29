from app.config import (
    MEMORY_RETRIEVAL_LIMIT,
)

from app.ollama_client import (
    chat_stream,
    OllamaError,
    OllamaConnectionError,
)

from app.router import (
    route_model,
)

from app.database import (
    save_message,
    mark_memories_accessed,
    user_has_permission,
)

from app.memory import (
    retrieve_relevant_memories,
    process_automatic_memory,
)

from app.sessions import (
    maybe_update_session_summary,
    build_session_context,
)

from app.services.attachments import (
    bind_attachments_to_message,
)

from app.services.documents import (
    DOCUMENT_CONTEXT_SIZE,
    DOCUMENT_TEXT_BUDGET,
    SHOW_DOCUMENT_ACTIVITY,
    VISION_DOCUMENT_TEXT_BUDGET,
    build_document_context,
    list_document_attachments,
    prepare_document_attachments,
)

from app.services.vision import (
    MAX_VISION_IMAGES,
    SHOW_VISION_ACTIVITY,
    VISION_MODEL,
    VisionPreparationError,
    build_vision_messages,
    list_image_attachments,
    stream_vision_chat,
)

from app.services.web_research import (
    SHOW_WEB_ACTIVITY,
    WEB_CONTEXT_SIZE,
    WEB_TEXT_CONTEXT_BUDGET,
    WEB_VISION_CONTEXT_BUDGET,
    WebResearchError,
    build_private_search_query,
    build_web_context,
    format_sources_markdown,
    parse_web_command,
    research_direct_url,
    research_search_query,
    sources_event_data,
)


# =========================================================
# SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = (
    "You are one consistent private personal "
    "AI assistant. "

    "Respond naturally, clearly, and helpfully. "

    "Do not change personality based on the "
    "underlying model being used. "

    "Use relevant session context and long-term "
    "memory when available. "

    "Recent user statements take priority over "
    "older summaries or memories."
)


# =========================================================
# MEMORY CONTEXT
# =========================================================

def build_memory_context(
    relevant_memories,
):
    if not relevant_memories:
        return None

    memory_text = "\n".join(
        (
            f"- "
            f"[{memory['category']} | "
            f"importance "
            f"{memory['importance']} | "
            f"confidence "
            f"{memory['confidence']:.2f}] "
            f"{memory['content']}"
        )
        for memory
        in relevant_memories
    )

    return (
        "RELEVANT LONG-TERM MEMORY:\n"
        f"{memory_text}\n\n"

        "Use these memories naturally "
        "when helpful. "

        "Ignore irrelevant memories. "

        "Recent explicit user statements "
        "take priority over older memory."
    )


# =========================================================
# BUILD COMPLETE CHAT CONTEXT
# =========================================================

def build_chat_context(
    user_id,
    conversation_id,
    user_message,
):
    messages = build_session_context(
        user_id,
        conversation_id,
    )

    relevant_memories = (
        retrieve_relevant_memories(
            user_id,
            user_message,
            limit=MEMORY_RETRIEVAL_LIMIT,
        )
    )

    if relevant_memories:
        mark_memories_accessed(
            user_id,
            [
                memory["id"]
                for memory
                in relevant_memories
            ],
        )

        memory_context = (
            build_memory_context(
                relevant_memories
            )
        )

        if memory_context:
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": memory_context,
                },
            )

    messages.insert(
        0,
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
    )

    return messages


# =========================================================
# CONTEXT HELPERS
# =========================================================

def _insert_context_before_latest_user(
    messages,
    context,
):
    if not context:
        return messages

    insert_at = len(messages)

    for index in range(
        len(messages) - 1,
        -1,
        -1,
    ):
        if (
            messages[index].get(
                "role"
            )
            == "user"
        ):
            insert_at = index
            break

    messages.insert(
        insert_at,
        {
            "role": "system",
            "content": context,
        },
    )

    return messages


def _replace_latest_user_content(
    messages,
    content,
):
    for index in range(
        len(messages) - 1,
        -1,
        -1,
    ):
        if (
            messages[index].get(
                "role"
            )
            == "user"
        ):
            updated = dict(
                messages[index]
            )

            updated["content"] = (
                content
            )

            messages[index] = updated
            break

    return messages


def _combine_contexts(*contexts):
    parts = [
        str(context).strip()
        for context in contexts
        if str(context or "").strip()
    ]

    if not parts:
        return None

    return "\n\n".join(parts)


# =========================================================
# STREAM CHAT
# =========================================================

def stream_chat(
    user_id,
    conversation_id,
    user_message,
    model_mode=None,
    include_thinking=True,
    attachments=None,
):
    """
    Shared UI-independent chat pipeline.

    Web requests should pass model_mode explicitly.
    Terminal requests may leave it as None.

    Current attachment/tool routing:
        image -> local VLM
        DOCX/TXT/MD/CSV/JSON -> local text extraction
        text PDF -> local text extraction
        scanned/low-text PDF pages -> local VLM
        /web <query> -> local SearXNG + native page fetch
        /fetch <url> -> native public webpage fetch

    Standard event families:
        status
        activity
        tool
        sources
        route
        thinking
        content
        response_complete
        done
        error
    """

    attachments = list(
        attachments or []
    )

    try:
        web_mode, effective_user_message = (
            parse_web_command(
                user_message
            )
        )

    except WebResearchError as error:
        yield {
            "type": "error",
            "kind": "web_command",
            "message": str(error),
        }
        return

    if (
        web_mode
        and not user_has_permission(
            user_id,
            "web_search.use",
        )
    ):
        yield {
            "type": "error",
            "kind": "permission",
            "message": (
                "This account does not have web search permission."
            ),
        }
        return

    image_attachments = (
        list_image_attachments(
            attachments
        )
    )

    document_attachments = (
        list_document_attachments(
            attachments
        )
    )

    yield {
        "type": "status",
        "status": "preparing",
        "label": "Preparing...",
    }

    # -----------------------------------------------------
    # Save user message / bind attachments
    # -----------------------------------------------------

    user_message_id = save_message(
        conversation_id,
        user_id,
        "user",
        user_message,
    )

    if attachments:
        bind_attachments_to_message(
            user_id=
                user_id,
            conversation_id=
                conversation_id,
            message_id=
                user_message_id,
            attachment_ids=[
                attachment["id"]
                for attachment
                in attachments
            ],
        )

    # -----------------------------------------------------
    # Session summary maintenance
    # -----------------------------------------------------

    maybe_update_session_summary(
        user_id,
        conversation_id,
    )

    # -----------------------------------------------------
    # Build text/memory context
    # -----------------------------------------------------

    messages = build_chat_context(
        user_id,
        conversation_id,
        effective_user_message,
    )

    if web_mode:
        messages = (
            _replace_latest_user_content(
                messages,
                effective_user_message,
            )
        )

    # -----------------------------------------------------
    # Document extraction / scanned PDF rendering
    # -----------------------------------------------------

    document_result = None

    if document_attachments:
        if SHOW_DOCUMENT_ACTIVITY:
            yield {
                "type": "status",
                "status": "reading_document",
                "label": "Reading document...",
            }

        remaining_visual_slots = max(
            0,
            MAX_VISION_IMAGES
            - len(image_attachments),
        )

        document_result = (
            prepare_document_attachments(
                document_attachments,
                max_vision_pages=
                    remaining_visual_slots,
            )
        )

    document_vision_images = (
        list(
            (
                document_result
                or {}
            ).get(
                "vision_images",
                [],
            )
        )
    )

    using_vision = bool(
        image_attachments
        or document_vision_images
    )

    document_context = None

    if document_result:
        document_context = (
            build_document_context(
                document_result,
                max_chars=(
                    VISION_DOCUMENT_TEXT_BUDGET
                    if using_vision
                    else DOCUMENT_TEXT_BUDGET
                ),
            )
        )

    # -----------------------------------------------------
    # Explicit web tools
    # -----------------------------------------------------

    web_research = None
    web_context = None

    if web_mode:
        try:
            if web_mode == "search":
                search_query = (
                    build_private_search_query(
                        effective_user_message
                    )
                )

                if SHOW_WEB_ACTIVITY:
                    yield {
                        "type": "activity",
                        "phase": "searching",
                        "label": "Searching web...",
                        "detail": search_query,
                    }

                yield {
                    "type": "tool",
                    "tool": "web.search",
                    "state": "start",
                    "query": search_query,
                }

                web_research = (
                    research_search_query(
                        search_query
                    )
                )

                yield {
                    "type": "tool",
                    "tool": "web.search",
                    "state": "done",
                    "query": search_query,
                    "result_count": len(
                        web_research.get(
                            "sources"
                        )
                        or []
                    ),
                }

            else:
                if SHOW_WEB_ACTIVITY:
                    yield {
                        "type": "activity",
                        "phase": "reading",
                        "label": "Reading source...",
                        "detail": effective_user_message,
                    }

                yield {
                    "type": "tool",
                    "tool": "web.fetch",
                    "state": "start",
                    "url": effective_user_message,
                }

                web_research = (
                    research_direct_url(
                        effective_user_message
                    )
                )

                yield {
                    "type": "tool",
                    "tool": "web.fetch",
                    "state": "done",
                    "url": (
                        web_research
                        .get("query")
                    ),
                }

            if SHOW_WEB_ACTIVITY:
                yield {
                    "type": "activity",
                    "phase": "reading",
                    "label": "Reading sources...",
                    "detail": (
                        f"{len(web_research.get('sources') or [])} source(s)"
                    ),
                }

            yield {
                "type": "sources",
                "items": sources_event_data(
                    web_research
                ),
            }

            web_context = (
                build_web_context(
                    web_research,
                    max_chars=(
                        WEB_VISION_CONTEXT_BUDGET
                        if using_vision
                        else WEB_TEXT_CONTEXT_BUDGET
                    ),
                )
            )

        except WebResearchError as error:
            yield {
                "type": "error",
                "kind": "web_research",
                "message": str(error),
            }
            return

    combined_context = (
        _combine_contexts(
            document_context,
            web_context,
        )
    )

    # -----------------------------------------------------
    # Model routing
    # -----------------------------------------------------

    yield {
        "type": "status",
        "status": "routing",
        "label": "Routing...",
    }

    if using_vision:
        selected_mode = "vision"
        selected_model = VISION_MODEL

    else:
        selected_mode, selected_model = (
            route_model(
                effective_user_message,
                mode=model_mode,
            )
        )

    yield {
        "type": "route",
        "mode": selected_mode,
        "model": selected_model,
    }

    # -----------------------------------------------------
    # Prepare generation stream
    # -----------------------------------------------------

    if using_vision:
        if SHOW_VISION_ACTIVITY:
            if (
                document_vision_images
                and image_attachments
            ):
                vision_label = (
                    "Analyzing images and document pages..."
                )

            elif document_vision_images:
                vision_label = (
                    "Analyzing document pages..."
                )

            else:
                vision_label = (
                    "Analyzing image..."
                )

            yield {
                "type": "status",
                "status": "analyzing_visuals",
                "label": vision_label,
            }

        try:
            model_messages = (
                build_vision_messages(
                    base_messages=
                        messages,
                    image_attachments=
                        image_attachments,
                    other_attachments=(
                        (
                            document_result
                            or {}
                        ).get(
                            "unprocessed_attachments",
                            [],
                        )
                    ),
                    extra_images=
                        document_vision_images,
                    additional_context=
                        combined_context,
                )
            )

        except VisionPreparationError as error:
            yield {
                "type": "error",
                "kind": "vision_prepare",
                "message": str(error),
            }
            return

        generation_stream = (
            stream_vision_chat(
                model_messages,
                timeout=900,
            )
        )

    else:
        if combined_context:
            messages = (
                _insert_context_before_latest_user(
                    messages,
                    combined_context,
                )
            )

        yield {
            "type": "status",
            "status": "generating",
            "label": "Generating...",
        }

        options = None

        if (
            document_attachments
            or web_mode
        ):
            context_size = max(
                (
                    DOCUMENT_CONTEXT_SIZE
                    if document_attachments
                    else 0
                ),
                (
                    WEB_CONTEXT_SIZE
                    if web_mode
                    else 0
                ),
            )

            options = {
                "num_ctx":
                    context_size,
            }

        generation_stream = (
            chat_stream(
                model=selected_model,
                messages=messages,
                options=options,
                timeout=900,
            )
        )

    # -----------------------------------------------------
    # Generate response
    # -----------------------------------------------------

    full_response = ""
    full_thinking = ""

    thinking_started = False
    content_started = False

    try:
        for data in generation_stream:
            message_data = data.get(
                "message",
                {},
            )

            # ---------------------------------------------
            # Thinking stream
            # ---------------------------------------------

            thinking_chunk = (
                message_data.get(
                    "thinking",
                    "",
                )
            )

            if thinking_chunk:
                if include_thinking:
                    if not thinking_started:
                        yield {
                            "type": "status",
                            "status": "thinking",
                            "label": "Thinking...",
                        }

                        thinking_started = True

                    full_thinking += (
                        thinking_chunk
                    )

                    yield {
                        "type": "thinking",
                        "content": thinking_chunk,
                    }

            # ---------------------------------------------
            # Final answer stream
            # ---------------------------------------------

            content_chunk = (
                message_data.get(
                    "content",
                    "",
                )
            )

            if content_chunk:
                if not content_started:
                    yield {
                        "type": "status",
                        "status": "responding",
                        "label": "Responding...",
                    }

                    content_started = True

                full_response += (
                    content_chunk
                )

                yield {
                    "type": "content",
                    "content": content_chunk,
                }

        # -------------------------------------------------
        # Deterministic persisted source links
        # -------------------------------------------------

        if web_research:
            full_response += (
                format_sources_markdown(
                    web_research
                )
            )

        # -------------------------------------------------
        # Save assistant response
        # -------------------------------------------------

        save_message(
            conversation_id,
            user_id,
            "assistant",
            full_response,
        )

        yield {
            "type": "response_complete",
            "mode": selected_mode,
            "model": selected_model,
            "response": full_response,
        }

        # -------------------------------------------------
        # Automatic long-term memory processing
        # -------------------------------------------------

        yield {
            "type": "status",
            "status": "memory",
            "label": "Updating memory...",
        }

        process_automatic_memory(
            user_id,
            effective_user_message,
        )

        # -------------------------------------------------
        # Fully finished
        # -------------------------------------------------

        yield {
            "type": "done",
            "mode": selected_mode,
            "model": selected_model,
            "response": full_response,
            "thinking": (
                full_thinking
                if include_thinking
                else ""
            ),
        }

    except OllamaConnectionError:
        yield {
            "type": "error",
            "kind": "connection",
            "message": (
                "Could not connect to Ollama."
            ),
        }

    except OllamaError as error:
        yield {
            "type": "error",
            "kind": "ollama",
            "message": str(error),
        }
