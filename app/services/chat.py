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
)

from app.memory import (
    retrieve_relevant_memories,
    process_automatic_memory,
)

from app.sessions import (
    maybe_update_session_summary,
    build_session_context,
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
# STREAM CHAT
# =========================================================

def stream_chat(
    user_id,
    conversation_id,
    user_message,
    model_mode=None,
    include_thinking=True,
):
    """
    Shared UI-independent chat pipeline.

    Web requests should pass model_mode explicitly.
    Terminal requests may leave it as None.

    Standard event families:
        status
        route
        thinking
        content
        response_complete
        done
        error

    Future capabilities such as vision, search, speech,
    and image generation can reuse the same event pattern.
    """

    yield {
        "type": "status",
        "status": "preparing",
        "label": "Preparing...",
    }

    # -----------------------------------------------------
    # Save user message
    # -----------------------------------------------------

    save_message(
        conversation_id,
        user_id,
        "user",
        user_message,
    )

    # -----------------------------------------------------
    # Session summary maintenance
    # -----------------------------------------------------

    maybe_update_session_summary(
        user_id,
        conversation_id,
    )

    # -----------------------------------------------------
    # Build context
    # -----------------------------------------------------

    messages = build_chat_context(
        user_id,
        conversation_id,
        user_message,
    )

    # -----------------------------------------------------
    # Model routing
    # -----------------------------------------------------

    yield {
        "type": "status",
        "status": "routing",
        "label": "Routing...",
    }

    selected_mode, selected_model = (
        route_model(
            user_message,
            mode=model_mode,
        )
    )

    yield {
        "type": "route",
        "mode": selected_mode,
        "model": selected_model,
    }

    yield {
        "type": "status",
        "status": "generating",
        "label": "Generating...",
    }

    # -----------------------------------------------------
    # Generate response
    # -----------------------------------------------------

    full_response = ""
    full_thinking = ""

    thinking_started = False
    content_started = False

    try:
        for data in chat_stream(
            model=selected_model,
            messages=messages,
            timeout=600,
        ):
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
            user_message,
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
