import json
import os
import re
import time

import requests

from app.config import (
    AGENT_MAX_RUNTIME_SECONDS,
    DEFAULT_MODEL,
    OLLAMA_CHAT_URL,
)
from app.database import (
    mark_memories_accessed,
    user_has_permission,
)
from app.memory import retrieve_relevant_memories
from app.router import route_model
from app.services.agents import (
    AgentStoreError,
    begin_agent_step,
    create_agent_artifact,
    finish_agent_step,
    get_agent_controls,
    get_agent_run,
    get_agent_source,
    list_agent_artifacts,
    list_agent_document_sources,
    list_agent_inputs,
    list_agent_sources,
    list_agent_steps,
    mark_agent_cancelled,
    mark_agent_completed,
    mark_agent_failed,
    mark_agent_paused,
    mark_agent_waiting_input,
    replace_agent_evidence,
    save_agent_document_source,
    save_agent_source,
    update_agent_source_content,
    write_agent_log,
)
from app.services.rag import (
    RAGError,
    has_indexed_documents,
    retrieve_document_chunks,
)
from app.services.web_research import (
    WebResearchError,
    research_direct_url,
    research_search_query,
)


AGENT_CONTEXT_SIZE = 8192
AGENT_RESULT_LIMIT = 18000
AGENT_STEP_OUTPUT_LIMIT = 6500
AGENT_LEDGER_LIMIT = 18000

# A stalled local model request used to wait up to 900 seconds with no stream
# activity, leaving the Agent UI looking frozen between recorded steps.
#
# The read timeout is an IDLE timeout, not a total-generation timeout. As long
# as Ollama continues streaming chunks (including thinking chunks), long useful
# generations may continue. If Ollama produces no HTTP stream data for this
# period, the run fails cleanly instead of appearing stuck for 15 minutes.
AGENT_MODEL_CONNECT_TIMEOUT_SECONDS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_MODEL_CONNECT_TIMEOUT_SECONDS",
        "10",
    )
)
AGENT_MODEL_IDLE_TIMEOUT_SECONDS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_MODEL_IDLE_TIMEOUT_SECONDS",
        "180",
    )
)
AGENT_MODEL_KEEP_ALIVE = os.environ.get(
    "PRIVATE_AI_AGENT_MODEL_KEEP_ALIVE",
    "10m",
).strip() or "10m"

EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)"
)
LONG_NUMBER_RE = re.compile(
    r"(?<!\d)\d{7,}(?!\d)"
)


class AgentExecutionError(Exception):
    pass


class AgentCancelled(AgentExecutionError):
    pass


class AgentToolUnavailable(AgentExecutionError):
    """Raised when an enabled infrastructure tool is unavailable."""

    pass


def _safe_json_object(text, label="agent model"):
    value = str(text or "").strip()

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise AgentExecutionError(
                f"The {label} did not return valid JSON."
            )
        try:
            parsed = json.loads(value[start:end + 1])
        except json.JSONDecodeError as error:
            raise AgentExecutionError(
                f"The {label} did not return valid JSON."
            ) from error

    if not isinstance(parsed, dict):
        raise AgentExecutionError(
            f"The {label} returned an invalid object."
        )
    return parsed


def _select_agent_model(run):
    mode = str(run.get("model_mode") or "auto").strip().lower()

    # Agent control is multi-step. Auto deliberately uses the 8B model rather
    # than routing simple-looking planner prompts to the 4B model.
    if mode == "auto":
        return "default", DEFAULT_MODEL

    selected_mode, model = route_model(
        str(run.get("goal") or ""),
        mode=mode,
    )
    return selected_mode, model


def _control_probe(run, include_pause=False, force=False):
    now_mono = time.monotonic()
    last = float(run.get("_control_probe") or 0.0)

    if not force and now_mono - last < 0.35:
        return None

    run["_control_probe"] = now_mono
    controls = get_agent_controls(
        int(run["user_id"]),
        run["id"],
    )
    if not controls:
        raise AgentCancelled("Agent run no longer exists.")

    if controls.get("cancel_requested"):
        raise AgentCancelled("Cancelled by user.")

    if include_pause and controls.get("pause_requested"):
        return "pause"

    return None


