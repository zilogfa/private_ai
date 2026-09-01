import re
import time

from app.database import user_has_permission
from app.services import agent_runner as base_runner
from app.services.agents import (
    AgentStoreError,
    begin_agent_step,
    finish_agent_step,
    get_agent_run,
    list_agent_steps,
    mark_agent_cancelled,
    mark_agent_failed,
    mark_agent_paused,
    mark_agent_waiting_input,
)
from app.services.agent_sandbox import (
    AGENT_CODE_PERMISSION,
    AgentSandboxError,
    AgentSandboxUnavailable,
    agent_run_allows_code,
    format_execution_observation,
    list_agent_sandbox_executions,
    list_workspace_files,
    read_workspace_file,
    run_python_sandbox,
    write_workspace_file,
)

_CODE_GOAL_RE = re.compile(
    r"\b(?:code|coding|python|script|program|app|application|algorithm|function|"
    r"class|calculator|test|tests|unit test|debug|fix|rewrite|refactor|implement|build)\b",
    re.IGNORECASE,
)


def _code_enabled(run):
    return bool(
        run
        and agent_run_allows_code(run["user_id"], run["id"])
        and user_has_permission(run["user_id"], AGENT_CODE_PERMISSION)
    )


def _workspace_catalog(run):
    files = list_workspace_files(run["user_id"], run["id"])
    if not files:
        return "No workspace files yet."
    return "\n".join(
        f"- {item['filename']} ({item['size_bytes']} bytes)" for item in files
    )[:8000]


def _execution_catalog(run):
    rows = list_agent_sandbox_executions(run["user_id"], run["id"], limit=8)
    if not rows:
        return "No sandbox executions yet."
    blocks = []
    for item in rows[-8:]:
        block = (
            f"{item.get('filename')} | {item.get('status')} | "
            f"exit={item.get('exit_code')} | {item.get('duration_ms')} ms"
        )
        stdout_text = str(item.get("stdout") or "").strip()
        stderr_text = str(item.get("stderr") or "").strip()
        if stdout_text:
            block += "\nstdout: " + stdout_text[:1200]
        if stderr_text:
            block += "\nstderr: " + stderr_text[:1600]
        blocks.append(block)
    return "\n\n".join(blocks)[-10000:]


def _available_actions(run):
    actions = list(base_runner._available_actions(run))
    if _code_enabled(run):
        for name in ("workspace_list", "workspace_read", "run_python"):
            if name not in actions:
                actions.append(name)
    return actions


def _latest_python_target(run):
    names = [
        item["filename"]
        for item in list_workspace_files(run["user_id"], run["id"])
        if str(item.get("filename") or "").lower().endswith(".py")
    ]
    if not names:
        return None
    lower_map = {name.lower(): name for name in names}
    for preferred in (
        "test_main.py",
        "test_calculator.py",
        "tests.py",
        "test.py",
        "main.py",
        "app.py",
        "calculator.py",
    ):
        if preferred in lower_map:
            return lower_map[preferred]
    tests = [name for name in names if name.lower().startswith("test_")]
    return tests[-1] if tests else names[-1]


def _has_successful_execution(run):
    return any(
        str(item.get("status") or "") == "success"
        and int(item.get("exit_code") or 0) == 0
        for item in list_agent_sandbox_executions(
            run["user_id"], run["id"], limit=50
        )
    )


def _latest_execution_state(run):
    """
    Return the latest sandbox execution plus whether workspace code changed
    afterward. Any post-execution rewrite invalidates the previous test result
    until another sandbox execution occurs.
    """
    executions = list_agent_sandbox_executions(
        run["user_id"],
        run["id"],
        limit=100,
    )
    if not executions:
        return {
            "execution": None,
            "step_index": 0,
            "writes_after": [],
            "verified": False,
        }

    latest = executions[-1]
    step_id = latest.get("step_id")
    steps = list_agent_steps(
        run["user_id"],
        run["id"],
    )

    step_index = 0
    if step_id is not None:
        for step in steps:
            if int(step.get("id") or 0) == int(step_id):
                step_index = int(step.get("step_index") or 0)
                break

    writes_after = [
        step
        for step in steps
        if (
            str(step.get("action") or "") == "write_file"
            and str(step.get("status") or "") == "completed"
            and int(step.get("step_index") or 0) > step_index
        )
    ]

    verified = (
        str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
        and not writes_after
    )

    return {
        "execution": latest,
        "step_index": step_index,
        "writes_after": writes_after,
        "verified": verified,
    }


