"""ATLAS v3 local-model gateway.

One gateway owns model routing, context budgeting, cancellation, hard wall-clock
limits, structured action protocol recovery, identity context, and telemetry.
The v3 orchestrator never talks to Ollama directly.

v3.6 design rule: model reasoning and action serialization are separate trust
boundaries.  A malformed model control message is an internal protocol problem;
it is not a committed engineering repair and must not directly mutate the
workspace or terminate a run before bounded protocol recovery is attempted.
"""

import json
import os
import time

import requests

from app.config import DEFAULT_MODEL, DEEP_MODEL, OLLAMA_CHAT_URL
from app.services import agent_runner as legacy_runner
from app.services.agent_identity import agent_context_for_run
from app.services.agent_v3_action_protocol import (
    GENERIC_OBJECT_SCHEMA,
    V3ProtocolError,
    parse_and_validate,
    raw_preview,
    schema_text,
)
from app.services.agent_v3_storage import record_model_call, record_protocol_event

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
V3_PROTOCOL_REPAIR_TIMEOUT_SECONDS = max(
    60,
    int(os.environ.get("PRIVATE_AI_V3_PROTOCOL_REPAIR_TIMEOUT_SECONDS", "180")),
)
V3_PROTOCOL_REPAIR_ATTEMPTS = max(
    0,
    min(2, int(os.environ.get("PRIVATE_AI_V3_PROTOCOL_REPAIR_ATTEMPTS", "1"))),
)
V3_KEEP_ALIVE = os.environ.get("PRIVATE_AI_AGENT_MODEL_KEEP_ALIVE", "10m").strip() or "10m"


class V3ModelError(Exception):
    pass


class V3ModelTimeout(V3ModelError):
    pass


class V3ModelProtocolError(V3ModelError):
    pass


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


def _serializer_model(run, original_model):
    # In Auto mode the 8B worker is intentionally used as a serializer even
    # when DeepSeek performed the reasoning.  Serialization is a narrow task
    # and should not spend another expensive reasoning call unless the user
    # explicitly pinned one model for the whole run.
    if str(run.get("model_mode") or "auto").lower() == "auto":
        return DEFAULT_MODEL
    return original_model


def _schema_format(schema):
    return schema if isinstance(schema, dict) else "json"


def _schema_format_rejected(error):
    text = str(error or "").lower()
    if "http 400" not in text:
        return False
    return any(token in text for token in (
        "schema", "format", "structured", "json schema", "invalid format",
    ))


def _stream_once(
    run,
    *,
    phase,
    purpose,
    system_prompt,
    user_prompt,
    model,
    total_timeout_seconds,
    prompt_budget_chars,
    format_spec,
):
    """One physical Ollama generation with telemetry."""
    system_prompt, user_prompt = _fit_prompt(
        system_prompt,
        user_prompt,
        budget=prompt_budget_chars,
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "format": format_spec,
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
                min(V3_MODEL_IDLE_TIMEOUT_SECONDS, int(total_timeout_seconds)),
            ),
        ) as response:
            if not response.ok:
                detail = ""
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        raw_error = body.get("error")
                        if isinstance(raw_error, dict):
                            detail = str(raw_error.get("message") or raw_error.get("error") or raw_error).strip()
                        else:
                            detail = str(raw_error or body.get("message") or "").strip()
                except Exception:
                    detail = str(response.text or "").strip()[:900]
                raise V3ModelError(
                    f"Local v3 model failed: Ollama HTTP {response.status_code}"
                    + (f": {detail}" if detail else "")
                )

            for line in response.iter_lines():
                legacy_runner._control_probe(run)
                if time.monotonic() - started >= int(total_timeout_seconds):
                    raise V3ModelTimeout(
                        f"Local v3 model exceeded its {int(total_timeout_seconds)}s wall-clock limit."
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
        status = "success"
        return raw

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
            model=model,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_chars=len(system_prompt) + len(user_prompt),
            output_chars=sum(len(part) for part in pieces),
            prompt_budget_chars=prompt_budget_chars,
            context_size=V3_CONTEXT_SIZE,
            total_timeout_seconds=int(total_timeout_seconds),
            error=error_text,
        )


