"""ATLAS v3 local-model gateway.

One gateway owns model routing, context budgeting, cancellation, hard wall-clock
limits, structured JSON parsing, identity context, and telemetry.  The v3
orchestrator never talks to Ollama directly.
"""

import json
import os
import time

import requests

from app.config import DEFAULT_MODEL, DEEP_MODEL, OLLAMA_CHAT_URL
from app.services import agent_runner as legacy_runner
from app.services.agent_identity import agent_context_for_run
from app.services.agent_v3_storage import record_model_call

V3_CONTEXT_SIZE = max(4096, int(os.environ.get("PRIVATE_AI_V3_CONTEXT_SIZE", "8192")))
# ~6k tokens for ordinary English/code, leaving room for structured output.
V3_PROMPT_CHAR_BUDGET = max(
    12000,
    int(os.environ.get("PRIVATE_AI_V3_PROMPT_CHAR_BUDGET", "24000")),
)
V3_MODEL_CONNECT_TIMEOUT_SECONDS = max(
    3,
    int(os.environ.get("PRIVATE_AI_V3_MODEL_CONNECT_TIMEOUT_SECONDS", "10")),
)
V3_MODEL_IDLE_TIMEOUT_SECONDS = max(
    30,
    int(os.environ.get("PRIVATE_AI_V3_MODEL_IDLE_TIMEOUT_SECONDS", "180")),
)
V3_WORKER_TOTAL_TIMEOUT_SECONDS = max(
    120,
    int(os.environ.get("PRIVATE_AI_V3_WORKER_TOTAL_TIMEOUT_SECONDS", "360")),
)
V3_REASONING_TOTAL_TIMEOUT_SECONDS = max(
    240,
    int(os.environ.get("PRIVATE_AI_V3_REASONING_TOTAL_TIMEOUT_SECONDS", "720")),
)
V3_KEEP_ALIVE = os.environ.get("PRIVATE_AI_AGENT_MODEL_KEEP_ALIVE", "10m").strip() or "10m"


class V3ModelError(Exception):
    pass


class V3ModelTimeout(V3ModelError):
    pass


def _safe_json_object(text, label="v3 model"):
    value = str(text or "").strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise V3ModelError(f"The {label} did not return valid JSON.")
        try:
            parsed = json.loads(value[start:end + 1])
        except json.JSONDecodeError as error:
            raise V3ModelError(f"The {label} did not return valid JSON.") from error
    if not isinstance(parsed, dict):
        raise V3ModelError(f"The {label} returned a non-object JSON value.")
    return parsed


def _compact_identity_context(run, limit=5000):
    try:
        context = str(agent_context_for_run(run) or "").strip()
    except Exception:
        context = ""
    if not context:
        return ""
    if len(context) <= limit:
        return context
    return context[:limit] + "\n[agent identity/memory context truncated by v3 context governor]"


def _fit_prompt(system_prompt, user_prompt, budget=V3_PROMPT_CHAR_BUDGET):
    """Deterministically keep prompts below the configured local context budget.

    System policy and the beginning/end of the user context are retained.  The
    middle is the first place trimmed because it normally contains source detail,
    never the goal or latest failure tail.
    """
    system = str(system_prompt or "").strip()
    user = str(user_prompt or "").strip()
    budget = max(8000, int(budget))

    if len(system) + len(user) <= budget:
        return system, user

    system_budget = min(max(3500, budget // 4), max(3500, len(system)))
    if len(system) > system_budget:
        system = system[:system_budget] + "\n[system context truncated]"

    remaining = max(3000, budget - len(system))
    if len(user) > remaining:
        head = max(1200, int(remaining * 0.48))
        tail = max(1200, remaining - head - 120)
        user = (
            user[:head]
            + "\n\n[... middle context removed by ATLAS v3 context governor ...]\n\n"
            + user[-tail:]
        )
    return system, user


def _model_for_tier(run, tier):
    tier = str(tier or "worker").lower()
    mode = str(run.get("model_mode") or "auto").lower()
    if mode != "auto":
        _, selected = legacy_runner._select_agent_model(run)
        return selected
    return DEEP_MODEL if tier == "reasoning" else DEFAULT_MODEL


def run_json(
    run,
    *,
    phase,
    purpose,
    system_prompt,
    user_prompt,
    tier="worker",
    model=None,
    total_timeout_seconds=None,
    prompt_budget_chars=V3_PROMPT_CHAR_BUDGET,
):
    identity = _compact_identity_context(run)
    if identity:
        system_prompt = identity + "\n\n" + str(system_prompt or "")

    system_prompt, user_prompt = _fit_prompt(
        system_prompt,
        user_prompt,
        budget=prompt_budget_chars,
    )

    selected = model or _model_for_tier(run, tier)
    timeout_total = int(
        total_timeout_seconds
        or (
            V3_REASONING_TOTAL_TIMEOUT_SECONDS
            if str(tier).lower() == "reasoning"
            else V3_WORKER_TOTAL_TIMEOUT_SECONDS
        )
    )

    payload = {
        "model": selected,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "format": "json",
        "keep_alive": V3_KEEP_ALIVE,
        "options": {"num_ctx": V3_CONTEXT_SIZE, "temperature": 0},
    }

    started = time.monotonic()
    pieces = []
    status = "error"
    error_text = None

    try:
        legacy_runner._control_probe(run, force=True)
        with requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            stream=True,
            timeout=(
                V3_MODEL_CONNECT_TIMEOUT_SECONDS,
                min(V3_MODEL_IDLE_TIMEOUT_SECONDS, timeout_total),
            ),
        ) as response:
            if not response.ok:
                detail = ""
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        detail = str(body.get("error") or body.get("message") or "").strip()
                except Exception:
                    detail = str(response.text or "").strip()[:700]
                raise V3ModelError(
                    f"Local v3 model failed: Ollama HTTP {response.status_code}"
                    + (f": {detail}" if detail else "")
                )

            for line in response.iter_lines():
                legacy_runner._control_probe(run)
                if time.monotonic() - started >= timeout_total:
                    raise V3ModelTimeout(
                        f"Local v3 model exceeded its {timeout_total}s wall-clock limit."
                    )
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    raise V3ModelError("Local v3 model returned invalid streaming JSON.") from error
                message = item.get("message") or {}
                content = str(message.get("content") or "")
                if content:
                    pieces.append(content)
                if item.get("done"):
                    break

        raw = "".join(pieces).strip()
        if not raw:
            raise V3ModelError("Local v3 model returned no structured content.")
        result = _safe_json_object(raw, purpose)
        status = "success"
        return result, selected

    except requests.exceptions.ReadTimeout as error:
        error_text = (
            "Local v3 model stopped streaming for too long "
            f"({V3_MODEL_IDLE_TIMEOUT_SECONDS}s idle timeout)."
        )
        raise V3ModelTimeout(error_text) from error
    except requests.exceptions.ConnectTimeout as error:
        error_text = "Timed out connecting to Ollama."
        raise V3ModelTimeout(error_text) from error
    except requests.exceptions.ConnectionError as error:
        error_text = "Could not connect to Ollama."
        raise V3ModelError(error_text) from error
    except Exception as error:
        error_text = str(error)
        raise
    finally:
        record_model_call(
            run,
            phase=phase,
            purpose=purpose,
            model=selected,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_chars=len(system_prompt) + len(user_prompt),
            output_chars=sum(len(part) for part in pieces),
            prompt_budget_chars=prompt_budget_chars,
            context_size=V3_CONTEXT_SIZE,
            total_timeout_seconds=timeout_total,
            error=error_text,
        )
