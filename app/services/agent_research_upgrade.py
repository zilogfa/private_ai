"""
v1.9.2 Agent research/evidence intelligence.

This module deliberately patches the v1.9 agent runner at application startup
instead of duplicating the persistent run engine. The upgrade is intentionally
additive: existing agent runs/workspaces remain valid.

Goals:
- make web research genuinely iterative instead of one-search/one-answer
- investigate missing criteria before finalizing when budget permits
- avoid repeating identical public queries
- reserve user questions for actual user decisions/facts
- generate structured evidence with explicit uncertainty states
- produce clearer candidate-oriented final reports
"""

import json
import re

from app.config import DEFAULT_MODEL
from app.services.agents import (
    list_agent_artifacts,
    list_agent_document_sources,
    list_agent_evidence,
    list_agent_sources,
    list_agent_steps,
    replace_agent_evidence,
)


_APPLIED = False

# Research runs should normally do more than one public discovery pass.
MIN_WEB_SEARCHES_BEFORE_FINAL = 2
MIN_PUBLIC_OBSERVATIONS_BEFORE_FINAL = 3
MAX_PUBLIC_QUERY_HISTORY = 10

_INTERACTION_WORDS = re.compile(
    r"\b(?:ask me|check with me|let me choose|my choice|wait for my|"
    r"before choosing|before deciding|approval|approve|confirm with me)\b",
    re.IGNORECASE,
)


def _safe_json_object(runner, text, label):
    return runner._safe_json_object(text, label)


def _completed_public_steps(runner, run):
    steps = runner.list_agent_steps(run["user_id"], run["id"])
    return [
        step
        for step in steps
        if str(step.get("status") or "") == "completed"
        and str(step.get("action") or "") in {"web_search", "web_fetch"}
    ]


def _successful_search_count(runner, run):
    count = 0
    for step in runner.list_agent_steps(run["user_id"], run["id"]):
        if (
            str(step.get("action") or "") == "web_search"
            and str(step.get("status") or "") == "completed"
        ):
            output = str(step.get("output") or "").lower()
            if "public search query:" in output and "public search failed" not in output:
                count += 1
    return count


def _public_source_count(runner, run):
    return len(runner.list_agent_sources(run["user_id"], run["id"]))


def _unfetched_source_keys(runner, run):
    """
    Prefer explicit fetches for sources whose stored content is still thin.
    Search orchestration may already fetch some pages; only select sources where
    an extra direct fetch can plausibly add information.
    """
    candidates = []
    for source in runner.list_agent_sources(run["user_id"], run["id"]):
        content = str(source.get("content") or "").strip()
        snippet = str(source.get("snippet") or "").strip()
        if len(content) < 900 or content == snippet:
            candidates.append(str(source.get("source_key") or "").strip())
    return [key for key in candidates if key]


def _research_needs_depth(runner, run):
    if not run.get("allow_web"):
        return False

    remaining = max(
        0,
        int(run.get("max_steps") or 6) - int(run.get("current_step") or 0),
    )
    if remaining <= 1:
        return False

    searches = _successful_search_count(runner, run)
    observations = len(_completed_public_steps(runner, run))

    if searches < MIN_WEB_SEARCHES_BEFORE_FINAL:
        return True

    if observations < MIN_PUBLIC_OBSERVATIONS_BEFORE_FINAL:
        return True

    return False


def _goal_explicitly_wants_interaction(run):
    return bool(_INTERACTION_WORDS.search(str(run.get("goal") or "")))


def _public_query_history(runner, run):
    queries = []
    for source in runner.list_agent_sources(run["user_id"], run["id"]):
        query = " ".join(str(source.get("query") or "").split()).strip()
        if query and query.lower() not in {q.lower() for q in queries}:
            queries.append(query)
    return queries[-MAX_PUBLIC_QUERY_HISTORY:]