def _run_model(
    run,
    system_prompt,
    user_prompt,
    response_format="json",
    model_override=None,
):
    _, selected = _select_agent_model(run)
    model = model_override or selected

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        # Agent runs make many short controller/finalizer calls. Keep the model
        # resident for a while so Ollama does not unload/reload it between steps.
        "keep_alive": AGENT_MODEL_KEEP_ALIVE,
        "options": {
            "num_ctx": AGENT_CONTEXT_SIZE,
        },
    }

    if response_format is not None:
        payload["format"] = response_format

    _control_probe(run, force=True)
    parts = []

    try:
        with requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            stream=True,
            timeout=(
                AGENT_MODEL_CONNECT_TIMEOUT_SECONDS,
                AGENT_MODEL_IDLE_TIMEOUT_SECONDS,
            ),
        ) as response:
            if not response.ok:
                detail = ""
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        detail = str(body.get("error") or "").strip()
                except ValueError:
                    detail = str(response.text or "").strip()[:500]

                raise AgentExecutionError(
                    "Local agent model failed: Ollama HTTP "
                    + str(response.status_code)
                    + (f": {detail}" if detail else "")
                )

            for line in response.iter_lines():
                _control_probe(run)
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AgentExecutionError(
                        "The local agent model returned invalid streaming JSON."
                    ) from error

                message = data.get("message") or {}
                chunk = str(message.get("content") or "")
                if chunk:
                    parts.append(chunk)

                if data.get("done"):
                    break

    except AgentCancelled:
        raise
    except requests.exceptions.ReadTimeout as error:
        raise AgentExecutionError(
            "Local agent model stopped streaming for too long. "
            f"No Ollama stream activity was received for "
            f"{AGENT_MODEL_IDLE_TIMEOUT_SECONDS} seconds. "
            "The run was stopped cleanly instead of remaining stuck; "
            "use Continue / Revise or retry after checking Ollama."
        ) from error
    except requests.exceptions.ConnectTimeout as error:
        raise AgentExecutionError(
            "Timed out connecting to Ollama during the agent run."
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise AgentExecutionError(
            "Could not connect to Ollama during the agent run."
        ) from error
    except requests.exceptions.RequestException as error:
        raise AgentExecutionError(
            f"Local agent model request failed: {error}"
        ) from error

    _control_probe(run, force=True)
    text = "".join(parts).strip()
    if not text:
        raise AgentExecutionError(
            "The local agent model returned an empty response."
        )
    return text, model


def _public_query_scrub(value):
    """
    Agent web queries may use the user's explicit agent goal and prior public
    search evidence only. Never feed local memory/RAG observations into this
    function. Preserve public constraints such as ZIP codes, acreage and price.
    """

    text = " ".join(str(value or "").split()).strip()
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    text = LONG_NUMBER_RE.sub("[number]", text)
    text = text.strip(" \t\r\n\"'`")

    if len(text) > 260:
        text = text[:260]
        if " " in text:
            text = text.rsplit(" ", 1)[0]
        text = text.strip()

    return text


def _public_research_summary(run):
    sources = list_agent_sources(
        run["user_id"],
        run["id"],
    )
    prior_queries = []
    blocks = []

    for source in sources:
        query = str(source.get("query") or "").strip()
        if query and query not in prior_queries:
            prior_queries.append(query)

        title = str(source.get("title") or "Web source")[:300]
        snippet = str(
            source.get("snippet")
            or source.get("content")
            or ""
        ).strip()[:900]
        blocks.append(
            f"{source.get('source_key')}: {title}\n{snippet}"
        )

    return prior_queries[-8:], "\n\n".join(blocks[-10:])[:9000]


def _next_public_query(run):
    prior_queries, public_summary = _public_research_summary(run)

    system_prompt = (
        "You are the public-search subplanner for a private local research agent. "
        "You are intentionally isolated from private memory and local documents. "
        "Create ONE concise web search query that advances the ORIGINAL PUBLIC GOAL. "
        "Use only the goal and public-search history shown below. Preserve important "
        "public constraints such as place, ZIP code, price, acreage, model number or "
        "date. Avoid repeating prior queries. If previous results reveal a missing "
        "criterion, refine the next query toward that criterion. Return ONLY JSON with "
        "one key: query."
    )

    user_prompt = (
        "ORIGINAL PUBLIC GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nPRIOR PUBLIC QUERIES:\n"
        + ("\n".join(prior_queries) if prior_queries else "None")
        + "\n\nPUBLIC SOURCE SUMMARY:\n"
        + (public_summary or "No public results yet.")
    )

    raw, _ = _run_model(
        run,
        system_prompt,
        user_prompt,
        response_format="json",
        model_override=DEFAULT_MODEL,
    )
    data = _safe_json_object(raw, "public-search subplanner")
    query = _public_query_scrub(data.get("query"))
    if not query:
        query = _public_query_scrub(run.get("goal"))
    if not query:
        raise AgentExecutionError(
            "The agent could not create a safe public search query."
        )
    return query


def _step_ledger(run):
    steps = list_agent_steps(
        run["user_id"],
        run["id"],
    )
    blocks = []

    for step in steps[-8:]:
        output = str(step.get("output") or "").strip()
        if len(output) > 2500:
            output = output[:2500] + "\n[step output truncated]"

        blocks.append(
            "STEP "
            + str(step.get("step_index"))
            + " | "
            + str(step.get("action") or step.get("phase") or "action")
            + " | "
            + str(step.get("status") or "")
            + "\nReason: "
            + str(step.get("reason") or "")
            + "\nObservation:\n"
            + output
        )

    text = "\n\n".join(blocks)
    return text[-AGENT_LEDGER_LIMIT:]


def _source_catalog(run):
    web = list_agent_sources(
        run["user_id"],
        run["id"],
    )
    docs = list_agent_document_sources(
        run["user_id"],
        run["id"],
    )

    lines = []
    for item in web:
        lines.append(
            f"{item['source_key']}: {item.get('title') or 'Web source'} — {item.get('url') or ''}"
        )
    for item in docs:
        page = item.get("page_number")
        location = f" page {page}" if page else ""
        lines.append(
            f"{item['source_key']}: {item.get('document_name')}{location}"
        )

    return "\n".join(lines)[:7000]


def _inputs_text(run):
    inputs = list_agent_inputs(
        run["user_id"],
        run["id"],
    )
    if not inputs:
        return "None"
    return "\n".join(
        f"- {item['content']}"
        for item in inputs[-6:]
    )[:7000]


def _tool_counts(run):
    counts = {
        "web_search": 0,
        "web_fetch": 0,
        "document_search": 0,
        "memory_search": 0,
        "write_file": 0,
    }
    for step in list_agent_steps(
        run["user_id"],
        run["id"],
    ):
        action = str(step.get("action") or "")
        if action in counts:
            counts[action] += 1
    return counts


def _available_actions(run):
    counts = _tool_counts(run)
    actions = ["final", "needs_input", "write_file"]

    if run.get("allow_web") and user_has_permission(
        run["user_id"], "web_search.use"
    ):
        if counts["web_search"] < 4:
            actions.append("web_search")
        if counts["web_fetch"] < 5 and list_agent_sources(
            run["user_id"], run["id"]
        ):
            actions.append("web_fetch")

    if run.get("allow_rag") and counts["document_search"] < 4:
        actions.append("document_search")

    if (
        run.get("allow_memory")
        and counts["memory_search"] < 3
        and user_has_permission(run["user_id"], "memory.manage_self")
    ):
        actions.append("memory_search")

    return actions


def _plan_next_action(run):
    available = _available_actions(run)
    current_step = int(run.get("current_step") or 0)
    remaining = max(0, int(run.get("max_steps") or 6) - current_step)

    system_prompt = (
        "You are the controller for a persistent private AI agent run. Choose exactly "
        "ONE next action. This is iterative work: do not stop after one shallow search "
        "when important criteria remain unresolved and step budget remains. When evidence "
        "is incomplete, explicitly pursue the missing criterion or finish with uncertainty "
        "labels rather than inventing facts. Distinguish confirmed, likely, unverified, "
        "conflicting, and rejected evidence. Do not reveal hidden chain-of-thought; the "
        "reason field must be one concise operational sentence.\n\n"
        "IMPORTANT PRIVACY RULE: web_search does NOT accept a query from you. A separate "
        "public-only subplanner creates the network query from the original goal and prior "
        "public results, so private memory/document contents cannot leak into web search.\n\n"
        "ACTIONS:\n"
        "- web_search: perform another public search.\n"
        "- web_fetch: fetch one existing public source by source_key, for example S2.\n"
        "- document_search: search local indexed documents; include query.\n"
        "- memory_search: search local personal memory; include query.\n"
        "- write_file: write a text/code artifact into this run's isolated workspace; "
        "include filename and content. Files are not executed in v1.9.\n"
        "- needs_input: pause and ask the user only when a genuinely important decision or "
        "missing user-specific fact cannot be reasonably handled, OR when the goal explicitly "
        "asks you to pause for the user's choice before continuing. Include question. Never "
        "use needs_input merely to ask permission to retry a search, change a query, inspect "
        "another source, or recover from a tool error; make those operational decisions yourself.\n"
        "- final: signal that research/work is complete. The dedicated finalizer will compose "
        "the supported final answer and evidence from the run ledger.\n\n"
        "For write_file, supported artifacts are text, Markdown, CSV, JSON, HTML, CSS, "
        "JavaScript, and Python source files; they are stored only, never executed.\n\n"
        "Return ONLY one JSON object."
    )

    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nUSER INPUT RECEIVED DURING THIS RUN:\n"
        + _inputs_text(run)
        + "\n\nAVAILABLE ACTIONS:\n"
        + ", ".join(available)
        + f"\n\nSTEP BUDGET REMAINING: {remaining}\n"
        + "\nSOURCE CATALOG:\n"
        + (_source_catalog(run) or "No sources recorded yet.")
        + "\n\nRUN LEDGER:\n"
        + (_step_ledger(run) or "No actions have run yet.")
        + "\n\nJSON KEYS:\n"
        "action, reason, query, source_key, filename, content, question"
    )

    last_error = None
    for attempt in range(2):
        raw, model = _run_model(
            run,
            system_prompt,
            user_prompt
            + (
                "\n\nPrevious reply was invalid. Return strict JSON only."
                if attempt
                else ""
            ),
            response_format="json",
        )
        try:
            data = _safe_json_object(raw, "agent controller")
        except AgentExecutionError as error:
            last_error = error
            continue

        action = str(data.get("action") or "").strip().lower()
        if action not in available:
            last_error = AgentExecutionError(
                f"Agent controller selected unavailable action: {action or 'empty'}"
            )
            continue

        data["action"] = action
        data["model"] = model
        data["reason"] = str(data.get("reason") or "").strip()[:1000]
        return data

    raise last_error or AgentExecutionError(
        "Agent controller could not select a valid action."
    )