def _required_retest_target(run):
    """
    After a sandbox execution, the first successful code rewrite must be
    followed by another execution before the controller may keep rewriting.

    This is the deterministic guard that turns:
        fail -> write -> write -> write...
    into:
        fail -> write -> re-test -> observe -> next decision
    """
    state = _latest_execution_state(run)
    latest = state["execution"]

    if not latest or not state["writes_after"]:
        return None

    filename = str(latest.get("filename") or "").strip()
    available = {
        item["filename"]
        for item in list_workspace_files(
            run["user_id"],
            run["id"],
        )
    }

    if filename and filename in available:
        return filename

    return _latest_python_target(run)


def _sandbox_verification_summary(run):
    state = _latest_execution_state(run)
    latest = state["execution"]

    if latest is None:
        return (
            "NOT VERIFIED: no sandbox execution has been recorded for the "
            "current workspace."
        )

    if state["writes_after"]:
        return (
            "NOT VERIFIED: workspace file(s) were changed after the latest "
            f"sandbox execution of {latest.get('filename')}. A re-test is required."
        )

    status = str(latest.get("status") or "unknown")
    exit_code = latest.get("exit_code")

    if state["verified"]:
        return (
            "VERIFIED: the latest sandbox execution "
            f"({latest.get('filename')}) succeeded with exit code 0."
        )

    return (
        "NOT VERIFIED: the latest sandbox execution "
        f"({latest.get('filename')}) ended with status {status} "
        f"and exit code {exit_code}."
    )


def _sandbox_forced_final(run):
    """
    Sandbox-aware finalizer.

    The base finalizer does not know whether code was actually re-tested after
    a rewrite. This prompt receives the execution ledger and an explicit
    deterministic verification state so it cannot truthfully claim tests pass
    when they were never re-run.
    """
    verification = _sandbox_verification_summary(run)

    system_prompt = (
        "You are finishing a persistent private local coding-agent run. "
        "Synthesize the useful result from the workspace, step ledger, and "
        "sandbox execution history. Do not invent successful testing. "
        "The SANDBOX VERIFICATION STATE below is authoritative. "
        "You may say tests/code passed only when it says VERIFIED. "
        "If it says NOT VERIFIED, clearly state that the current workspace "
        "still requires a successful re-test and describe the latest observed "
        "failure or remaining work. Return ONLY JSON with keys: answer (string), "
        "evidence (array), artifacts (array)."
    )

    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nSANDBOX VERIFICATION STATE:\n"
        + verification
        + "\n\nWORKSPACE FILES:\n"
        + _workspace_catalog(run)
        + "\n\nSANDBOX EXECUTION HISTORY:\n"
        + _execution_catalog(run)
        + "\n\nSOURCE CATALOG:\n"
        + (base_runner._source_catalog(run) or "None")
        + "\n\nRUN LEDGER:\n"
        + (base_runner._step_ledger(run) or "No observations were recorded.")
        + "\n\nUSER INPUT:\n"
        + base_runner._inputs_text(run)
    )

    raw, _ = base_runner._run_model(
        run,
        system_prompt,
        user_prompt,
        response_format="json",
    )
    data = base_runner._safe_json_object(
        raw,
        "sandbox agent finalizer",
    )
    return base_runner._finish_with_final(run, data)


