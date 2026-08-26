import json

from app.config import (
    FAST_MODEL,
    DEFAULT_MODEL,
    DEEP_MODEL,
    ROUTER_MODEL,
    DEFAULT_MODEL_MODE,
    VALID_MODEL_MODES,
)

from app.ollama_client import (
    chat_once,
)


# =========================================================
# ROUTER STATE
# =========================================================

_current_model_mode = DEFAULT_MODEL_MODE


def get_model_mode():
    return _current_model_mode


def set_model_mode(mode):
    global _current_model_mode

    mode = str(
        mode
    ).lower().strip()

    if mode not in VALID_MODEL_MODES:
        return False

    _current_model_mode = mode

    return True


# =========================================================
# MODEL LOOKUP
# =========================================================

def model_name_from_mode(mode):

    if mode == "fast":
        return FAST_MODEL

    if mode == "deep":
        return DEEP_MODEL

    return DEFAULT_MODEL


# =========================================================
# HEURISTIC FALLBACK
# =========================================================

def heuristic_route(message):

    text = message.lower()

    deep_keywords = [
        "debug",
        "architecture",
        "algorithm",
        "leetcode",
        "optimize",
        "complex",
        "reason",
        "reasoning",
        "prove",
        "math",
        "analyze deeply",
        "deep analysis",
        "step by step",
        "database design",
        "system design",
        "security",
        "performance issue",
    ]

    fast_keywords = [
        "short answer",
        "quick question",
        "briefly",
        "simple question",
        "translate",
        "rewrite",
        "grammar",
        "what does",
        "define",
    ]

    if any(
        keyword in text
        for keyword in deep_keywords
    ):
        return "deep"

    if any(
        keyword in text
        for keyword in fast_keywords
    ):
        return "fast"

    if len(message) < 80:
        return "fast"

    return "default"


# =========================================================
# QUICK ROUTING
# =========================================================

def obvious_route(message):
    """
    Handle obvious routing decisions without loading the
    router model.

    Returns:
        fast / deep / None

    None means the router model should decide.
    """

    text = message.lower().strip()

    fast_phrases = [
        "short answer",
        "quick answer",
        "briefly",
        "rewrite this",
        "translate this",
        "fix grammar",
        "grammar check",
    ]

    deep_phrases = [
        "leetcode",
        "time complexity",
        "space complexity",
        "system design",
        "database architecture",
        "debug this code",
        "debug this error",
        "prove that",
        "deep analysis",
        "analyze deeply",
    ]

    if any(
        phrase in text
        for phrase in deep_phrases
    ):
        return "deep"

    if any(
        phrase in text
        for phrase in fast_phrases
    ):
        return "fast"

    return None


# =========================================================
# AI ROUTER
# =========================================================

def route_model(user_message):

    current_mode = get_model_mode()

    # -----------------------------------------------------
    # Manual mode
    # -----------------------------------------------------

    if current_mode != "auto":

        return (
            current_mode,
            model_name_from_mode(
                current_mode
            ),
        )

    # -----------------------------------------------------
    # Skip router model for very obvious cases
    # -----------------------------------------------------

    obvious = obvious_route(
        user_message
    )

    if obvious is not None:

        return (
            obvious,
            model_name_from_mode(
                obvious
            ),
        )

    # -----------------------------------------------------
    # AI router
    # -----------------------------------------------------

    router_prompt = """
You are a model router for a private AI assistant.

Choose which model tier should answer the user's message.

You have exactly three choices:

fast
default
deep

FAST:
Use for:
- simple questions
- casual conversation
- definitions
- straightforward factual explanations
- short rewriting
- grammar
- translation
- simple commands
- easy coding questions
- lightweight summaries

DEFAULT:
Use for:
- normal conversation
- most technical questions
- normal coding
- comparisons
- planning
- moderate reasoning
- explanations requiring some nuance
- most general-purpose requests

DEEP:
Use only when useful for:
- difficult debugging
- complex coding
- algorithms
- LeetCode-style reasoning
- math
- multi-step logical reasoning
- architecture decisions
- difficult planning
- subtle tradeoffs
- complex system design
- difficult technical analysis

Do NOT select deep merely because the message is long.

Prefer the smallest model that can answer well.

Return ONLY valid JSON:

{
    "mode": "fast"
}

or:

{
    "mode": "default"
}

or:

{
    "mode": "deep"
}

Do not explain.
"""

    messages = [
        {
            "role": "system",
            "content": router_prompt,
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]

    try:

        data = chat_once(
            model=ROUTER_MODEL,
            messages=messages,
            response_format="json",
            options={
                "temperature": 0
            },
            timeout=120,
        )

        content = (
            data
            .get(
                "message",
                {},
            )
            .get(
                "content",
                "",
            )
            .strip()
        )

        result = json.loads(
            content
        )

        selected_mode = (
            str(
                result.get(
                    "mode",
                    "default",
                )
            )
            .lower()
            .strip()
        )

        if selected_mode not in (
            "fast",
            "default",
            "deep",
        ):

            selected_mode = (
                heuristic_route(
                    user_message
                )
            )

    except Exception:

        selected_mode = (
            heuristic_route(
                user_message
            )
        )

    return (
        selected_mode,
        model_name_from_mode(
            selected_mode
        ),
    )