def _log_step(run, step, action_data, output, status):
    write_agent_log(
        run["user_id"],
        run["id"],
        f"step_{int(step['step_index']):03d}.json",
        {
            "step": step["step_index"],
            "action": action_data.get("action"),
            "reason": action_data.get("reason"),
            "status": status,
            "output": output,
        },
    )


def _execute_web_search(run):
    query = _next_public_query(run)

    try:
        research = research_search_query(query)
    except WebResearchError as error:
        message = str(error).strip()
        lowered = message.lower()
        if (
            "could not connect" in lowered
            or "connection refused" in lowered
            or "searxng" in lowered and "connect" in lowered
        ):
            raise AgentToolUnavailable(
                "Public web research is unavailable because the local SearXNG service "
                f"could not be reached. Details: {message}"
            ) from error

        return (
            f"Public search failed for query: {query}\nError: {message}",
            {"query": query, "source_keys": []},
        )

    keys = []
    lines = [f"Public search query: {query}"]

    for source in research.get("sources") or []:
        key = save_agent_source(
            run["user_id"],
            run["id"],
            query,
            source,
        )
        if not key:
            continue
        keys.append(key)
        body = str(
            source.get("content")
            or source.get("snippet")
            or ""
        ).strip()[:1500]
        lines.append(
            f"\n{key} | {source.get('title') or 'Web source'}\n"
            f"URL: {source.get('url') or ''}\n{body}"
        )

    if not keys:
        lines.append("\nNo usable public sources were recorded.")

    return "\n".join(lines)[:AGENT_STEP_OUTPUT_LIMIT], {
        "query": query,
        "source_keys": keys,
    }