def _protocol_repair(
    run,
    *,
    phase,
    purpose,
    original_model,
    raw,
    schema,
    schema_name,
    parse_error,
    attempt,
):
    serializer = _serializer_model(run, original_model)
    schema = schema or GENERIC_OBJECT_SCHEMA
    system = (
        "You are ATLAS v3 ACTION SERIALIZER. Do not solve the engineering problem again and do not change the proposed code or intent. "
        "Your only job is to re-emit the supplied candidate as one JSON object matching the provided action schema. "
        "Preserve every filename and complete file content exactly when they exist in the candidate. "
        "Do not add markdown fences, commentary, analysis, or thinking. Return only the schema-conforming JSON object."
    )
    user = (
        "ACTION SCHEMA:\n" + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n\nPROTOCOL ERROR TO CORRECT:\n" + str(parse_error)[:1800]
        + "\n\nRAW CANDIDATE TO SERIALIZE:\n" + str(raw or "")
    )
    repair_purpose = f"{purpose}_protocol_repair_{attempt}"
    raw_repaired = _stream_once(
        run,
        phase=phase,
        purpose=repair_purpose,
        system_prompt=system,
        user_prompt=user,
        model=serializer,
        total_timeout_seconds=V3_PROTOCOL_REPAIR_TIMEOUT_SECONDS,
        prompt_budget_chars=V3_PROMPT_CHAR_BUDGET,
        format_spec=_schema_format(schema),
    )
    record_protocol_event(
        run,
        phase=phase,
        purpose=purpose,
        model=serializer,
        event_type="protocol_repair_generation",
        status="generated",
        schema_name=schema_name,
        raw_output_chars=len(raw_repaired),
        raw_preview=raw_preview(raw_repaired),
        detail=f"Protocol serializer attempt {attempt} generated a replacement control message.",
    )
    return raw_repaired, serializer


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
    schema=None,
    schema_name=None,
    protocol_repair_attempts=None,
):
    """Run one logical structured model action.

    A logical action may contain internal serialization recovery generations.
    Those are telemetry-visible but do not create Agent steps, mutate the
    workspace, or consume committed-repair budget.
    """
    identity = _compact_identity_context(run)
    if identity:
        system_prompt = identity + "\n\n" + str(system_prompt or "")

    selected = model or _model_for_tier(run, tier)
    timeout_total = int(
        total_timeout_seconds
        or (
            V3_REASONING_TOTAL_TIMEOUT_SECONDS
            if str(tier).lower() == "reasoning"
            else V3_WORKER_TOTAL_TIMEOUT_SECONDS
        )
    )
    schema = schema or GENERIC_OBJECT_SCHEMA
    schema_name = str(schema_name or purpose or "v3_action")[:120]
    repair_limit = (
        V3_PROTOCOL_REPAIR_ATTEMPTS
        if protocol_repair_attempts is None
        else max(0, min(2, int(protocol_repair_attempts)))
    )

    format_spec = _schema_format(schema)
    try:
        raw = _stream_once(
            run,
            phase=phase,
            purpose=purpose,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=selected,
            total_timeout_seconds=timeout_total,
            prompt_budget_chars=prompt_budget_chars,
            format_spec=format_spec,
        )
    except V3ModelError as error:
        # Older Ollama versions may support JSON mode but not JSON-schema mode.
        # Fall back once to JSON mode while preserving the schema in the prompt.
        if isinstance(format_spec, dict) and _schema_format_rejected(error):
            record_protocol_event(
                run,
                phase=phase,
                purpose=purpose,
                model=selected,
                event_type="schema_transport_fallback",
                status="fallback",
                schema_name=schema_name,
                detail=str(error),
            )
            raw = _stream_once(
                run,
                phase=phase,
                purpose=purpose + "_json_mode_fallback",
                system_prompt=(
                    str(system_prompt or "")
                    + "\n\nATLAS ACTION SCHEMA (JSON mode fallback):\n"
                    + schema_text(schema)
                    + "\nReturn one JSON object matching this schema exactly."
                ),
                user_prompt=user_prompt,
                model=selected,
                total_timeout_seconds=timeout_total,
                prompt_budget_chars=prompt_budget_chars,
                format_spec="json",
            )
        else:
            raise

    last_error = None
    current_raw = raw
    current_model = selected
    for protocol_attempt in range(repair_limit + 1):
        try:
            result = parse_and_validate(current_raw, schema=schema, label=purpose)
            if protocol_attempt:
                record_protocol_event(
                    run,
                    phase=phase,
                    purpose=purpose,
                    model=current_model,
                    event_type="protocol_recovered",
                    status="success",
                    schema_name=schema_name,
                    raw_output_chars=len(current_raw),
                    detail=f"Structured action recovered after {protocol_attempt} serializer attempt(s).",
                )
            return result, selected
        except V3ProtocolError as error:
            last_error = error
            record_protocol_event(
                run,
                phase=phase,
                purpose=purpose,
                model=current_model,
                event_type="protocol_invalid",
                status="rejected",
                schema_name=schema_name,
                raw_output_chars=len(current_raw),
                raw_preview=raw_preview(current_raw),
                detail=str(error),
            )
            if protocol_attempt >= repair_limit:
                break
            current_raw, current_model = _protocol_repair(
                run,
                phase=phase,
                purpose=purpose,
                original_model=selected,
                raw=current_raw,
                schema=schema,
                schema_name=schema_name,
                parse_error=error,
                attempt=protocol_attempt + 1,
            )

    raise V3ModelProtocolError(
        f"The {purpose} action remained invalid after bounded protocol recovery: {last_error or 'unknown protocol error'}."
    )
