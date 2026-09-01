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
from app.services.agent_environment import (
    AgentEnvironmentError,
    add_missing_dependency_to_manifest,
    dependency_manifest_needs_update,
    environment_needs_setup,
    environment_status_for_run,
    format_environment_observation,
    project_environment_allowed,
    setup_project_environment,
)
from app.services.agent_project_planner import (
    active_plan_blocks_on_environment,
    active_plan_matches_current_failure,
    analyze_project_state,
    create_debug_plan,
    execute_project_repair,
    format_debug_plan,
    get_active_debug_plan,
    get_next_project_repair,
    mark_active_plan_exhausted,
    mark_active_plan_resolved,
    mark_active_plan_superseded,
    project_planner_context,
    should_create_debug_plan,
    structured_planner_exhausted_for_current_failure,
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


def _workspace_debug_context(
    run,
    max_total_chars=12000,
    max_file_chars=4000,
):
    """
    Give the local controller a bounded snapshot of the current Python project.

    Multi-file failures often come from contracts spread across imports,
    implementations and tests. Filenames alone are not enough for an 8B
    controller to reason reliably about those relationships.

    This stays entirely local and is never sent to the public-only web-query
    planner.
    """
    files = list_workspace_files(
        run["user_id"],
        run["id"],
    )

    python_names = [
        str(item.get("filename") or "").strip()
        for item in files
        if str(item.get("filename") or "").lower().endswith(".py")
    ]

    if not python_names:
        return "No Python workspace files."

    available = {
        name: name
        for name in python_names
        if name
    }

    priority = []

    def add(name):
        if (
            name
            and name in available
            and name not in priority
        ):
            priority.append(name)

    state = _latest_execution_state(run)
    latest = state.get("execution")

    if latest:
        add(
            str(
                latest.get("filename")
                or ""
            ).strip()
        )

        stderr = str(
            latest.get("stderr")
            or ""
        )

        # Traceback filenames.
        for match in re.findall(
            r'File "[^"]*/([^/"]+\.py)"',
            stderr,
        ):
            add(match)

        # Imported local modules implicated by ImportError/tracebacks.
        for module in re.findall(
            r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)\s+import\b",
            stderr,
        ):
            add(
                module
                + ".py"
            )

        for module in re.findall(
            r"\bimport\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            stderr,
        ):
            add(
                module
                + ".py"
            )

    # Tests are especially useful for understanding the expected contract.
    for name in python_names:
        if (
            name.lower().startswith("test_")
            or name.lower().endswith("_test.py")
            or name.lower() in {
                "tests.py",
                "test.py",
            }
        ):
            add(name)

    # For small projects include the rest. This is the common ATLAS workspace
    # case and is much more useful than making the model guess interfaces.
    for name in python_names:
        add(name)

    blocks = []
    used = 0

    for name in priority:
        if used >= max_total_chars:
            break

        try:
            content = read_workspace_file(
                run["user_id"],
                run["id"],
                name,
                max_chars=max_file_chars,
            )
        except Exception:
            continue

        remaining = max_total_chars - used
        content = str(
            content
            or ""
        )[:remaining]

        block = (
            f"--- {name} ---\n"
            + content
        )

        blocks.append(block)
        used += len(block)

    return (
        "\n\n".join(blocks)
        if blocks
        else "Python workspace files could not be read."
    )


def _deterministic_unverified_answer(
    run,
    state,
):
    """
    Never let model prose overrule the actual sandbox ledger.

    A failed/latest-untested revision can still be a useful completed *run
    cycle*, but it is not a verified successful implementation.
    """
    latest = state.get(
        "execution"
    )

    if latest is None:
        return (
            "NOT VERIFIED — No successful sandbox validation exists for the "
            "current workspace. The run ended before the implementation could "
            "be verified. Use Continue / Revise to keep working in the same run."
        )

    filename = str(
        latest.get(
            "filename"
        )
        or "workspace test"
    )

    if state.get(
        "writes_after"
    ):
        return (
            "NOT VERIFIED — The workspace was modified after its latest sandbox "
            f"execution ({filename}), so the current revision still requires a "
            "re-test. The run reached its current step budget before verification. "
            "Use Continue / Revise to continue the same workspace."
        )

    status = str(
        latest.get(
            "status"
        )
        or "unknown"
    )

    exit_code = latest.get(
        "exit_code"
    )

    stderr = str(
        latest.get(
            "stderr"
        )
        or ""
    ).strip()

    # The tail normally contains the actual exception/assertion instead of
    # pages of traceback setup.
    blocker = stderr[-1400:]

    answer = (
        "NOT VERIFIED — The current workspace did not pass its latest sandbox "
        f"validation ({filename}: status {status}, exit code {exit_code}). "
        "The run reached its current step budget before a verified pass."
    )

    if blocker:
        answer += (
            "\n\nLatest blocker:\n"
            + blocker
        )

    answer += (
        "\n\nUse Continue / Revise to continue this same Agent run with the "
        "existing workspace and history."
    )

    return answer