def _execute_web_fetch(run, source_key):
    key = str(source_key or "").strip().upper()
    source = get_agent_source(
        run["user_id"],
        run["id"],
        key,
    )
    if not source:
        return f"Public source {key or '(missing)'} was not found in this run."

    try:
        research = research_direct_url(source["url"])
    except WebResearchError as error:
        return f"Could not fetch {key}: {error}"

    fetched = (research.get("sources") or [{}])[0]
    update_agent_source_content(
        run["user_id"],
        run["id"],
        key,
        fetched,
    )

    body = str(fetched.get("content") or "").strip()
    if not body:
        body = "No readable page content was returned."

    return (
        f"Fetched {key}: {fetched.get('title') or source.get('title')}\n"
        f"URL: {fetched.get('url') or source.get('url')}\n\n"
        + body[:AGENT_STEP_OUTPUT_LIMIT]
    )


def _execute_document_search(run, query):
    text = str(query or run.get("goal") or "").strip()

    try:
        if not has_indexed_documents(run["user_id"]):
            return "No indexed documents are currently available."

        chunks = retrieve_document_chunks(
            run["user_id"],
            text,
            force=True,
            limit=5,
        )
    except RAGError as error:
        return f"Local document search failed: {error}"

    if not chunks:
        return "Local document search returned no matching passages."

    lines = [f"Local document search: {text}"]
    for item in chunks:
        key = save_agent_document_source(
            run["user_id"],
            run["id"],
            item,
        )
        page = item.get("page_number")
        location = f" page {page}" if page else ""
        lines.append(
            f"\n{key} | {item.get('name') or 'document'}{location}\n"
            + str(item.get("content") or "")[:1600]
        )

    return "\n".join(lines)[:AGENT_STEP_OUTPUT_LIMIT]


