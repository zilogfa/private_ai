import hashlib
import re
import time

from app.config import DEEP_MODEL
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
    list_npm_scripts,
    list_workspace_files,
    read_workspace_file,
    run_node_sandbox,
    run_npm_script_sandbox,
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
from app.services.agent_node_environment import (
    add_missing_node_dependency_to_manifest,
    format_node_environment_observation,
    node_environment_needs_setup,
    node_environment_status_for_run,
    node_manifest_needs_update,
    setup_node_project_environment,
)
from app.services.agent_runtime import (
    RUNTIME_NODE,
    RUNTIME_PYTHON,
    effective_runtime,
    get_agent_run_runtime,
)
from app.services.agent_file_versions import (
    list_workspace_mutations_after,
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
    r"\b(?:code|coding|python|javascript|node|node.js|npm|script|program|app|"
    r"application|frontend|game|algorithm|function|class|calculator|test|tests|"
    r"unit test|debug|fix|rewrite|refactor|implement|build)\b",
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
        runtime = str(item.get("runtime") or "python")
        action = str(item.get("execution_action") or "run_python")
        target = (
            item.get("command")
            if action == "run_npm"
            else item.get("filename")
        )
        block = (
            f"{runtime}:{action} {target} | {item.get('status')} | "
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
    max_total_chars=16000,
    max_file_chars=5000,
):
    """Bounded local source snapshot for the active runtime."""
    files = list_workspace_files(
        run["user_id"],
        run["id"],
    )

    runtime = effective_runtime(
        run
    )

    if runtime == RUNTIME_NODE:
        source_suffixes = (
            ".js",
            ".mjs",
            ".cjs",
            ".jsx",
            ".ts",
            ".tsx",
            ".json",
            ".html",
            ".css",
        )
    else:
        source_suffixes = (
            ".py",
            ".json",
            ".txt",
        )

    names = [
        str(item.get("filename") or "").strip()
        for item in files
        if str(item.get("filename") or "").lower().endswith(source_suffixes)
    ]

    if not names:
        return f"No {runtime} workspace source files."

    available = {
        name: name
        for name in names
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

    state = _latest_execution_state(
        run
    )
    latest = state.get(
        "execution"
    )

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

        if runtime == RUNTIME_PYTHON:
            for match in re.findall(
                r'File "[^"]*/([^/"]+\.py)"',
                stderr,
            ):
                add(match)

            for module in re.findall(
                r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)\s+import\b",
                stderr,
            ):
                add(module + ".py")

            for module in re.findall(
                r"\bimport\s+([A-Za-z_][A-Za-z0-9_]*)\b",
                stderr,
            ):
                add(module + ".py")
        else:
            # Node/Vitest/Jest stack traces commonly include one of these forms.
            for match in re.findall(
                r"(?:/runtime/|file://[^\s]*/)([^\s():]+\.(?:js|mjs|cjs|jsx|ts|tsx))",
                stderr,
            ):
                add(match)

    # Test files and manifests should be visible early because they define the
    # expected project contract even before a JS-specific deterministic planner
    # exists.
    for name in names:
        lower = name.lower()
        if (
            lower.startswith("test_")
            or lower.startswith("test-")
            or ".test." in lower
            or ".spec." in lower
            or lower in {
                "tests.py",
                "test.py",
                "package.json",
                "requirements.txt",
            }
        ):
            add(name)

    for name in names:
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
        content = str(content or "")[:remaining]
        block = f"--- {name} ---\n{content}"
        blocks.append(block)
        used += len(block)

    return (
        "\n\n".join(blocks)
        if blocks
        else "Workspace source files could not be read."
    )


def _latest_node_failure_fingerprint(run):
    rows = [
        item
        for item in list_agent_sandbox_executions(
            run["user_id"],
            run["id"],
            limit=12,
        )
        if str(item.get("runtime") or "python") == RUNTIME_NODE
    ]

    if not rows:
        return {
            "repeated": 0,
            "fingerprint": None,
            "latest": None,
        }

    latest = rows[-1]
    if (
        str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
    ):
        return {
            "repeated": 0,
            "fingerprint": None,
            "latest": latest,
        }

    def fingerprint(item):
        stderr = " ".join(
            str(item.get("stderr") or "").split()
        )[-1800:]
        stderr = re.sub(r"\bline\s+\d+\b", "line #", stderr, flags=re.I)
        stderr = re.sub(r":\d+:\d+\b", ":#:#", stderr)
        raw = "|".join([
            str(item.get("execution_action") or ""),
            str(item.get("filename") or ""),
            stderr,
        ])
        return hashlib.sha1(
            raw.encode("utf-8", errors="ignore")
        ).hexdigest()[:16]

    current = fingerprint(latest)
    repeated = 0
    for item in reversed(rows):
        if (
            str(item.get("status") or "") == "success"
            and int(item.get("exit_code") or 0) == 0
        ):
            break
        if fingerprint(item) == current:
            repeated += 1
        else:
            break

    return {
        "repeated": repeated,
        "fingerprint": current,
        "latest": latest,
    }


def _node_controller_model_override(run):
    # Python already has the richer project-contract escalation layer. Node gets
    # a conservative senior-controller escalation until its own deterministic
    # JS contract planner arrives in a later v2.3.x milestone.
    if str(run.get("model_mode") or "auto").lower() != "auto":
        return None

    failure = _latest_node_failure_fingerprint(run)
    if int(failure.get("repeated") or 0) >= 2:
        return DEEP_MODEL

    return None


def _project_context(run):
    runtime = effective_runtime(run)
    if runtime == RUNTIME_PYTHON:
        return project_planner_context(run)

    failure = _latest_node_failure_fingerprint(run)
    scripts = []
    try:
        scripts = list_npm_scripts(
            run["user_id"],
            run["id"],
        )
    except Exception:
        scripts = []

    lines = [
        "Project kind: Node.js",
        "Deterministic JS symbol/data-flow planner: not enabled in v2.3 foundation.",
        "Use current source, package.json, sandbox output and test/build scripts as truth.",
    ]
    if scripts:
        lines.append("npm scripts: " + ", ".join(scripts))
    if failure.get("fingerprint"):
        lines.append(
            f"latest failure fingerprint: {failure['fingerprint']}"
        )
        lines.append(
            f"same failure repeated: {int(failure.get('repeated') or 0)}"
        )
        if int(failure.get("repeated") or 0) >= 2:
            lines.append(
                "Auto model policy: repeated Node failure escalates the next controller call to Deep."
            )

    return "\n".join(lines)

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
        external = bool(
            state.get(
                "external_mutations"
            )
        )

        change_label = (
            "restored or changed outside the previous sandbox execution"
            if external
            else "modified after its latest sandbox execution"
        )

        return (
            f"NOT VERIFIED — The workspace was {change_label} "
            f"({filename}), so the current revision requires a fresh re-test. "
            "An earlier successful test cannot verify the changed workspace. "
            "Use Continue / Revise to continue the same workspace and re-verify it."
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
    actions = list(
        base_runner._available_actions(run)
    )

    if not _code_enabled(run):
        return actions

    runtime = effective_runtime(
        run
    )

    for name in (
        "workspace_list",
        "workspace_read",
    ):
        if name not in actions:
            actions.append(name)

    if runtime == RUNTIME_NODE:
        for name in (
            "run_node",
            "run_npm",
        ):
            if name not in actions:
                actions.append(name)
    else:
        if "run_python" not in actions:
            actions.append("run_python")

    if project_environment_allowed(
        run["user_id"],
        run["id"],
    ):
        for name in (
            "environment_plan",
            "environment_setup",
        ):
            if name not in actions:
                actions.append(name)

    # The v2.1 deterministic project-contract repair planner is Python-specific.
    # Node uses the generic controller + execution evidence for now.
    if runtime == RUNTIME_PYTHON:
        for name in (
            "project_plan",
            "project_repair",
        ):
            if name not in actions:
                actions.append(name)

    return actions


def _latest_python_target(run):
    names = [
        item["filename"]
        for item in list_workspace_files(
            run["user_id"],
            run["id"],
        )
        if str(item.get("filename") or "").lower().endswith(".py")
    ]
    if not names:
        return None

    lower_map = {
        name.lower(): name
        for name in names
    }

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

    tests = [
        name
        for name in names
        if name.lower().startswith("test_")
    ]
    return tests[-1] if tests else names[-1]


def _latest_node_target(run):
    names = [
        item["filename"]
        for item in list_workspace_files(
            run["user_id"],
            run["id"],
        )
        if str(item.get("filename") or "").lower().endswith(
            (".js", ".mjs", ".cjs")
        )
    ]

    if not names:
        return None

    lower_map = {
        name.lower(): name
        for name in names
    }

    for preferred in (
        "test.js",
        "tests.js",
        "test_app.js",
        "test-app.js",
        "app.test.js",
        "index.test.js",
        "main.js",
        "index.js",
        "app.js",
        "server.js",
    ):
        if preferred in lower_map:
            return lower_map[preferred]

    tests = [
        name
        for name in names
        if (
            name.lower().startswith("test")
            or ".test." in name.lower()
            or ".spec." in name.lower()
        )
    ]

    return tests[-1] if tests else names[-1]


def _preferred_node_script(run):
    try:
        scripts = list_npm_scripts(
            run["user_id"],
            run["id"],
        )
    except Exception:
        scripts = []

    for preferred in (
        "test",
        "check",
        "lint",
        "typecheck",
        "build",
    ):
        if preferred in scripts:
            return preferred

    return None


def _default_validation_action(run):
    runtime = effective_runtime(
        run
    )

    if runtime == RUNTIME_NODE:
        script = _preferred_node_script(
            run
        )
        if script:
            return {
                "action": "run_npm",
                "script": script,
            }

        target = _latest_node_target(
            run
        )
        if target:
            return {
                "action": "run_node",
                "filename": target,
            }
        return None

    target = _latest_python_target(
        run
    )
    if target:
        return {
            "action": "run_python",
            "filename": target,
        }
    return None

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
            "external_mutations": [],
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

    # Workspace truth is broader than the Agent step ledger.
    #
    # User rollback/version restore (and future manual edits, QA handoffs or
    # multi-Agent merges) can mutate the current workspace without creating a
    # write_file/project_repair step. Those changes MUST invalidate an older
    # successful sandbox execution.
    external_mutations = list_workspace_mutations_after(
        run["user_id"],
        run["id"],
        created_after=
            latest.get(
                "created_at"
            ),
        limit=100,
    )

    for mutation in external_mutations:
        writes_after.append(
            {
                "id":
                    mutation.get(
                        "id"
                    ),
                "step_index":
                    None,
                "phase":
                    "workspace",
                "action":
                    mutation.get(
                        "mutation_type"
                    )
                    or "external_change",
                "tool_name":
                    "agent.workspace.external_mutation",
                "status":
                    "completed",
                "reason":
                    mutation.get(
                        "note"
                    ),
                "input":
                    {
                        "artifact_id":
                            mutation.get(
                                "artifact_id"
                            ),
                        "filename":
                            mutation.get(
                                "filename"
                            ),
                        "version_id":
                            mutation.get(
                                "version_id"
                            ),
                    },
                "output":
                    mutation.get(
                        "note"
                    ),
                "started_at":
                    mutation.get(
                        "created_at"
                    ),
                "finished_at":
                    mutation.get(
                        "created_at"
                    ),
                "external_workspace_mutation":
                    True,
            }
        )

    verified = (
        str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
        and not writes_after
    )

    return {
        "execution": latest,
        "step_index": step_index,
        "writes_after": writes_after,
        "external_mutations": external_mutations,
        "verified": verified,
    }


def _required_retest_action(run):
    """Return the exact runtime verification action required after a mutation."""
    state = _latest_execution_state(
        run
    )
    latest = state[
        "execution"
    ]

    if not latest or not state[
        "writes_after"
    ]:
        return None

    runtime = str(
        latest.get(
            "runtime"
        )
        or RUNTIME_PYTHON
    ).lower()
    action = str(
        latest.get(
            "execution_action"
        )
        or (
            "run_node"
            if runtime == RUNTIME_NODE
            else "run_python"
        )
    )

    available_names = {
        item["filename"]
        for item in list_workspace_files(
            run["user_id"],
            run["id"],
        )
    }

    if action == "run_npm":
        command = str(
            latest.get(
                "command"
            )
            or ""
        )
        match = re.search(
            r"npm\s+run\s+([^\s]+)",
            command,
        )
        script = (
            match.group(1)
            if match
            else _preferred_node_script(run)
        )
        if script:
            return {
                "action": "run_npm",
                "script": script,
            }

    filename = str(
        latest.get(
            "filename"
        )
        or ""
    ).strip()

    if action == "run_node":
        if filename and filename in available_names:
            return {
                "action": "run_node",
                "filename": filename,
            }
        target = _latest_node_target(
            run
        )
        return (
            {
                "action": "run_node",
                "filename": target,
            }
            if target
            else _default_validation_action(run)
        )

    if action == "run_python":
        if filename and filename in available_names:
            return {
                "action": "run_python",
                "filename": filename,
            }
        target = _latest_python_target(
            run
        )
        return (
            {
                "action": "run_python",
                "filename": target,
            }
            if target
            else _default_validation_action(run)
        )

    return _default_validation_action(
        run
    )

def _sandbox_verification_summary(run):
    state = _latest_execution_state(run)
    latest = state["execution"]

    if latest is None:
        return (
            "NOT VERIFIED: no sandbox execution has been recorded for the "
            "current workspace."
        )

    if state["writes_after"]:
        if state.get("external_mutations"):
            mutations = state[
                "external_mutations"
            ]

            labels = [
                (
                    f"{item.get('mutation_type') or 'external_change'}"
                    + (
                        f":{item.get('filename')}"
                        if item.get("filename")
                        else ""
                    )
                )
                for item in mutations[-5:]
            ]

            return (
                "NOT VERIFIED: the current workspace changed outside the previous "
                f"sandbox execution of {latest.get('filename')} "
                f"({', '.join(labels)}). A fresh re-test is mandatory."
            )

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
    runtime = effective_runtime(run)

    try:
        status = (
            node_environment_status_for_run(
                run["user_id"],
                run["id"],
            )
            if runtime == RUNTIME_NODE
            else environment_status_for_run(
                run["user_id"],
                run["id"],
            )
        )
    except Exception as error:
        return f"Environment status unavailable: {error}"

    requested = list(
        status.get("current_requirements")
        or status.get("requested_requirements")
        or []
    )

    lines = [
        f"Runtime: {'Node.js' if runtime == RUNTIME_NODE else 'Python'}",
        f"Profile: {status.get('profile') or 'strict'}",
        f"Status: {status.get('status') or 'base'}",
        f"Ready: {'yes' if status.get('ready') else 'no'}",
        "Execution network: disabled",
    ]

    if status.get("profile") == "project":
        registry = "npm registry" if runtime == RUNTIME_NODE else "Python package index"
        lines.append(
            f"Dependency setup network: allowed only during isolated setup/build ({registry})"
        )

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
    runtime = effective_runtime(run)

    # Runtime-specific dependency discovery/setup always happens before a
    # deterministic re-test. This prevents an unavailable dependency from being
    # mistaken for a code defect.
    if (
        project_environment_allowed(run["user_id"], run["id"])
        and "environment_plan" in available
        and remaining > 0
    ):
        needs_manifest = (
            node_manifest_needs_update(run["user_id"], run["id"])
            if runtime == RUNTIME_NODE
            else dependency_manifest_needs_update(run["user_id"], run["id"])
        )
        if needs_manifest:
            return {
                "action": "environment_plan",
                "reason": (
                    "The current project source shows an undeclared npm dependency. "
                    "Update package.json without changing the requested architecture."
                    if runtime == RUNTIME_NODE
                    else (
                        "The current project source or latest sandbox failure shows an "
                        "undeclared Python dependency. Add a sanitized requirements manifest "
                        "without changing the requested application architecture."
                    )
                ),
                "model": "deterministic",
            }

    if (
        project_environment_allowed(run["user_id"], run["id"])
        and "environment_setup" in available
        and remaining > 0
    ):
        needs_setup = (
            node_environment_needs_setup(run["user_id"], run["id"])
            if runtime == RUNTIME_NODE
            else environment_needs_setup(run["user_id"], run["id"])
        )
        if needs_setup:
            return {
                "action": "environment_setup",
                "reason": (
                    "Build or reuse the isolated npm dependency image for package.json "
                    "before executing the Node.js project again."
                    if runtime == RUNTIME_NODE
                    else (
                        "Build or reuse the isolated pip dependency image for the current "
                        "requirements.txt before executing the Python project again."
                    )
                ),
                "model": "deterministic",
            }

    # Any workspace mutation after a recorded test must be followed by the same
    # runtime's verification action before further speculative changes.
    retest = _required_retest_action(run)
    if (
        retest
        and retest.get("action") in available
        and remaining > 0
    ):
        return {
            **retest,
            "reason": (
                "Re-test the current workspace revision before making another "
                "code change or claiming completion."
            ),
            "model": "deterministic",
        }

    project_analysis = None

    # The persistent AST project-contract planner is currently Python-specific.
    # Preserve it unchanged for Python while Node gets a runtime-aware controller
    # and automatic Deep escalation on repeated failures.
    if runtime == RUNTIME_PYTHON:
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

        active_plan = get_active_debug_plan(
            run["user_id"],
            run["id"],
        )

        current_failure_fingerprint = (
            project_execution["failure"].get("fingerprint")
        )

        if (
            active_plan
            and project_latest
            and not (
                str(project_latest.get("status") or "") == "success"
                and int(project_latest.get("exit_code") or 0) == 0
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
                    "The structured planner already attempted multiple recovery plans "
                    "against the same unchanged failure. Report the exact blocker instead "
                    "of burning more steps on the same hypothesis."
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
                    "Build a structured Python project contract/recovery plan because "
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
                    "The Python project plan identified a sandbox dependency blocker. "
                    "Preserve the requested architecture and report the limitation."
                ),
                "model": "deterministic",
            }

    runtime_info = get_agent_run_runtime(
        run["user_id"],
        run["id"],
    )
    runtime_label = runtime_info.get("label") or runtime

    system_prompt = (
        "You are the controller for a persistent private local AI engineering agent "
        f"using the ATLAS {runtime_label} Docker runtime. Choose exactly ONE next action. "
        "Do useful work autonomously; do not ask permission for normal file inspection, "
        "testing, debugging or rewriting.\n\n"
        "ENGINEERING LOOP:\n"
        "Create/update files -> execute the smallest useful test/build -> inspect actual "
        "stdout/stderr -> make a coherent repair -> re-run -> final. A failed execution is "
        "evidence to debug, not a reason to abandon the requested architecture. Never claim "
        "code works without a successful recorded sandbox execution against the current "
        "workspace state.\n\n"
        "MULTI-FILE CONSISTENCY:\n"
        "Treat imports, exports, function/class signatures, callers, manifests and tests as "
        "one project contract. Read related files together before repeatedly renaming or "
        "rewriting one side of an interface. Python runs may expose a deterministic project "
        "contract/debug plan; when present, follow it. Node.js v2.3 uses current source, "
        "package.json and test/build output as the contract and escalates repeated failures "
        "to the stronger reasoning model in Auto mode.\n\n"
        "SANDBOX SECURITY:\n"
        "Execution is isolated in Docker with network OFF, durable source read-only, a writable "
        "disposable runtime, no Docker socket, dropped Linux capabilities, and resource/time "
        "limits. Project dependency setup is separate and may access only the appropriate "
        "package registry using a sanitized manifest. Do not attempt to bypass restrictions.\n\n"
        "ACTIONS:\n"
        "- web_search / web_fetch / document_search / memory_search: existing research tools.\n"
        "- write_file: create or replace a complete logical workspace file.\n"
        "- workspace_list / workspace_read: inspect current workspace files.\n"
        "- run_python: Python only; execute one existing .py file.\n"
        "- run_node: Node.js only; execute one existing .js/.mjs/.cjs file.\n"
        "- run_npm: Node.js only; execute one EXISTING package.json script; include script. "
        "Prefer test for verification and build for frontend/build validation.\n"
        "- environment_plan: add a deterministically detected missing dependency to the "
        "runtime manifest (requirements.txt or package.json).\n"
        "- environment_setup: build/reuse the isolated dependency image.\n"
        "- project_plan / project_repair: Python deterministic debug planner actions only.\n"
        "- needs_input: only for a genuinely important user decision/fact.\n"
        "- final: only when useful work is complete or further progress is not reasonable.\n\n"
        "In Strict profile do not invent dependency downloads. In Project profile preserve "
        "legitimately requested frameworks and use the controlled pip/npm setup instead of "
        "rewriting the project to avoid dependencies. Return ONLY one JSON object."
    )

    project_context = (
        project_planner_context(run, project_analysis)
        if runtime == RUNTIME_PYTHON and project_analysis is not None
        else _project_context(run)
    )

    user_prompt = (
        "GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nACTIVE RUNTIME:\n"
        + f"selected={runtime_info.get('selected_runtime')} | effective={runtime_label}"
        + "\n\nUSER INPUT RECEIVED DURING THIS RUN:\n"
        + base_runner._inputs_text(run)
        + "\n\nAVAILABLE ACTIONS:\n"
        + ", ".join(available)
        + f"\n\nSTEP BUDGET REMAINING: {remaining}"
        + "\n\nWORKSPACE FILES:\n"
        + _workspace_catalog(run)
        + "\n\nPROJECT / DEBUG STATE:\n"
        + project_context
        + "\n\nCURRENT WORKSPACE CONTENT (LOCAL, BOUNDED):\n"
        + _workspace_debug_context(run)
        + "\n\nSANDBOX EXECUTION HISTORY:\n"
        + _execution_catalog(run)
        + "\n\nENVIRONMENT:\n"
        + _environment_context(run)
        + "\n\nSOURCE CATALOG:\n"
        + (base_runner._source_catalog(run) or "No sources recorded.")
        + "\n\nRUN LEDGER:\n"
        + (base_runner._step_ledger(run) or "No steps have run yet.")
        + "\n\nJSON KEYS:\naction, reason, query, source_key, filename, content, script, question"
    )

    last_error = None
    for attempt in range(2):
        retry_note = ""
        if attempt:
            retry_note = (
                "\n\nPrevious action was invalid: "
                + str(last_error or "missing required action data")
                + "\nRe-plan from current files and actual execution evidence. "
                "Do not repeat the invalid action. Return strict JSON only."
            )

        model_override = (
            _node_controller_model_override(run)
            if runtime == RUNTIME_NODE
            else None
        )

        raw, model = base_runner._run_model(
            run,
            system_prompt,
            user_prompt + retry_note,
            response_format="json",
            model_override=model_override,
        )

        try:
            data = base_runner._safe_json_object(
                raw,
                "sandbox agent controller",
            )
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
            requested = str(data.get("filename") or "").strip()
            target = requested or _latest_python_target(run)
            if (
                not target
                or target not in workspace_names
                or not target.lower().endswith(".py")
            ):
                last_error = base_runner.AgentExecutionError(
                    "run_python requires the filename of an existing .py workspace file."
                )
                continue
            data["filename"] = target

        elif action == "run_node":
            requested = str(data.get("filename") or "").strip()
            target = requested or _latest_node_target(run)
            if (
                not target
                or target not in workspace_names
                or not target.lower().endswith((".js", ".mjs", ".cjs"))
            ):
                last_error = base_runner.AgentExecutionError(
                    "run_node requires an existing .js, .mjs, or .cjs workspace file."
                )
                continue
            data["filename"] = target

        elif action == "run_npm":
            script = str(data.get("script") or "").strip()
            try:
                scripts = list_npm_scripts(
                    run["user_id"],
                    run["id"],
                )
            except AgentSandboxError as error:
                last_error = base_runner.AgentExecutionError(str(error))
                continue

            if not script:
                script = _preferred_node_script(run) or ""
            if not script or script not in scripts:
                last_error = base_runner.AgentExecutionError(
                    "run_npm requires the name of an existing package.json script. "
                    + (f"Available: {', '.join(scripts)}" if scripts else "No scripts are defined.")
                )
                continue
            data["script"] = script

        elif action == "workspace_read":
            filename = str(data.get("filename") or "").strip()
            if not filename or filename not in workspace_names:
                last_error = base_runner.AgentExecutionError(
                    "workspace_read requires the filename of an existing workspace file."
                )
                continue
            data["filename"] = filename

        elif action == "write_file":
            filename = str(data.get("filename") or "").strip()
            if not filename:
                last_error = base_runner.AgentExecutionError(
                    "write_file requires a workspace filename."
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
            and not _has_successful_execution(run)
        ):
            validation = _default_validation_action(run)
            if validation and validation.get("action") in available:
                data.update(validation)
                data["reason"] = (
                    f"Validate the runnable {runtime_label} workspace before claiming completion."
                )

        return data

    return {
        "action": "final",
        "reason": (
            "The controller could not produce a valid next workspace/runtime action "
            "after re-planning. Finalize the useful work completed so far and state "
            "any incomplete deliverable clearly."
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
        "It is stored locally and is executed only when the Agent explicitly selects "
        "an allowed sandbox runtime action."
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
            "Sandboxed Python execution is unavailable. " + str(error)
        ) from error
    return format_execution_observation(execution)[: base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_run_node(run, step, filename):
    try:
        execution = run_node_sandbox(
            run["user_id"],
            run["id"],
            filename,
            step_id=step["id"],
            cancel_check=lambda: base_runner._control_probe(run, force=True),
        )
    except AgentSandboxUnavailable as error:
        raise base_runner.AgentToolUnavailable(
            "Sandboxed Node.js execution is unavailable. " + str(error)
        ) from error
    return format_execution_observation(execution)[: base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_run_npm(run, step, script):
    try:
        execution = run_npm_script_sandbox(
            run["user_id"],
            run["id"],
            script,
            step_id=step["id"],
            cancel_check=lambda: base_runner._control_probe(run, force=True),
        )
    except AgentSandboxUnavailable as error:
        raise base_runner.AgentToolUnavailable(
            "Sandboxed npm execution is unavailable. " + str(error)
        ) from error
    return format_execution_observation(execution)[: base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_environment_plan(run):
    if effective_runtime(run) == RUNTIME_NODE:
        result = add_missing_node_dependency_to_manifest(
            run["user_id"],
            run["id"],
        )
        return (
            f"Updated {result['file']['filename']} for detected npm dependency "
            f"{result['package']} from {result.get('detected_in') or 'workspace source'}.\n"
            "The network-enabled setup build receives only a sanitized dependency manifest; "
            "project source and user npm scripts are not copied into that build context."
        )[:base_runner.AGENT_STEP_OUTPUT_LIMIT]

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
    if effective_runtime(run) == RUNTIME_NODE:
        result = setup_node_project_environment(
            run["user_id"],
            run["id"],
            cancel_check=lambda: base_runner._control_probe(run, force=True),
        )
        return format_node_environment_observation(result)[:base_runner.AGENT_STEP_OUTPUT_LIMIT]

    result = setup_project_environment(
        run["user_id"],
        run["id"],
        cancel_check=lambda: base_runner._control_probe(run, force=True),
    )
    return format_environment_observation(result)[:base_runner.AGENT_STEP_OUTPUT_LIMIT]


def _execute_project_plan(run):
    if effective_runtime(run) != RUNTIME_PYTHON:
        raise AgentSandboxError(
            "The deterministic project-contract planner currently supports Python projects only."
        )
    analysis = analyze_project_state(run)
    plan = create_debug_plan(
        run,
        analysis,
    )
    return format_debug_plan(plan)


def _execute_project_repair(run):
    if effective_runtime(run) != RUNTIME_PYTHON:
        raise AgentSandboxError(
            "The deterministic project repair planner currently supports Python projects only."
        )
    return execute_project_repair(run)

def _sandbox_tool_name(action):
    return {
        "run_python": "agent.sandbox.python",
        "run_node": "agent.sandbox.node",
        "run_npm": "agent.sandbox.npm",
    }.get(action)


def _execute_runtime_action(run, step, action_data):
    action = str(action_data.get("action") or "")

    if action == "run_python":
        return _execute_run_python(
            run,
            step,
            action_data.get("filename"),
        )

    if action == "run_node":
        return _execute_run_node(
            run,
            step,
            action_data.get("filename"),
        )

    if action == "run_npm":
        return _execute_run_npm(
            run,
            step,
            action_data.get("script"),
        )

    raise AgentSandboxError(
        f"Unsupported sandbox runtime action: {action}"
    )


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
                retest_action = _required_retest_action(run)
                available_actions = _available_actions(run)

                if (
                    retest_action
                    and retest_action.get("action") in available_actions
                ):
                    verification_action = retest_action["action"]
                    verification_input = {
                        **retest_action,
                        "verification_tail": True,
                    }

                    verification_step = begin_agent_step(
                        user_id,
                        run_id,
                        phase="verification",
                        action=verification_action,
                        tool_name=_sandbox_tool_name(verification_action),
                        reason=(
                            "Mandatory final verification of the workspace revision "
                            "created on the last nominal step."
                        ),
                        input_data=verification_input,
                    )

                    verification_output = ""
                    verification_status = "completed"

                    try:
                        verification_output = _execute_runtime_action(
                            run,
                            verification_step,
                            verification_input,
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
                            **verification_input,
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
                "run_node": "agent.sandbox.node",
                "run_npm": "agent.sandbox.npm",
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
                elif action in {"run_python", "run_node", "run_npm"}:
                    output = _execute_runtime_action(
                        run,
                        step,
                        action_data,
                    )
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
