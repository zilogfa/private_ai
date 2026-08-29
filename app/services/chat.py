from app.config import MEMORY_RETRIEVAL_LIMIT

from app.ollama_client import (
    chat_stream,
    OllamaError,
    OllamaConnectionError,
)
from app.router import route_model
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
from app.services.attachments import bind_attachments_to_message
from app.services.documents import (
    DOCUMENT_CONTEXT_SIZE,
    DOCUMENT_TEXT_BUDGET,
    SHOW_DOCUMENT_ACTIVITY,
    VISION_DOCUMENT_TEXT_BUDGET,
    build_document_context,
    list_document_attachments,
    prepare_document_attachments,
)
from app.services.rag import (
    RAG_CONTEXT_SIZE,
    SHOW_RAG_ACTIVITY,
    RAGError,
    build_rag_context,
    format_indexed_documents_markdown,
    format_rag_sources_markdown,
    forget_indexed_documents,
    has_indexed_documents,
    index_document_attachments,
    list_indexed_documents,
    parse_rag_command,
    retrieve_document_chunks,
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
    resolve_web_request,
    research_direct_url,
    research_search_query,
    sources_event_data,
)


SYSTEM_PROMPT = (
    "You are one consistent private personal AI assistant. "
    "Respond naturally, clearly, and helpfully. "
    "Do not change personality based on the underlying model being used. "
    "Use relevant session context and long-term memory when available. "
    "Recent user statements take priority over older summaries or memories."
)


def build_memory_context(relevant_memories):
    if not relevant_memories:
        return None

    memory_text = "\n".join(
        (
            f"- [{memory['category']} | importance {memory['importance']} | "
            f"confidence {memory['confidence']:.2f}] {memory['content']}"
        )
        for memory in relevant_memories
    )

    return (
        "RELEVANT LONG-TERM MEMORY:\n"
        f"{memory_text}\n\n"
        "Use these memories naturally when helpful. Ignore irrelevant memories. "
        "Recent explicit user statements take priority over older memory."
    )


def build_chat_context(user_id, conversation_id, user_message):
    messages = build_session_context(user_id, conversation_id)

    relevant_memories = retrieve_relevant_memories(
        user_id,
        user_message,
        limit=MEMORY_RETRIEVAL_LIMIT,
    )

    if relevant_memories:
        mark_memories_accessed(
            user_id,
            [memory["id"] for memory in relevant_memories],
        )

        memory_context = build_memory_context(relevant_memories)
        if memory_context:
            messages.insert(
                0,
                {"role": "system", "content": memory_context},
            )

    messages.insert(
        0,
        {"role": "system", "content": SYSTEM_PROMPT},
    )
    return messages


def _insert_context_before_latest_user(messages, context):
    if not context:
        return messages

    insert_at = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            insert_at = index
            break

    messages.insert(
        insert_at,
        {"role": "system", "content": context},
    )
    return messages


def _replace_latest_user_content(messages, content):
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            updated = dict(messages[index])
            updated["content"] = content
            messages[index] = updated
            break
    return messages


def _combine_contexts(*contexts):
    parts = [
        str(context).strip()
        for context in contexts
        if str(context or "").strip()
    ]
    return "\n\n".join(parts) if parts else None


def _emit_direct_response(conversation_id, user_id, response):
    save_message(
        conversation_id,
        user_id,
        "assistant",
        response,
    )

    yield {
        "type": "response_complete",
        "mode": "local",
        "model": "document-rag",
        "response": response,
    }
    yield {
        "type": "done",
        "mode": "local",
        "model": "document-rag",
        "response": response,
        "thinking": "",
    }