def _execute_memory_search(run, query):
    if not user_has_permission(run["user_id"], "memory.manage_self"):
        return "Personal memory permission is no longer available."

    text = str(query or run.get("goal") or "").strip()
    memories = retrieve_relevant_memories(
        run["user_id"],
        text,
        limit=6,
    )

    if memories:
        mark_memories_accessed(
            run["user_id"],
            [item["id"] for item in memories],
        )

    if not memories:
        return "Local personal memory search returned no relevant memories."

    lines = ["LOCAL PERSONAL MEMORY RESULTS (private, never sent to web search):"]
    for item in memories:
        lines.append("- " + str(item.get("content") or "")[:1400])
    return "\n".join(lines)[:AGENT_STEP_OUTPUT_LIMIT]


def _execute_write_file(run, filename, content):
    artifact = create_agent_artifact(
        run["user_id"],
        run["id"],
        filename=filename,
        content=content,
        kind="workspace_file",
        folder="files",
    )
    return (
        f"Created workspace file: {artifact['filename']} "
        f"({artifact['size_bytes']} bytes). It is stored locally and was not executed."
    )


def _fallback_final_answer(run):
    """Create a deterministic useful fallback if the finalizer omits answer text."""

    artifacts = list_agent_artifacts(
        run["user_id"],
        run["id"],
    )
    steps = list_agent_steps(
        run["user_id"],
        run["id"],
    )

    completed_outputs = [
        str(step.get("output") or "").strip()
        for step in steps
        if str(step.get("status") or "") == "completed"
        and str(step.get("output") or "").strip()
    ]

    parts = []
    if artifacts:
        names = ", ".join(
            str(item.get("filename") or "artifact")
            for item in artifacts[-5:]
        )
        parts.append(
            "Completed the available work and created the requested local artifact(s): "
            + names
            + "."
        )

    if completed_outputs:
        latest = completed_outputs[-1][:3500]
        parts.append("Latest completed step:\n" + latest)

    if not parts:
        parts.append(
            "The agent completed its available steps, but the local finalizer returned "
            "no answer text. Review the recorded steps, evidence, and sources for the "
            "work completed during this run."
        )

    return "\n\n".join(parts).strip()