def _plan_next_action(run):
    available = _available_actions(run)
    current_step = int(run.get("current_step") or 0)
    remaining = max(0, int(run.get("max_steps") or 6) - current_step)

    # Deterministic engineering-loop guard: once code has been changed after a
    # sandbox run, test that revision before allowing another speculative edit.
    retest_target = _required_retest_target(run)
    if (
        retest_target
        and "run_python" in available
        and remaining > 0
    ):
        return {
            "action": "run_python",
            "filename": retest_target,
            "reason": (
                "Re-test the current workspace revision before making another "
                "code change or claiming completion."
            ),
            "model": "deterministic",
        }

    system_prompt = (
        "You are the controller for a persistent private local AI agent with a "
        "controlled Docker Python sandbox. Choose exactly ONE next action. Do useful "
        "work autonomously; do not ask permission for normal research, file inspection, "
        "testing, debugging, or rewriting.\n\n"
        "After a failed execution, make the smallest useful repair and re-run before "
        "making another speculative repair. After a successful execution with no later "
        "workspace changes, prefer final unless the goal explicitly requires unfinished "
        "work. Never spend remaining steps rewriting the same already-tested file without "
        "a concrete reason.\n\n"
        "CODING LOOP:\n"
        "When runnable code is requested and sandbox execution is allowed, prefer an "
        "engineering loop: create/update file(s) -> execute/test -> inspect stdout/stderr "
        "-> fix or improve -> re-run -> final. For multi-file work, use workspace_list or "
        "workspace_read when useful. A failed execution is an observation to debug, not a "
        "reason to give up. Never claim code works without a successful recorded sandbox "
        "execution.\n\n"
        "SANDBOX SECURITY:\n"
        "run_python executes only a stored .py workspace file. The disposable container "
        "has no network, no host shell or Docker socket, a read-only workspace mount, a "
        "read-only root filesystem, dropped Linux capabilities, and CPU/memory/PID/time "
        "limits. Do not attempt to bypass these restrictions.\n\n"
        "ACTIONS:\n"
        "- web_search: public search; the existing privacy-isolated query planner chooses "
        "the actual public query.\n"
        "- web_fetch: fetch a recorded public source; include source_key.\n"
        "- document_search: search local indexed documents; include query.\n"
        "- memory_search: search local personal memory; include query.\n"
        "- write_file: create or REWRITE a logical workspace text/source file; include "
        "filename and COMPLETE content. Rewriting the same filename replaces its current "
        "workspace version.\n"
        "- workspace_list: list current logical workspace files.\n"
        "- workspace_read: read one current workspace file; include filename.\n"
        "- run_python: execute one EXISTING .py workspace file in Docker; filename is REQUIRED. "
        "Never use run_python to create/populate CSV, JSON, Markdown, or other non-Python files; "
        "use write_file for those.\n"
        "- needs_input: pause only for a genuinely important user decision/fact or when "
        "the goal explicitly asks to involve the user.\n"
        "- final: finish only when useful work is complete or further progress is not "
        "reasonable within the remaining budget.\n\n"
        "Prefer Python standard-library solutions in v2.0 because arbitrary dependency "
        "installation is intentionally disabled. Return ONLY one JSON object."
    )

    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nUSER INPUT RECEIVED DURING THIS RUN:\n"
        + base_runner._inputs_text(run)
        + "\n\nAVAILABLE ACTIONS:\n"
        + ", ".join(available)
        + f"\n\nSTEP BUDGET REMAINING: {remaining}"
        + "\n\nWORKSPACE FILES:\n"
        + _workspace_catalog(run)
        + "\n\nSANDBOX EXECUTION HISTORY:\n"
        + _execution_catalog(run)
        + "\n\nSOURCE CATALOG:\n"
        + (base_runner._source_catalog(run) or "No sources recorded.")
        + "\n\nRUN LEDGER:\n"
        + (base_runner._step_ledger(run) or "No steps have run yet.")
        + "\n\nJSON KEYS:\naction, reason, query, source_key, filename, content, question"
    )

    last_error = None
    for attempt in range(2):
        retry_note = ""

        if attempt:
            retry_note = (
                "\n\nPrevious action was invalid: "
                + str(
                    last_error
                    or "missing required action data"
                )
                + "\nRe-plan from the current workspace and ledger. "
                + "Do not repeat the invalid action. Return strict JSON only."
            )

        raw, model = base_runner._run_model(
            run,
            system_prompt,
            user_prompt
            + retry_note,
            response_format="json",
        )
        try:
            data = base_runner._safe_json_object(raw, "sandbox agent controller")
        except base_runner.AgentExecutionError as error:
            last_error = error
            continue

        action = str(data.get("action") or "").strip().lower()
        if action not in available:
            last_error = base_runner.AgentExecutionError(
                f"Sandbox agent controller selected unavailable action: {action or 'empty'}"
            )
            continue

        data["action"] = action
        data["model"] = model
        data["reason"] = str(data.get("reason") or "").strip()[:1000]

        workspace_names = {
            str(item.get("filename") or "").strip()
            for item in list_workspace_files(
                run["user_id"],
                run["id"],
            )
            if str(item.get("filename") or "").strip()
        }

        if action == "run_python":
            requested = str(
                data.get("filename")
                or ""
            ).strip()

            target = (
                requested
                or _latest_python_target(
                    run
                )
            )

            if (
                not target
                or target not in workspace_names
                or not target.lower().endswith(".py")
            ):
                last_error = base_runner.AgentExecutionError(
                    "run_python requires the filename of an existing .py workspace file. "
                    "If the task is creating CSV/JSON/Markdown/data, use write_file instead."
                )
                continue

            data["filename"] = target

        elif action == "workspace_read":
            filename = str(
                data.get("filename")
                or ""
            ).strip()

            if (
                not filename
                or filename not in workspace_names
            ):
                last_error = base_runner.AgentExecutionError(
                    "workspace_read requires the filename of an existing workspace file."
                )
                continue

            data["filename"] = filename

        elif action == "write_file":
            filename = str(
                data.get("filename")
                or ""
            ).strip()

            if not filename:
                last_error = base_runner.AgentExecutionError(
                    "write_file requires a workspace filename. Choose a clear filename "
                    "appropriate for the requested artifact."
                )
                continue

            if data.get("content") is None:
                last_error = base_runner.AgentExecutionError(
                    "write_file requires complete file content."
                )
                continue

            data["filename"] = filename

        if (
            action == "final"
            and _CODE_GOAL_RE.search(str(run.get("goal") or ""))
            and remaining > 1
        ):
            target = _latest_python_target(run)
            if target and not _has_successful_execution(run):
                data["action"] = "run_python"
                data["filename"] = target
                data["reason"] = (
                    "Validate the runnable Python workspace before claiming completion."
                )

        return data

    # A malformed filename/action should never strand a persistent run in
    # "running". If the controller twice fails validation, close the loop with a
    # normal finalization step rather than executing an invalid sandbox call.
    return {
        "action": "final",
        "reason": (
            "The controller could not produce a valid next file/sandbox action "
            "after re-planning. Finalize the useful work completed so far and "
            "state any incomplete deliverable clearly."
        ),
        "model": "deterministic",
    }