def _available_actions(run):
    actions = list(base_runner._available_actions(run))
    if _code_enabled(run):
        for name in ("workspace_list", "workspace_read", "run_python"):
            if name not in actions:
                actions.append(name)

        if project_environment_allowed(run["user_id"], run["id"]):
            for name in ("environment_plan", "environment_setup"):
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


def _step_changed_workspace(step):
    """
    A successful write_file OR planner-guided repair can invalidate the last
    test result.

    v2.1.1b only looked for write_file. That meant project_repair could rewrite
    one or several files while the retest guard still believed nothing changed,
    allowing stale repair sequences to run without verification.
    """
    if str(
        step.get(
            "status"
        )
        or ""
    ) != "completed":
        return False

    action = str(
        step.get(
            "action"
        )
        or ""
    )

    if action == "write_file":
        return True

    if action == "environment_plan":
        return True

    if action == "project_repair":
        output = str(
            step.get(
                "output"
            )
            or ""
        )

        return (
            "Planner-guided repair updated "
            in output
        )

    return False


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
            _step_changed_workspace(
                step
            )
            and int(
                step.get(
                    "step_index"
                )
                or 0
            ) > step_index
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


def _environment_context(run):
    try:
        status = environment_status_for_run(
            run["user_id"],
            run["id"],
        )
    except Exception as error:
        return f"Environment status unavailable: {error}"

    requested = list(
        status.get("current_requirements")
        or status.get("requested_requirements")
        or []
    )

    lines = [
        f"Profile: {status.get('profile') or 'strict'}",
        f"Status: {status.get('status') or 'base'}",
        f"Ready: {'yes' if status.get('ready') else 'no'}",
        "Execution network: disabled",
    ]

    if status.get("profile") == "project":
        lines.append("Dependency setup network: allowed only during isolated setup/build")

    if requested:
        lines.append("Requested dependencies: " + ", ".join(requested))

    if status.get("image_tag") or status.get("execution_image"):
        lines.append(
            "Environment image: "
            + str(status.get("execution_image") or status.get("image_tag"))
        )

    if status.get("last_error"):
        lines.append("Last environment error: " + str(status.get("last_error"))[-1800:])

    return "\n".join(lines)[:5000]


def _sandbox_forced_final(run):
    """
    Sandbox-aware finalizer.

    The base finalizer does not know whether code was actually re-tested after
    a rewrite. This prompt receives the execution ledger and an explicit
    deterministic verification state so it cannot truthfully claim tests pass
    when they were never re-run.
    """
    state = _latest_execution_state(run)
    verification = _sandbox_verification_summary(run)

    system_prompt = (
        "You are finishing a persistent private local coding-agent run. "
        "Synthesize the useful result from the workspace, step ledger, and "
        "sandbox execution history. Do not invent successful testing. "
        "The SANDBOX VERIFICATION STATE below is authoritative. "
        "You may say tests/code passed only when it says VERIFIED. "
        "If it says NOT VERIFIED, clearly state that the current workspace "
        "still requires a successful re-test and describe the latest observed "
        "failure or remaining work. The answer string may use concise Markdown headings, "
        "lists, tables, and code blocks when they improve clarity. Return ONLY JSON with "
        "keys: answer (string), evidence (array), artifacts (array)."
    )

    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nSANDBOX VERIFICATION STATE:\n"
        + verification
        + "\n\nWORKSPACE FILES:\n"
        + _workspace_catalog(run)
        + "\n\nPROJECT CONTRACT / DEBUG STATE:\n"
        + project_planner_context(run)
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

    # Model wording can never override recorded execution truth.
    if not state.get("verified"):
        data["answer"] = _deterministic_unverified_answer(
            run,
            state,
        )

    return base_runner._finish_with_final(run, data)