def _finish_with_final(run, data):
    answer = str(data.get("answer") or "").strip()
    if not answer:
        answer = _fallback_final_answer(run)

    evidence = data.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    replace_agent_evidence(
        run["user_id"],
        run["id"],
        evidence[:30],
    )

    artifacts = data.get("artifacts") or []
    if isinstance(artifacts, list):
        for artifact in artifacts[:3]:
            if not isinstance(artifact, dict):
                continue
            filename = str(artifact.get("filename") or "artifact.md")
            content = str(artifact.get("content") or "")
            if not content.strip():
                continue
            try:
                create_agent_artifact(
                    run["user_id"],
                    run["id"],
                    filename=filename,
                    content=content,
                    kind="final_artifact",
                    folder="artifacts",
                )
            except AgentStoreError:
                # Final answer should still survive if an optional artifact fails.
                pass

    final = answer[:AGENT_RESULT_LIMIT]
    mark_agent_completed(
        run["user_id"],
        run["id"],
        final,
    )
    write_agent_log(
        run["user_id"],
        run["id"],
        "final.json",
        {
            "answer": final,
            "evidence": evidence[:30],
        },
    )
    return final


def _forced_final(run):
    system_prompt = (
        "You are finishing a private local agent run because its step budget has been "
        "reached. Synthesize the most useful supported answer from the run ledger. Do not "
        "invent missing facts. Clearly distinguish confirmed, likely, unverified, "
        "conflicting, and rejected findings. Cite recorded source keys such as S1 or D1 "
        "when relevant. The answer string may use concise Markdown headings, lists, tables, "
        "and code blocks when those formats make the result easier to understand. "
        "Return ONLY JSON with keys: answer (string), evidence (array), artifacts (array)."
    )
    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nSOURCE CATALOG:\n"
        + (_source_catalog(run) or "None")
        + "\n\nRUN LEDGER:\n"
        + (_step_ledger(run) or "No observations were recorded.")
        + "\n\nUSER INPUT:\n"
        + _inputs_text(run)
    )

    raw, _ = _run_model(
        run,
        system_prompt,
        user_prompt,
        response_format="json",
    )
    data = _safe_json_object(raw, "agent finalizer")
    return _finish_with_final(run, data)


