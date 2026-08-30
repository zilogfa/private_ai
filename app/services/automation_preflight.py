import json

from app.config import DEFAULT_MODEL
from app.ollama_client import (
    OllamaError,
    chat_once,
)


AUTOMATION_PREFLIGHT_MODEL = DEFAULT_MODEL
AUTOMATION_PREFLIGHT_CONTEXT_SIZE = 4096
AUTOMATION_PREFLIGHT_VERSION = 1


class AutomationPreflightError(Exception):
    pass


def _safe_json_object(text):
    text = str(text or "").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start < 0 or end <= start:
            raise AutomationPreflightError(
                "Task review returned invalid JSON."
            )

        try:
            parsed = json.loads(
                text[start:end + 1]
            )
        except json.JSONDecodeError as error:
            raise AutomationPreflightError(
                "Task review returned invalid JSON."
            ) from error

    if not isinstance(parsed, dict):
        raise AutomationPreflightError(
            "Task review returned an invalid result."
        )

    return parsed


def _clean_text(value, max_chars=4000):
    return str(value or "").strip()[:max_chars]


def _bool_map(value):
    value = value if isinstance(value, dict) else {}

    return {
        "web": bool(value.get("web")),
        "rag": bool(value.get("rag")),
        "memory": bool(value.get("memory")),
    }


def _selected_tools(payload):
    return {
        "web": bool(payload.get("allow_web")),
        "rag": bool(payload.get("allow_rag")),
        "memory": bool(payload.get("allow_memory")),
    }


def _reminder_preflight(payload):
    instruction = _clean_text(
        payload.get("instruction"),
        6000,
    )

    return {
        "version": AUTOMATION_PREFLIGHT_VERSION,
        "status": "ready",
        "summary": "Reminder is ready to run without AI tools.",
        "clarification": "",
        "missing_required_tools": [],
        "required_tools": {
            "web": False,
            "rag": False,
            "memory": False,
        },
        "recommended_tools": {
            "web": False,
            "rag": False,
            "memory": False,
        },
        "compiled_spec": {
            "version": AUTOMATION_PREFLIGHT_VERSION,
            "objective": instruction,
            "execution_type": "reminder",
            "execution_steps": [
                "Deliver the reminder text at the scheduled time."
            ],
            "success_criteria": "The reminder text is delivered once for this run.",
            "output_style": "Use the reminder text directly.",
            "condition": "",
            "queries": {
                "web": "",
                "rag": "",
                "memory": "",
            },
            "required_tools": {
                "web": False,
                "rag": False,
                "memory": False,
            },
            "recommended_tools": {
                "web": False,
                "rag": False,
                "memory": False,
            },
            "assumptions": [],
        },
        "model": "none",
    }