def _plan_next_action(run):
    available = _available_actions(run)
    current_step = int(run.get("current_step") or 0)
    remaining = max(0, int(run.get("max_steps") or 6) - current_step)

    # v2.2 dependency-aware environment loop. Manifest changes and dependency
    # setup are infrastructure actions and must happen BEFORE re-testing a file
    # that previously failed because its dependency was unavailable.
    if (
        project_environment_allowed(run["user_id"], run["id"])
        and "environment_plan" in available
        and dependency_manifest_needs_update(run["user_id"], run["id"])
        and remaining > 0
    ):
        return {
            "action": "environment_plan",
            "reason": (
                "The current project source or latest sandbox failure shows an "
                "undeclared third-party dependency. Add a sanitized dependency manifest "
                "without changing the requested application architecture."
            ),
            "model": "deterministic",
        }

    if (
        project_environment_allowed(run["user_id"], run["id"])
        and "environment_setup" in available
        and environment_needs_setup(run["user_id"], run["id"])
        and remaining > 0
    ):
        return {
            "action": "environment_setup",
            "reason": (
                "Build or reuse the isolated dependency image for the current "
                "requirements.txt before executing the project again."
            ),
            "model": "deterministic",
        }

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

    # v2.1.1b: maintain a persistent project contract and use it to stop the
    # normal 8B loop from repeatedly guessing at the same cross-file failure.
    project_analysis = analyze_project_state(run)
    project_execution = project_analysis["execution"]
    project_latest = project_execution.get("latest")

    if (
        project_latest
        and str(project_latest.get("status") or "") == "success"
        and int(project_latest.get("exit_code") or 0) == 0
    ):
        mark_active_plan_resolved(
            run["user_id"],
            run["id"],
        )

    # Every planner-guided code repair is now re-tested before another repair.
    # If that test produces a DIFFERENT failure fingerprint, the previous plan
    # did useful work but is now stale. Discard its remaining sequence and build
    # a fresh plan around the new reality instead of applying old assumptions.
    active_plan = get_active_debug_plan(
        run["user_id"],
        run["id"],
    )

    current_failure_fingerprint = (
        project_execution[
            "failure"
        ].get(
            "fingerprint"
        )
    )

    if (
        active_plan
        and project_latest
        and not (
            str(
                project_latest.get(
                    "status"
                )
                or ""
            )
            == "success"
            and int(
                project_latest.get(
                    "exit_code"
                )
                or 0
            )
            == 0
        )
        and not active_plan_matches_current_failure(
            run["user_id"],
            run["id"],
            current_failure_fingerprint,
        )
    ):
        mark_active_plan_superseded(
            run["user_id"],
            run["id"],
        )

        active_plan = None

    planned_repair = get_next_project_repair(
        run["user_id"],
        run["id"],
    )

    if (
        planned_repair
        and project_latest
        and not (
            str(project_latest.get("status") or "") == "success"
            and int(project_latest.get("exit_code") or 0) == 0
        )
        and remaining > 0
    ):
        return {
            "action": "project_repair",
            "reason": (
                "Follow the persistent project-contract recovery plan instead "
                "of making another unstructured cross-file guess."
            ),
            "model": planned_repair["plan"]["planner_model"],
            "plan_id": planned_repair["plan"]["id"],
            "repair_index": planned_repair["repair_index"],
            "filename": planned_repair["repair"].get("file"),
        }

    if (
        active_plan
        and project_latest
        and not planned_repair
        and not active_plan["plan"].get("blocked_by_environment")
    ):
        # The prior structured repair sequence has been consumed but the test
        # still fails. Close it so a fresh plan can use the new failure state.
        mark_active_plan_exhausted(
            run["user_id"],
            run["id"],
        )

    if (
        project_latest
        and structured_planner_exhausted_for_current_failure(
            run,
            project_analysis,
        )
    ):
        return {
            "action": "final",
            "reason": (
                "The structured planner has already attempted multiple recovery "
                "plans against the same unchanged failure. Stop the loop and "
                "report the exact blocker instead of spending more steps on the "
                "same hypothesis."
            ),
            "model": "deterministic",
        }

    if (
        project_latest
        and should_create_debug_plan(
            run,
            project_analysis,
        )
        and remaining > 0
    ):
        return {
            "action": "project_plan",
            "reason": (
                "Build a structured project contract/recovery plan because "
                "the current failure is cross-file, repeated, or stalled."
            ),
            "model": "adaptive",
        }

    if (
        active_plan_blocks_on_environment(
            run["user_id"],
            run["id"],
        )
        and not project_environment_allowed(
            run["user_id"],
            run["id"],
        )
    ):
        return {
            "action": "final",
            "reason": (
                "The structured project plan identified a sandbox environment "
                "dependency blocker. Preserve the requested architecture and "
                "report the limitation instead of rewriting it away."
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
        "MULTI-FILE CONTRACTS:\n"
        "ATLAS now maintains a deterministic PROJECT CONTRACT containing modules, imports, "
        "symbols, signatures, callers, tests, failure fingerprints and progress state. Use it "
        "as authoritative structural evidence. For import errors, missing symbols, constructor/"
        "signature mismatches, or tests that disagree with implementation, reconcile the whole "
        "contract. Do not ping-pong rename one file while leaving callers/tests inconsistent. "
        "If a structured debug plan exists, follow it rather than inventing a competing repair. "
        "Prefer the smallest coherent cross-file repair.\n\n"
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
        "In Strict profile prefer standard-library/preinstalled dependencies. In Project "
        "profile preserve legitimately requested third-party frameworks and use the controlled "
        "requirements/environment flow rather than rewriting the architecture merely to avoid "
        "a dependency. Return ONLY one JSON object."
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
        + "\n\nPERSISTENT PROJECT CONTRACT / DEBUG STATE:\n"
        + project_planner_context(run, project_analysis)
        + "\n\nCURRENT WORKSPACE CONTENT (LOCAL, BOUNDED):\n"
        + _workspace_debug_context(run)
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
                + "\nRe-plan from the current workspace, CURRENT WORKSPACE CONTENT, "
                + "execution history and ledger. Do not repeat the invalid action. "
                + "For multi-file failures, reconcile the implementation/test contract "
                + "before editing. Return strict JSON only."
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


def _execute_environment_plan(run):
    result = add_missing_dependency_to_manifest(
        run["user_id"],
        run["id"],
    )
    return (
        f"Updated {result['file']['filename']} for missing import "
        f"{result['module']} -> PyPI package {result['package']}.\n"
        "The manifest contains only sanitized PyPI package specifications. "
        "ATLAS will build/reuse the isolated Project dependency image before re-testing."
    )[:base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_environment_setup(run):
    result = setup_project_environment(
        run["user_id"],
        run["id"],
        cancel_check=lambda: base_runner._control_probe(run, force=True),
    )
    return format_environment_observation(result)[:base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_project_plan(run):
    analysis = analyze_project_state(run)
    plan = create_debug_plan(
        run,
        analysis,
    )
    return format_debug_plan(plan)


def _execute_project_repair(run):
    return execute_project_repair(run)


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
                # Verification tail:
                # If the final nominal step changed the workspace, grant exactly
                # one deterministic sandbox verification step. This does not give
                # the LLM another planning/editing step; it only tests the current
                # revision so a run never ends merely because the repair consumed
                # the last budget slot.
                retest_target = _required_retest_target(run)

                if (
                    retest_target
                    and "run_python" in _available_actions(run)
                ):
                    verification_step = begin_agent_step(
                        user_id,
                        run_id,
                        phase="verification",
                        action="run_python",
                        tool_name="agent.sandbox.python",
                        reason=(
                            "Mandatory final verification of the workspace revision "
                            "created on the last nominal step."
                        ),
                        input_data={
                            "filename": retest_target,
                            "verification_tail": True,
                        },
                    )

                    verification_output = ""
                    verification_status = "completed"

                    try:
                        verification_output = _execute_run_python(
                            run,
                            verification_step,
                            retest_target,
                        )
                    except AgentSandboxError as error:
                        verification_output = (
                            f"Sandbox action could not complete: {error}"
                        )
                        verification_status = "error"
                    except Exception as error:
                        if isinstance(
                            error,
                            base_runner.AgentCancelled,
                        ):
                            raise

                        verification_output = (
                            f"Action error: {error}"
                        )
                        verification_status = "error"

                    verification_output = str(
                        verification_output
                        or ""
                    )[:base_runner.AGENT_STEP_OUTPUT_LIMIT]

                    finish_agent_step(
                        user_id,
                        verification_step["id"],
                        verification_status,
                        verification_output,
                    )

                    base_runner._log_step(
                        run,
                        verification_step,
                        {
                            "action": "run_python",
                            "filename": retest_target,
                            "verification_tail": True,
                            "reason": (
                                "Mandatory final verification of the workspace "
                                "revision created on the last nominal step."
                            ),
                        },
                        verification_output,
                        verification_status,
                    )

                    run = get_agent_run(
                        user_id,
                        run_id,
                    ) or run

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
                "environment_plan": "agent.environment.plan",
                "environment_setup": "agent.environment.setup",
                "project_plan": "agent.project.plan",
                "project_repair": "agent.project.repair",
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
                elif action == "environment_plan":
                    output = _execute_environment_plan(run)
                elif action == "environment_setup":
                    output = _execute_environment_setup(run)
                elif action == "project_plan":
                    output = _execute_project_plan(run)
                elif action == "project_repair":
                    output = _execute_project_repair(run)
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
            except AgentEnvironmentError as error:
                output = f"Project environment action could not complete: {error}"
                status = "error"
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