def execute_agent_run(user_id, run_id):
    """
    Execute or resume one persistent iterative agent run.

    v1.9 intentionally provides a filesystem-isolated workspace but no arbitrary
    host code execution. Code/text files may be created as artifacts for later
    inspection; execution sandboxing is a separate future capability.
    """

    run = get_agent_run(user_id, run_id)
    if not run:
        return

    started_mono = time.monotonic()
    runtime_limit = max(60, int(AGENT_MAX_RUNTIME_SECONDS))

    try:
        while True:
            run = get_agent_run(user_id, run_id)
            if not run:
                return

            _control_probe(run, force=True)

            if _control_probe(run, include_pause=True, force=True) == "pause":
                mark_agent_paused(user_id, run_id)
                return

            if time.monotonic() - started_mono > runtime_limit:
                raise AgentExecutionError(
                    "Agent runtime budget was reached. Resume the run to continue with its existing workspace."
                )

            if int(run.get("current_step") or 0) >= int(run.get("max_steps") or 6):
                _forced_final(run)
                return

            action_data = _plan_next_action(run)
            action = action_data["action"]
            reason = action_data.get("reason") or ""

            step = begin_agent_step(
                user_id,
                run_id,
                phase="action",
                action=action,
                tool_name=(
                    {
                        "web_search": "web.search",
                        "web_fetch": "web.fetch",
                        "document_search": "document.search",
                        "memory_search": "memory.search",
                        "write_file": "agent.workspace.write",
                        "final": "agent.finalize",
                        "needs_input": "agent.input.request",
                    }.get(action)
                ),
                reason=reason,
                input_data={
                    key: value
                    for key, value in action_data.items()
                    if key not in {"content", "answer", "artifacts", "evidence"}
                },
            )

            output = ""
            status = "completed"

            try:
                if action == "web_search":
                    output, metadata = _execute_web_search(run)
                    action_data["search_metadata"] = metadata

                elif action == "web_fetch":
                    output = _execute_web_fetch(
                        run,
                        action_data.get("source_key"),
                    )

                elif action == "document_search":
                    output = _execute_document_search(
                        run,
                        action_data.get("query"),
                    )

                elif action == "memory_search":
                    output = _execute_memory_search(
                        run,
                        action_data.get("query"),
                    )

                elif action == "write_file":
                    output = _execute_write_file(
                        run,
                        action_data.get("filename"),
                        action_data.get("content"),
                    )

                elif action == "needs_input":
                    question = str(
                        action_data.get("question")
                        or "The agent needs additional input before continuing."
                    ).strip()[:5000]
                    output = question
                    finish_agent_step(
                        user_id,
                        step["id"],
                        "waiting_input",
                        output,
                    )
                    _log_step(run, step, action_data, output, "waiting_input")
                    mark_agent_waiting_input(user_id, run_id, question)
                    return

                elif action == "final":
                    # The controller only decides *when* to finish. A dedicated
                    # finalizer synthesizes the answer from the persistent ledger,
                    # sources, evidence and artifacts. This avoids wasting the step
                    # budget when a controller correctly selects `final` but omits
                    # a long `answer` field.
                    answer = _forced_final(run)
                    output = answer
                    finish_agent_step(
                        user_id,
                        step["id"],
                        "completed",
                        output,
                    )
                    _log_step(run, step, action_data, output, "completed")
                    return

                else:
                    output = f"Unsupported agent action: {action}"
                    status = "blocked"

            except AgentStoreError as error:
                output = f"Action could not complete: {error}"
                status = "blocked"
            except AgentToolUnavailable:
                raise
            except Exception as error:
                if isinstance(error, AgentCancelled):
                    raise
                output = f"Action error: {error}"
                status = "error"

            output = str(output or "")[:AGENT_STEP_OUTPUT_LIMIT]
            finish_agent_step(
                user_id,
                step["id"],
                status,
                output,
            )
            _log_step(run, step, action_data, output, status)

            refreshed = get_agent_run(user_id, run_id)
            if refreshed and _control_probe(
                refreshed,
                include_pause=True,
                force=True,
            ) == "pause":
                mark_agent_paused(user_id, run_id)
                return

    except AgentCancelled:
        mark_agent_cancelled(user_id, run_id)
    except AgentToolUnavailable as error:
        mark_agent_failed(
            user_id,
            run_id,
            str(error)
            + " Start/restore the required local service, then use Resume to continue "
              "the same agent run with its existing workspace."
        )
    except Exception as error:
        mark_agent_failed(user_id, run_id, str(error))
