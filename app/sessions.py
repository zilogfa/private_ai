from app.config import (
    SESSION_SUMMARY_MODEL,
    SUMMARY_TRIGGER_MESSAGES,
    RECENT_MESSAGE_LIMIT,
    SUMMARY_BATCH_LIMIT,
    MAX_SUMMARY_PASSES,
    SHOW_SUMMARY_ACTIVITY,
)

from app.ollama_client import (
    chat_once,
    OllamaError,
)

from app.database import (
    get_conversation_summary,
    update_conversation_summary,
    load_message_records,
)


# =========================================================
# SESSION SUMMARY GENERATION
# =========================================================

def create_updated_session_summary(
    existing_summary,
    message_records,
):
    if not message_records:
        return existing_summary

    transcript_parts = []

    for message in message_records:
        role = message["role"].upper()

        transcript_parts.append(
            f"[Message {message['id']}] "
            f"{role}:\n"
            f"{message['content']}"
        )

    transcript = "\n\n".join(
        transcript_parts
    )

    prompt = """
You are the conversation-context summarizer for a private personal AI assistant.

Maintain a concise but useful summary of the ongoing conversation.

Preserve:

- current topic
- goals
- decisions made
- technical architecture
- important facts
- relevant preferences
- corrections
- unresolved questions
- project progress
- context required to continue naturally

Remove:

- greetings
- filler
- repetition
- obsolete information
- unnecessary verbosity

If later information corrects earlier information,
preserve the newer version.

Return ONLY the updated summary.
Do not include reasoning.
"""

    current = (
        existing_summary
        if existing_summary
        else "None yet."
    )

    user_content = (
        "CURRENT SESSION SUMMARY:\n"
        f"{current}\n\n"
        "NEW MESSAGES TO INCORPORATE:\n"
        f"{transcript}"
    )

    messages = [
        {
            "role": "system",
            "content": prompt,
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]

    try:
        data = chat_once(
            model=SESSION_SUMMARY_MODEL,
            messages=messages,
            options={
                "temperature": 0.2,
            },
            timeout=300,
        )

        summary = (
            data
            .get("message", {})
            .get("content", "")
            .strip()
        )

        if summary:
            return summary

    except OllamaError as error:
        if SHOW_SUMMARY_ACTIVITY:
            print(
                "\nSession summary error:"
                f" {error}\n"
            )

    return None


# =========================================================
# SESSION SUMMARY LIFECYCLE
# =========================================================

def maybe_update_session_summary(
    user_id,
    conversation_id,
):
    summary_info = get_conversation_summary(
        conversation_id,
        user_id,
    )

    current_summary = (
        summary_info["summary"]
    )

    summarized_through = (
        summary_info[
            "summarized_through_message_id"
        ]
    )

    passes = 0

    while passes < MAX_SUMMARY_PASSES:
        unsummarized = load_message_records(
            conversation_id,
            user_id,
            after_id=summarized_through,
        )

        if (
            len(unsummarized)
            <= SUMMARY_TRIGGER_MESSAGES
        ):
            break

        candidates = (
            unsummarized[
                :-RECENT_MESSAGE_LIMIT
            ]
        )

        if not candidates:
            break

        batch = candidates[
            :SUMMARY_BATCH_LIMIT
        ]

        if not batch:
            break

        if SHOW_SUMMARY_ACTIVITY:
            print(
                "Context: summarizing "
                "older messages...",
                end="",
                flush=True,
            )

        new_summary = (
            create_updated_session_summary(
                current_summary,
                batch,
            )
        )

        if SHOW_SUMMARY_ACTIVITY:
            print(
                "\r"
                + " " * 80
                + "\r",
                end="",
                flush=True,
            )

        if not new_summary:
            break

        summarized_through = (
            batch[-1]["id"]
        )

        update_conversation_summary(
            conversation_id,
            user_id,
            new_summary,
            summarized_through,
        )

        current_summary = new_summary
        passes += 1

        if SHOW_SUMMARY_ACTIVITY:
            print(
                "Context: session summary "
                f"updated through message "
                f"{summarized_through}.\n"
            )


# =========================================================
# BUILD CHAT CONTEXT
# =========================================================

def build_session_context(
    user_id,
    conversation_id,
):
    summary_info = (
        get_conversation_summary(
            conversation_id,
            user_id,
        )
    )

    session_summary = (
        summary_info["summary"]
    )

    summarized_through = (
        summary_info[
            "summarized_through_message_id"
        ]
    )

    raw_records = load_message_records(
        conversation_id,
        user_id,
        after_id=summarized_through,
    )

    messages = []

    if session_summary:
        messages.append({
            "role": "system",
            "content": (
                "SESSION CONTEXT SUMMARY:\n"
                f"{session_summary}\n\n"
                "This represents older messages "
                "from the current conversation. "
                "Recent raw messages take priority "
                "if there is a conflict."
            ),
        })

    for record in raw_records:
        messages.append({
            "role": record["role"],
            "content": record["content"],
        })

    return messages