def _enhanced_next_public_query(runner, run):
    prior_queries, public_summary = runner._public_research_summary(run)
    prior_queries = list(dict.fromkeys(prior_queries + _public_query_history(runner, run)))

    system_prompt = (
        "You are the PUBLIC-ONLY research subplanner for a private local agent. "
        "You never see private memory or local documents. Produce ONE concise public "
        "web-search query that advances the user's original public goal. This is an "
        "ITERATIVE research loop, so each query should have a distinct job.\n\n"
        "Rules:\n"
        "- Preserve hard public constraints such as location/ZIP, price, acreage, date, "
        "product/model names, and other explicit criteria.\n"
        "- Do not repeat a prior query with cosmetic wording changes.\n"
        "- If candidate results already exist, search the most important MISSING criterion "
        "rather than restarting broad discovery.\n"
        "- For property/land research, useful follow-up angles can include zoning, parcel "
        "records, county assessor/GIS, critical areas, wetlands, access, utilities, septic, "
        "well feasibility, setbacks, and buildability. Use only angles relevant to the goal.\n"
        "- Prefer authoritative/official-source wording when verification is needed.\n"
        "- Never add private facts not present in the original public goal/history.\n"
        "Return ONLY JSON: {\"query\":\"...\", \"purpose\":\"short operational purpose\"}."
    )

    user_prompt = (
        "ORIGINAL PUBLIC GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nPRIOR PUBLIC QUERIES:\n"
        + ("\n".join(prior_queries) if prior_queries else "None")
        + "\n\nPUBLIC SOURCE SUMMARY:\n"
        + (public_summary or "No public results yet.")
    )

    raw, _ = runner._run_model(
        run,
        system_prompt,
        user_prompt,
        response_format="json",
        model_override=DEFAULT_MODEL,
    )
    data = _safe_json_object(runner, raw, "public-search subplanner")

    query = runner._public_query_scrub(data.get("query"))
    if not query:
        query = runner._public_query_scrub(run.get("goal"))

    if not query:
        raise runner.AgentExecutionError(
            "The agent could not create a safe public search query."
        )

    # Protect against the 8B model returning the exact same query repeatedly.
    normalized = " ".join(query.lower().split())
    prior_normalized = {" ".join(q.lower().split()) for q in prior_queries}

    if normalized in prior_normalized:
        # Deterministic public-only fallback: keep the original public goal and
        # request a verification angle. No private context is introduced.
        suffixes = (
            " official records verification",
            " zoning buildability official records",
            " alternative listings verification",
            " county records requirements",
        )
        base = runner._public_query_scrub(run.get("goal"))
        for suffix in suffixes:
            candidate = runner._public_query_scrub((base + suffix).strip())
            if candidate and " ".join(candidate.lower().split()) not in prior_normalized:
                return candidate

    return query


def _enhanced_plan_next_action(runner, run):
    """
    Let the normal 8B controller reason, but add a deterministic research-depth
    safety net around it. This prevents a capable model from prematurely
    finalizing after a single shallow search.
    """
    data = runner._ORIGINAL_PLAN_NEXT_ACTION(run)
    action = str(data.get("action") or "").strip().lower()

    available = runner._available_actions(run)
    remaining = max(
        0,
        int(run.get("max_steps") or 6) - int(run.get("current_step") or 0),
    )

    if action == "final" and remaining > 1 and _research_needs_depth(runner, run):
        unfetched = _unfetched_source_keys(runner, run)
        if "web_fetch" in available and unfetched:
            data["action"] = "web_fetch"
            data["source_key"] = unfetched[0]
            data["reason"] = (
                "Verify an existing public candidate/source before finalizing."
            )
            return data

        if "web_search" in available:
            data["action"] = "web_search"
            data["reason"] = (
                "Continue public research because important criteria remain "
                "under-investigated."
            )
            return data

    if action == "needs_input" and not _goal_explicitly_wants_interaction(run):
        # Do not ask the user for permission to perform normal research work.
        # If the agent has a research action left, continue autonomously.
        if "web_search" in available and remaining > 1:
            data["action"] = "web_search"
            data["reason"] = (
                "Continue with an alternative public research strategy without "
                "requiring user permission."
            )
            data["question"] = ""
            return data

        unfetched = _unfetched_source_keys(runner, run)
        if "web_fetch" in available and unfetched and remaining > 1:
            data["action"] = "web_fetch"
            data["source_key"] = unfetched[0]
            data["reason"] = (
                "Inspect an existing public source before requesting user input."
            )
            data["question"] = ""
            return data

    return data


def _evidence_source_excerpt(runner, run):
    blocks = []

    for source in runner.list_agent_sources(run["user_id"], run["id"]):
        key = str(source.get("source_key") or "")
        title = str(source.get("title") or "Web source")
        content = str(
            source.get("content")
            or source.get("snippet")
            or ""
        ).strip()
        blocks.append(
            f"{key} | {title}\nURL: {source.get('url') or ''}\n"
            + content[:2200]
        )

    for source in runner.list_agent_document_sources(run["user_id"], run["id"]):
        key = str(source.get("source_key") or "")
        page = source.get("page_number")
        location = f" page {page}" if page else ""
        blocks.append(
            f"{key} | {source.get('document_name') or 'document'}{location}\n"
            + str(source.get("content") or "")[:2200]
        )

    return "\n\n".join(blocks)[-16000:]


