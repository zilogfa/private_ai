import re

from app.config import FAST_MODEL

from app.database import (
    get_conversation,
    update_conversation_title,
)

from app.ollama_client import (
    chat_once,
)


DEFAULT_CONVERSATION_TITLE = "New Chat"
MAX_TITLE_LENGTH = 60


def clean_conversation_title(title):
    title = str(title or "").strip()

    if not title:
        return None

    title = title.splitlines()[0].strip()

    # Remove common model formatting.
    title = re.sub(
        r"^(title\s*:\s*)",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = title.strip(
        " \t\r\n"
        "\"'`*_#"
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    )

    title = title.rstrip(
        " .,:;!?-"
    )

    if not title:
        return None

    return title[
        :MAX_TITLE_LENGTH
    ].rstrip()


def fallback_conversation_title(
    user_message,
):
    text = str(
        user_message or ""
    )

    text = re.sub(
        r"```.*?```",
        " ",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(
        r"[#*_>`~\[\]()]",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    if not text:
        return DEFAULT_CONVERSATION_TITLE

    words = text.split()

    title = " ".join(
        words[:7]
    )

    cleaned = (
        clean_conversation_title(
            title
        )
    )

    return (
        cleaned
        or DEFAULT_CONVERSATION_TITLE
    )


def generate_conversation_title(
    user_message,
    assistant_response,
):
    """
    Generate a short chat title with the fast local model.

    If generation fails, return a deterministic fallback.
    """

    prompt = """
Create a concise title for this chat.

Rules:
- 3 to 7 words when possible.
- Capture the actual topic or task.
- Use the same language as the user's message.
- Do not use quotes.
- Do not use Markdown.
- Do not write "Title:".
- Do not end with punctuation.
- Output only the title.
"""

    user_excerpt = str(
        user_message or ""
    )[:1200]

    assistant_excerpt = str(
        assistant_response or ""
    )[:1600]

    messages = [
        {
            "role": "system",
            "content": prompt,
        },
        {
            "role": "user",
            "content": (
                "USER MESSAGE:\n"
                f"{user_excerpt}\n\n"
                "ASSISTANT RESPONSE:\n"
                f"{assistant_excerpt}"
            ),
        },
    ]

    try:
        data = chat_once(
            model=FAST_MODEL,
            messages=messages,
            options={
                "temperature": 0.2,
            },
            timeout=90,
        )

        raw_title = (
            data
            .get(
                "message",
                {},
            )
            .get(
                "content",
                "",
            )
        )

        cleaned = (
            clean_conversation_title(
                raw_title
            )
        )

        if cleaned:
            return cleaned

    except Exception:
        pass

    return fallback_conversation_title(
        user_message
    )


def maybe_generate_conversation_title(
    user_id,
    conversation_id,
    user_message,
    assistant_response,
):
    """
    Automatically title only untouched "New Chat" sessions.

    A manual rename is never overwritten.
    """

    conversation = get_conversation(
        conversation_id,
        user_id,
    )

    if not conversation:
        return None

    current_title = (
        str(
            conversation[1]
            or ""
        )
        .strip()
    )

    if (
        current_title
        and current_title
        != DEFAULT_CONVERSATION_TITLE
    ):
        return None

    title = generate_conversation_title(
        user_message,
        assistant_response,
    )

    if (
        not title
        or title
        == DEFAULT_CONVERSATION_TITLE
    ):
        return None

    changed = update_conversation_title(
        conversation_id=
            conversation_id,
        user_id=
            user_id,
        title=
            title,
        only_if_new=True,
    )

    if not changed:
        return None

    return title