def _execute_workspace_list(run):
    files = list_workspace_files(run["user_id"], run["id"])
    if not files:
        return "Workspace currently contains no logical files."
    return "\n".join(
        f"- {item['filename']} ({item['size_bytes']} bytes)" for item in files
    )


def _execute_workspace_read(run, filename):
    text = read_workspace_file(run["user_id"], run["id"], filename)
    return (f"Workspace file: {filename}\n\n" + text)[: base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_write_file(run, filename, content):
    result = write_workspace_file(run["user_id"], run["id"], filename, content)
    verb = "Updated" if result.get("updated") else "Created"
    return (
        f"{verb} workspace file: {result['filename']} ({result['size_bytes']} bytes). "
        "It is stored locally and is only executed when the agent explicitly selects "
        "the run_python sandbox action."
    )


def _execute_run_python(run, step, filename):
    try:
        execution = run_python_sandbox(
            run["user_id"],
            run["id"],
            filename,
            step_id=step["id"],
            cancel_check=lambda: base_runner._control_probe(run, force=True),
        )
    except AgentSandboxUnavailable as error:
        raise base_runner.AgentToolUnavailable(
            "Sandboxed code execution is unavailable. " + str(error)
        ) from error
    return format_execution_observation(execution)[: base_runner.AGENT_STEP_OUTPUT_LIMIT]


def execute_agent_run(user_id, run_id):
    """
    Runs without code opt-in continue through the exact v1.9/v1.9.2 runner.
    Only explicitly opted-in code runs use this extended action loop.
    """
    initial = get_agent_run(user_id, run_id)
    if not initial:
        return
    if not _code_enabled(initial):
        return base_runner.execute_agent_run(user_id, run_id)

    started_mono = time.monotonic()
    runtime_limit = max(60, int(base_runner.AGENT_MAX_RUNTIME_SECONDS))

    try:
        while True:
            run = get_agent_run(user_id, run_id)
            if not run:
                return

            base_runner._control_probe(run, force=True)
            if base_runner._control_probe(run, include_pause=True, force=True) == "pause":
                mark_agent_paused(user_id, run_id)
                return

            if time.monotonic() - started_mono > runtime_limit:
                raise base_runner.AgentExecutionError(
                    "Agent runtime budget was reached. Resume the run to continue with its existing workspace."
                )

            if int(run.get("current_step") or 0) >= int(run.get("max_steps") or 6):
                _sandbox_forced_final(run)
                return

            action_data = _plan_next_action(run)
            action = action_data["action"]
            reason = action_data.get("reason") or ""
            tool_name = {
                "web_search": "web.search",
                "web_fetch": "web.fetch",
                "document_search": "document.search",
                "memory_search": "memory.search",
                "write_file": "agent.workspace.write",
                "workspace_list": "agent.workspace.list",
                "workspace_read": "agent.workspace.read",
                "run_python": "agent.sandbox.python",
                "final": "agent.finalize",
                "needs_input": "agent.input.request",
            }.get(action)

            step = begin_agent_step(
                user_id,
                run_id,
                phase="action",
                action=action,
                tool_name=tool_name,
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
                    output, metadata = base_runner._execute_web_search(run)
                    action_data["search_metadata"] = metadata
                elif action == "web_fetch":
                    output = base_runner._execute_web_fetch(run, action_data.get("source_key"))
                elif action == "document_search":
                    output = base_runner._execute_document_search(run, action_data.get("query"))
                elif action == "memory_search":
                    output = base_runner._execute_memory_search(run, action_data.get("query"))
                elif action == "write_file":
                    output = _execute_write_file(
                        run,
                        action_data.get("filename"),
                        action_data.get("content"),
                    )
                elif action == "workspace_list":
                    output = _execute_workspace_list(run)
                elif action == "workspace_read":
                    output = _execute_workspace_read(run, action_data.get("filename"))
                elif action == "run_python":
                    output = _execute_run_python(run, step, action_data.get("filename"))
                elif action == "needs_input":
                    question = str(
                        action_data.get("question")
                        or "The agent needs additional input before continuing."
                    ).strip()[:5000]
                    output = question
                    finish_agent_step(user_id, step["id"], "waiting_input", output)
                    base_runner._log_step(run, step, action_data, output, "waiting_input")
                    mark_agent_waiting_input(user_id, run_id, question)
                    return
                elif action == "final":
                    answer = _sandbox_forced_final(run)
                    output = answer
                    finish_agent_step(user_id, step["id"], "completed", output)
                    base_runner._log_step(run, step, action_data, output, "completed")
                    return
                else:
                    output = f"Unsupported agent action: {action}"
                    status = "blocked"

            except AgentStoreError as error:
                output = f"Action could not complete: {error}"
                status = "blocked"
            except AgentSandboxError as error:
                output = f"Sandbox action could not complete: {error}"
                status = "error"
            except base_runner.AgentToolUnavailable:
                raise
            except Exception as error:
                if isinstance(error, base_runner.AgentCancelled):
                    raise
                output = f"Action error: {error}"
                status = "error"

            output = str(output or "")[: base_runner.AGENT_STEP_OUTPUT_LIMIT]
            finish_agent_step(user_id, step["id"], status, output)
            base_runner._log_step(run, step, action_data, output, status)

            refreshed = get_agent_run(user_id, run_id)
            if (
                refreshed
                and base_runner._control_probe(
                    refreshed, include_pause=True, force=True
                )
                == "pause"
            ):
                mark_agent_paused(user_id, run_id)
                return

    except base_runner.AgentCancelled:
        mark_agent_cancelled(user_id, run_id)
    except base_runner.AgentToolUnavailable as error:
        mark_agent_failed(
            user_id,
            run_id,
            str(error)
            + " Start/restore the required local service, then use Resume to continue "
            "the same agent run with its existing workspace.",
        )
    except Exception as error:
        mark_agent_failed(user_id, run_id, str(error))