def _enhanced_forced_final(runner, run):
    """
    Candidate/evidence-aware finalization. The finalizer is explicitly told that
    absence of proof is not proof of rejection.
    """
    system_prompt = (
        "You are the evidence finalizer for a persistent private local research agent. "
        "Produce the most useful supported result from the run. Do NOT invent facts and "
        "do NOT discard a potentially useful candidate merely because one criterion could "
        "not be verified.\n\n"
        "Evidence states:\n"
        "- confirmed: directly supported by reliable recorded evidence.\n"
        "- likely: multiple clues support it, but an important part is not fully confirmed.\n"
        "- unverified: plausible/candidate information exists, but a required criterion "
        "still needs verification.\n"
        "- conflicting: recorded sources disagree materially.\n"
        "- rejected: recorded evidence actually contradicts a required criterion.\n\n"
        "CRITICAL RULE: 'not stated', 'not found', or 'not verified' is normally "
        "UNVERIFIED, not REJECTED.\n\n"
        "For comparison/research goals, preserve promising candidates and rank/group them "
        "by evidence state. State exactly what remains to verify. Use source keys such as "
        "S1/S2/D1. If a table would communicate candidates clearly, use a Markdown table "
        "inside the answer.\n\n"
        "Return ONLY JSON with:\n"
        "{\n"
        "  \"answer\": \"supported final answer\",\n"
        "  \"evidence\": [\n"
        "    {\"claim\":\"...\",\"status\":\"confirmed|likely|unverified|conflicting|rejected\","
        "\"source_refs\":[\"S1\"],\"notes\":\"what supports or remains missing\"}\n"
        "  ],\n"
        "  \"artifacts\": []\n"
        "}."
    )

    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nSOURCE CATALOG:\n"
        + (runner._source_catalog(run) or "None")
        + "\n\nSOURCE EXCERPTS:\n"
        + (_evidence_source_excerpt(runner, run) or "None")
        + "\n\nRUN LEDGER:\n"
        + (runner._step_ledger(run) or "No observations were recorded.")
        + "\n\nUSER INPUT:\n"
        + runner._inputs_text(run)
        + "\n\nEXISTING ARTIFACTS:\n"
        + (
            ", ".join(
                str(item.get("filename") or "artifact")
                for item in runner.list_agent_artifacts(
                    run["user_id"], run["id"]
                )
            )
            or "None"
        )
    )

    raw, _ = runner._run_model(
        run,
        system_prompt,
        user_prompt,
        response_format="json",
    )
    data = _safe_json_object(runner, raw, "agent evidence finalizer")
    return runner._finish_with_final(run, data)


def _postprocess_search_output(runner, run, output, metadata):
    """
    Add a small progress hint to the step output. This is visible in the
    workspace and helps diagnose whether a research run is broadening/refining.
    """
    searches = _successful_search_count(runner, run)
    sources = _public_source_count(runner, run)
    suffix = (
        f"\n\nResearch progress: {searches} completed public search pass(es), "
        f"{sources} unique public source(s) recorded."
    )
    return (str(output or "") + suffix)[: runner.AGENT_STEP_OUTPUT_LIMIT], metadata


def apply_agent_research_upgrade():
    global _APPLIED
    if _APPLIED:
        return

    import app.services.agent_runner as runner

    # Keep references for the wrapper to use without recursion.
    if not hasattr(runner, "_ORIGINAL_PLAN_NEXT_ACTION"):
        runner._ORIGINAL_PLAN_NEXT_ACTION = runner._plan_next_action

    original_execute_web_search = runner._execute_web_search

    def patched_next_public_query(run):
        return _enhanced_next_public_query(runner, run)

    def patched_plan_next_action(run):
        return _enhanced_plan_next_action(runner, run)

    def patched_forced_final(run):
        return _enhanced_forced_final(runner, run)

    def patched_execute_web_search(run):
        output, metadata = original_execute_web_search(run)
        return _postprocess_search_output(runner, run, output, metadata)

    runner._next_public_query = patched_next_public_query
    runner._plan_next_action = patched_plan_next_action
    runner._forced_final = patched_forced_final
    runner._execute_web_search = patched_execute_web_search

    _APPLIED = True