def review_task_definition(payload):
    payload = dict(payload or {})
    task_type = _clean_text(
        payload.get("task_type") or "reminder",
        40,
    ).lower()

    if task_type == "reminder":
        return _reminder_preflight(payload)

    title = _clean_text(payload.get("title"), 160)
    instruction = _clean_text(
        payload.get("instruction"),
        6000,
    )
    condition_text = _clean_text(
        payload.get("condition_text"),
        3000,
    )

    if not instruction:
        raise AutomationPreflightError(
            "Task instruction is required before review."
        )

    selected = _selected_tools(payload)

    system_prompt = (
        "You are the task compiler for a private local automation engine. "
        "The task will run later without a live user conversation. Review the "
        "task definition, make harmless obvious assumptions, and avoid asking "
        "for optional preferences. Only return needs_input when a missing detail "
        "makes meaningful unattended execution impossible or unsafe. For example, "
        "'Check current OpenAI news' is clear and should be ready. 'Check my flight' "
        "without any flight identifier or route may need input. "
        "Recommend the minimum tools needed. Use web for fresh/public changing "
        "information, current news, prices, or public URLs. Use RAG only when the "
        "task actually depends on the user's indexed documents. Use memory only "
        "when the task genuinely depends on the user's personal history, preferences, "
        "or saved personal context. Do not recommend RAG or memory merely because "
        "they are available. The selected tools are permissions, not requirements to "
        "use every tool on every run. "
        "Return ONLY one JSON object with keys: status ('ready' or 'needs_input'), "
        "summary, clarification, required_tools, recommended_tools, compiled_spec. "
        "required_tools and recommended_tools must each contain booleans for web, "
        "rag, memory. compiled_spec must contain objective, execution_steps (array), "
        "success_criteria, output_style, condition, queries (web/rag/memory strings), "
        "required_tools, recommended_tools, assumptions (array). Preserve the user's "
        "intent; do not invent a different task."
    )

    user_prompt = json.dumps(
        {
            "title": title,
            "task_type": task_type,
            "instruction": instruction,
            "condition": condition_text,
            "currently_allowed_tools": selected,
        },
        ensure_ascii=False,
        indent=2,
    )

    try:
        response = chat_once(
            model=AUTOMATION_PREFLIGHT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            response_format="json",
            options={
                "num_ctx": AUTOMATION_PREFLIGHT_CONTEXT_SIZE,
            },
            timeout=600,
        )
    except OllamaError as error:
        raise AutomationPreflightError(
            f"Could not review the automation with the local model: {error}"
        ) from error

    content = (
        (response or {})
        .get("message", {})
        .get("content", "")
    )
    data = _safe_json_object(content)

    raw_status = _clean_text(
        data.get("status") or "ready",
        40,
    ).lower()
    status = (
        "needs_input"
        if raw_status == "needs_input"
        else "ready"
    )

    required = _bool_map(
        data.get("required_tools")
    )
    recommended = _bool_map(
        data.get("recommended_tools")
    )

    # Required capabilities are always also recommended.
    for key in required:
        if required[key]:
            recommended[key] = True

    compiled = (
        data.get("compiled_spec")
        if isinstance(
            data.get("compiled_spec"),
            dict,
        )
        else {}
    )

    compiled_required = _bool_map(
        compiled.get("required_tools")
    )
    compiled_recommended = _bool_map(
        compiled.get("recommended_tools")
    )

    # Prefer the top-level review result, while keeping the compiled spec in sync.
    for key in required:
        required[key] = bool(
            required[key]
            or compiled_required[key]
        )
        recommended[key] = bool(
            recommended[key]
            or compiled_recommended[key]
            or required[key]
        )

    queries = (
        compiled.get("queries")
        if isinstance(
            compiled.get("queries"),
            dict,
        )
        else {}
    )

    execution_steps = compiled.get(
        "execution_steps"
    )
    if not isinstance(execution_steps, list):
        execution_steps = []

    assumptions = compiled.get("assumptions")
    if not isinstance(assumptions, list):
        assumptions = []

    normalized_compiled = {
        "version": AUTOMATION_PREFLIGHT_VERSION,
        "objective": _clean_text(
            compiled.get("objective")
            or instruction,
            6000,
        ),
        "execution_type": task_type,
        "execution_steps": [
            _clean_text(item, 1000)
            for item in execution_steps
            if _clean_text(item, 1000)
        ][:12],
        "success_criteria": _clean_text(
            compiled.get("success_criteria"),
            2000,
        ),
        "output_style": _clean_text(
            compiled.get("output_style"),
            1200,
        ),
        "condition": _clean_text(
            compiled.get("condition")
            or condition_text,
            3000,
        ),
        "queries": {
            "web": _clean_text(
                queries.get("web"),
                1200,
            ),
            "rag": _clean_text(
                queries.get("rag"),
                1200,
            ),
            "memory": _clean_text(
                queries.get("memory"),
                1200,
            ),
        },
        "required_tools": required,
        "recommended_tools": recommended,
        "assumptions": [
            _clean_text(item, 600)
            for item in assumptions
            if _clean_text(item, 600)
        ][:10],
    }

    missing = [
        key
        for key, needed in required.items()
        if needed and not selected.get(key)
    ]

    clarification = _clean_text(
        data.get("clarification"),
        3000,
    )
    summary = _clean_text(
        data.get("summary")
        or "Task review completed.",
        2000,
    )

    if status == "ready" and missing:
        status = "needs_tools"
        labels = {
            "web": "Web search",
            "rag": "indexed document search",
            "memory": "personal memory",
        }
        clarification = (
            "Enable the required tool access before activating this task: "
            + ", ".join(
                labels[item]
                for item in missing
            )
            + "."
        )

    return {
        "version": AUTOMATION_PREFLIGHT_VERSION,
        "status": status,
        "summary": summary,
        "clarification": clarification,
        "missing_required_tools": missing,
        "required_tools": required,
        "recommended_tools": recommended,
        "compiled_spec": normalized_compiled,
        "model": AUTOMATION_PREFLIGHT_MODEL,
    }