def stream_chat(
    user_id,
    conversation_id,
    user_message,
    model_mode=None,
    include_thinking=True,
    attachments=None,
    web_mode=None,
):
    """
    Shared UI-independent chat pipeline.

    v1.7 adds persistent local document RAG while preserving the existing
    memory, web, vision, attachment, and model-routing behavior.

    Document behavior:
        attached readable documents -> local persistent indexing
        /docs -> list indexed documents
        /rag <question> -> force semantic document retrieval
        normal chat -> conservative automatic document retrieval when the
                       semantic match is strong enough
    """

    attachments = list(attachments or [])

    try:
        rag_command = parse_rag_command(user_message)
    except RAGError as error:
        yield {
            "type": "error",
            "kind": "rag_command",
            "message": str(error),
        }
        return

    try:
        web_request = resolve_web_request(
            user_message,
            preference=web_mode,
        )
    except WebResearchError as error:
        yield {
            "type": "error",
            "kind": "web_command",
            "message": str(error),
        }
        return

    resolved_web_mode = web_request.get("mode")
    effective_user_message = (
        rag_command.get("query")
        if rag_command and rag_command.get("mode") == "search"
        else (
            web_request.get("user_message")
            or user_message
        )
    )
    web_target = web_request.get("target") or effective_user_message

    if (
        resolved_web_mode
        and not user_has_permission(user_id, "web_search.use")
    ):
        yield {
            "type": "error",
            "kind": "permission",
            "message": "This account does not have web search permission.",
        }
        return

    image_attachments = list_image_attachments(attachments)
    document_attachments = list_document_attachments(attachments)

    yield {
        "type": "status",
        "status": "preparing",
        "label": "Preparing...",
    }

    user_message_id = save_message(
        conversation_id,
        user_id,
        "user",
        user_message,
    )

    if attachments:
        bind_attachments_to_message(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=user_message_id,
            attachment_ids=[attachment["id"] for attachment in attachments],
        )

    # -----------------------------------------------------
    # Persistent document indexing
    # -----------------------------------------------------
    if document_attachments:
        if SHOW_RAG_ACTIVITY:
            yield {
                "type": "activity",
                "phase": "indexing_document",
                "label": "Indexing document knowledge...",
                "detail": f"{len(document_attachments)} document(s)",
            }

        yield {
            "type": "tool",
            "tool": "document.index",
            "state": "start",
            "count": len(document_attachments),
        }

        reports = index_document_attachments(
            user_id,
            document_attachments,
        )

        yield {
            "type": "tool",
            "tool": "document.index",
            "state": "done",
            "items": reports,
        }

        failed = [report for report in reports if report.get("error")]
        if failed and SHOW_RAG_ACTIVITY:
            yield {
                "type": "activity",
                "phase": "indexing_document",
                "label": "Document indexing partially completed",
                "detail": failed[0].get("error"),
            }

    maybe_update_session_summary(user_id, conversation_id)

    # /docs is a direct local command and does not call an LLM.
    if rag_command and rag_command.get("mode") == "list":
        response = format_indexed_documents_markdown(
            list_indexed_documents(user_id)
        )
        yield from _emit_direct_response(
            conversation_id,
            user_id,
            response,
        )
        return

    if rag_command and rag_command.get("mode") == "forget":
        try:
            forgotten = forget_indexed_documents(
                user_id,
                rag_command.get("query"),
            )
        except RAGError as error:
            yield {
                "type": "error",
                "kind": "rag_forget",
                "message": str(error),
            }
            return

        if forgotten.get("all"):
            response = (
                f"Removed {forgotten.get('deleted', 0)} document index(es) "
                "from local RAG storage. Original uploaded files were not deleted."
            )
        else:
            name = (forgotten.get("names") or ["document"])[0]
            response = (
                f"Removed **{name}** from local RAG storage. "
                "The original uploaded file was not deleted."
            )

        yield from _emit_direct_response(
            conversation_id,
            user_id,
            response,
        )
        return

    messages = build_chat_context(
        user_id,
        conversation_id,
        effective_user_message,
    )

    if resolved_web_mode or rag_command:
        messages = _replace_latest_user_content(
            messages,
            effective_user_message,
        )

    # -----------------------------------------------------
    # Current-request document extraction / scanned PDF vision
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
            MAX_VISION_IMAGES - len(image_attachments),
        )

        document_result = prepare_document_attachments(
            document_attachments,
            max_vision_pages=remaining_visual_slots,
        )

    document_vision_images = list(
        (document_result or {}).get("vision_images", [])
    )

    using_vision = bool(
        image_attachments
        or document_vision_images
    )

    document_context = None
    if document_result:
        document_context = build_document_context(
            document_result,
            max_chars=(
                VISION_DOCUMENT_TEXT_BUDGET
                if using_vision
                else DOCUMENT_TEXT_BUDGET
            ),
        )

    # -----------------------------------------------------
    # Persistent RAG retrieval
    # -----------------------------------------------------
    rag_chunks = []
    rag_context = None
    force_rag = bool(
        rag_command
        and rag_command.get("mode") == "search"
    )

    # The current attachment already has direct extraction context. Avoid
    # duplicating it through persistent retrieval in the same turn.
    should_try_rag = (
        force_rag
        or (
            not document_attachments
            and has_indexed_documents(user_id)
        )
    )

    if should_try_rag:
        if SHOW_RAG_ACTIVITY:
            yield {
                "type": "activity",
                "phase": "searching_documents",
                "label": "Searching local documents...",
                "detail": effective_user_message,
            }

        yield {
            "type": "tool",
            "tool": "document.search",
            "state": "start",
            "query": effective_user_message,
            "forced": force_rag,
        }

        try:
            rag_chunks = retrieve_document_chunks(
                user_id,
                effective_user_message,
                force=force_rag,
            )
        except RAGError as error:
            if force_rag:
                yield {
                    "type": "error",
                    "kind": "rag_search",
                    "message": str(error),
                }
                return
            rag_chunks = []

        yield {
            "type": "tool",
            "tool": "document.search",
            "state": "done",
            "query": effective_user_message,
            "result_count": len(rag_chunks),
            "forced": force_rag,
        }

        if force_rag and not rag_chunks:
            response = (
                "I couldn't find useful passages in the locally indexed "
                "documents for that question. Try `/docs` to see what is "
                "currently indexed."
            )
            yield from _emit_direct_response(
                conversation_id,
                user_id,
                response,
            )
            return

        if rag_chunks:
            rag_context = build_rag_context(rag_chunks)

    # -----------------------------------------------------
    # Web research
    # -----------------------------------------------------
    web_research = None
    web_context = None

    if resolved_web_mode:
        yield {
            "type": "tool",
            "tool": "web.route",
            "state": "done",
            "mode": resolved_web_mode,
            "automatic": bool(web_request.get("automatic")),
            "reason": web_request.get("reason"),
        }

        try:
            if resolved_web_mode == "search":
                search_query = build_private_search_query(web_target)

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

                web_research = research_search_query(search_query)

                yield {
                    "type": "tool",
                    "tool": "web.search",
                    "state": "done",
                    "query": search_query,
                    "result_count": len(web_research.get("sources") or []),
                }
            else:
                if SHOW_WEB_ACTIVITY:
                    yield {
                        "type": "activity",
                        "phase": "reading",
                        "label": "Reading source...",
                        "detail": web_target,
                    }

                yield {
                    "type": "tool",
                    "tool": "web.fetch",
                    "state": "start",
                    "url": web_target,
                }

                web_research = research_direct_url(web_target)

                yield {
                    "type": "tool",
                    "tool": "web.fetch",
                    "state": "done",
                    "url": web_research.get("query"),
                }

            if SHOW_WEB_ACTIVITY:
                yield {
                    "type": "activity",
                    "phase": "reading",
                    "label": "Reading sources...",
                    "detail": f"{len(web_research.get('sources') or [])} source(s)",
                }

            yield {
                "type": "sources",
                "items": sources_event_data(web_research),
            }

            web_context = build_web_context(
                web_research,
                max_chars=(
                    WEB_VISION_CONTEXT_BUDGET
                    if using_vision
                    else WEB_TEXT_CONTEXT_BUDGET
                ),
            )

        except WebResearchError as error:
            yield {
                "type": "error",
                "kind": "web_research",
                "message": str(error),
            }
            return

    combined_context = _combine_contexts(
        document_context,
        rag_context,
        web_context,
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
        selected_mode, selected_model = route_model(
            effective_user_message,
            mode=model_mode,
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
            if document_vision_images and image_attachments:
                vision_label = "Analyzing images and document pages..."
            elif document_vision_images:
                vision_label = "Analyzing document pages..."
            else:
                vision_label = "Analyzing image..."

            yield {
                "type": "status",
                "status": "analyzing_visuals",
                "label": vision_label,
            }

        try:
            model_messages = build_vision_messages(
                base_messages=messages,
                image_attachments=image_attachments,
                other_attachments=(
                    (document_result or {}).get(
                        "unprocessed_attachments",
                        [],
                    )
                ),
                extra_images=document_vision_images,
                additional_context=combined_context,
            )
        except VisionPreparationError as error:
            yield {
                "type": "error",
                "kind": "vision_prepare",
                "message": str(error),
            }
            return

        generation_stream = stream_vision_chat(
            model_messages,
            timeout=900,
        )
    else:
        if combined_context:
            messages = _insert_context_before_latest_user(
                messages,
                combined_context,
            )

        yield {
            "type": "status",
            "status": "generating",
            "label": "Generating...",
        }

        context_size = 0
        if document_attachments:
            context_size = max(context_size, DOCUMENT_CONTEXT_SIZE)
        if resolved_web_mode:
            context_size = max(context_size, WEB_CONTEXT_SIZE)
        if rag_chunks:
            context_size = max(context_size, RAG_CONTEXT_SIZE)

        options = {"num_ctx": context_size} if context_size else None

        generation_stream = chat_stream(
            model=selected_model,
            messages=messages,
            options=options,
            timeout=900,
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
            message_data = data.get("message", {})

            thinking_chunk = message_data.get("thinking", "")
            if thinking_chunk and include_thinking:
                if not thinking_started:
                    yield {
                        "type": "status",
                        "status": "thinking",
                        "label": "Thinking...",
                    }
                    thinking_started = True

                full_thinking += thinking_chunk
                yield {
                    "type": "thinking",
                    "content": thinking_chunk,
                }

            content_chunk = message_data.get("content", "")
            if content_chunk:
                if not content_started:
                    yield {
                        "type": "status",
                        "status": "responding",
                        "label": "Responding...",
                    }
                    content_started = True

                full_response += content_chunk
                yield {
                    "type": "content",
                    "content": content_chunk,
                }

        if web_research:
            full_response += format_sources_markdown(web_research)

        if rag_chunks:
            full_response += format_rag_sources_markdown(rag_chunks)

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

        yield {
            "type": "status",
            "status": "memory",
            "label": "Updating memory...",
        }

        process_automatic_memory(
            user_id,
            effective_user_message,
        )

        yield {
            "type": "done",
            "mode": selected_mode,
            "model": selected_model,
            "response": full_response,
            "thinking": full_thinking if include_thinking else "",
        }

    except OllamaConnectionError:
        yield {
            "type": "error",
            "kind": "connection",
            "message": "Could not connect to Ollama.",
        }

    except OllamaError as error:
        yield {
            "type": "error",
            "kind": "ollama",
            "message": str(error),
        }
