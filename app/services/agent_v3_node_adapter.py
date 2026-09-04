"""ATLAS v3 native Node/JavaScript coding adapter.

This is the first native language adapter for the unified v3 orchestrator.  It
owns project construction, dependency setup, authoritative verification,
evidence extraction, bounded repair, and final goal acceptance for Node work.

Important design rule: BUILD validity and FINAL acceptance are different gates.
The initial project may still contain an implementation defect; it only needs to
be coherent and safe enough to execute.  Final acceptance is evaluated after
real sandbox evidence exists.
"""

import hashlib
import json
import re

from app.services import agent_runner as legacy_runner
from app.services.agent_environment import AgentEnvironmentError, ENV_PROFILE_PROJECT, get_agent_run_environment
from app.services.agent_node_environment import (
    node_environment_status_for_run,
    setup_node_project_environment,
)
from app.services.agent_sandbox import (
    AgentSandboxError,
    format_execution_observation,
    list_agent_sandbox_executions,
    list_npm_scripts,
    list_workspace_files,
    read_workspace_file,
    run_node_sandbox,
    run_npm_script_sandbox,
    write_workspace_file,
)
from app.services.agent_v3_model_gateway import V3ModelError, run_json
from app.services.agent_v3_candidate_pipeline import V3CandidateError, validate_candidate
from app.services.agent_v3_acceptance import (
    KIND_EXECUTION,
    KIND_PLATFORM,
    KIND_USER,
    acceptance_layers_summary,
    criteria_from_spec,
    evaluate_execution_criteria,
    evaluate_platform_criteria,
    filter_model_unmet_ids,
    partition_criteria,
    repairable_acceptance_issues,
)

_ALLOWED_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt")
_TEST_NAME_RE = re.compile(r"\b(?:test|it)\s*\(\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


class V3NodeError(Exception):
    pass


def _workspace_names(run):
    return [str(item.get("filename") or "") for item in list_workspace_files(run["user_id"], run["id"])]


def _workspace_sources(run, *, budget=22000, per_file=7000):
    names = _workspace_names(run)
    priority = []
    for preferred in ("test.js", "tests.js", "package.json", "index.js", "main.js"):
        if preferred in names and preferred not in priority:
            priority.append(preferred)
    for name in names:
        if name not in priority:
            priority.append(name)

    used = 0
    blocks = []
    for name in priority:
        if used >= budget:
            break
        if not name.lower().endswith(_ALLOWED_SUFFIXES):
            continue
        try:
            content = read_workspace_file(run["user_id"], run["id"], name, max_chars=per_file)
        except Exception:
            continue
        remaining = budget - used
        block = f"--- {name} ---\n" + str(content or "")[:remaining]
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _normalize_filename(value):
    name = str(value or "").strip().replace("\\", "/").split("/")[-1]
    if not name or name.startswith(".") or ".." in name:
        return ""
    if not name.lower().endswith(_ALLOWED_SUFFIXES):
        return ""
    return name[:180]


def _matching_brace(source, open_index):
    """Best-effort JavaScript callback-body matcher for test-contract analysis."""
    depth = 0
    quote = None
    escaped = False
    index = int(open_index)
    while index < len(source):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _test_names_from_source(source):
    """Return semantic/leaf test names, excluding test() calls used as suites.

    Node's test API permits nested subtests.  A repair may legitimately replace
    a describe()/it() suite with test()/subtests, so counting the parent test as
    a new behavioral requirement would make the integrity guard preserve a
    harness wrapper rather than the actual test cases.
    """
    text = str(source or "")
    result = []
    for match in _TEST_NAME_RE.finditer(text):
        is_container = False
        # Callback braces normally occur immediately after the title/argument
        # list. Limit the search window so unrelated later blocks are ignored.
        brace = text.find("{", match.end(), min(len(text), match.end() + 220))
        if brace >= 0:
            close = _matching_brace(text, brace)
            if close is not None:
                body = text[brace + 1:close]
                if _TEST_NAME_RE.search(body):
                    is_container = True
        if not is_container:
            result.append(match.group(1).strip())
    return result


def _test_names(run):
    names = []
    for filename in _workspace_names(run):
        lower = filename.lower()
        if not (lower.startswith("test") or ".test." in lower or ".spec." in lower):
            continue
        try:
            source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=50000)
        except Exception:
            continue
        names.extend(_test_names_from_source(source))
    return names


def _package_json_from_files(files):
    for item in files:
        if item["filename"].lower() == "package.json":
            try:
                data = json.loads(item["content"])
            except Exception as error:
                raise V3NodeError(f"Generated package.json is invalid JSON: {error}") from error
            if not isinstance(data, dict):
                raise V3NodeError("Generated package.json must be a JSON object.")
            return data, item
    return None, None




def _validate_package_requirements(package, spec):
    dependencies = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(package.get(section), dict):
            dependencies.update({str(name).lower(): value for name, value in package[section].items()})
    missing_dependencies = [
        str(name) for name in spec.get("required_dependencies") or []
        if str(name).lower() not in dependencies
    ]
    if missing_dependencies:
        raise V3NodeError(
            "package.json change would remove required dependency/dependencies: "
            + ", ".join(missing_dependencies)
        )
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    missing_scripts = [
        str(name) for name in spec.get("required_scripts") or []
        if str(name) not in scripts
    ]
    if missing_scripts:
        raise V3NodeError(
            "package.json change would remove required script(s): "
            + ", ".join(missing_scripts)
        )

def _augment_package_json(files, spec):
    package, item = _package_json_from_files(files)
    if package is None:
        if "package.json" not in [str(name) for name in spec.get("required_files") or []]:
            return files
        package = {"name": "atlas-project", "version": "1.0.0", "private": True}
        item = {"filename": "package.json", "content": "", "reason": "Required project manifest."}
        files.append(item)

    dependencies = package.get("dependencies") if isinstance(package.get("dependencies"), dict) else {}
    for dependency in spec.get("required_dependencies") or []:
        dependencies.setdefault(str(dependency), "*")
    if dependencies:
        package["dependencies"] = dependencies

    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    for script in spec.get("required_scripts") or []:
        if script == "test" and "test" not in scripts:
            test_file = next(
                (
                    entry["filename"]
                    for entry in files
                    if entry["filename"].lower().startswith("test") and entry["filename"].lower().endswith((".js", ".mjs", ".cjs"))
                ),
                "test.js",
            )
            scripts["test"] = f"node --test {test_file}"
    if scripts:
        package["scripts"] = scripts

    # If generated JS uses import/export syntax, make ESM explicit.
    if any(
        re.search(r"(?m)^\s*(?:import|export)\b", entry["content"])
        for entry in files
        if entry["filename"].lower().endswith((".js", ".mjs", ".cjs"))
    ):
        package.setdefault("type", "module")

    item["content"] = json.dumps(package, ensure_ascii=False, indent=2) + "\n"
    return files



def _is_test_filename(filename):
    lower = str(filename or "").lower()
    return lower.startswith("test") or ".test." in lower or ".spec." in lower


def _coerce_generated_content(filename, content):
    """Normalize model-produced complete-file content at the adapter boundary.

    JSON response formats make it natural for a model to return package.json
    content as a JSON object instead of a JSON-encoded string.  That is still a
    complete file representation, so canonicalize it here rather than burning
    another Agent repair cycle.  Source/text files remain strict strings.
    """
    if isinstance(content, str):
        return content

    if str(filename or "").lower().endswith(".json") and isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2) + "\n"

    raise V3NodeError(
        f"Generated content for {filename} must be a complete string"
        + (" or JSON object/array for .json files." if str(filename or "").lower().endswith(".json") else ".")
    )


def _test_harness_diagnostics(run, execution=None):
    """Return deterministic diagnostics for verifier/test-harness mechanics.

    These diagnostics do not change tests themselves.  They authorize a repair
    model to fix test *mechanics* while a separate integrity guard preserves
    the existing test names/count.
    """
    diagnostics = []
    evidence = "\n".join([
        str((execution or {}).get("stdout") or ""),
        str((execution or {}).get("stderr") or ""),
    ]).lower()

    for filename in _workspace_names(run):
        if not _is_test_filename(filename):
            continue
        try:
            source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=60000)
        except Exception:
            continue

        uses_node_test_api = bool(re.search(r"\b(?:describe|it|test)\s*\(", source))
        imports_node_test = bool(
            re.search(r"require\s*\(\s*['\"]node:test['\"]\s*\)", source)
            or re.search(r"from\s+['\"]node:test['\"]", source)
        )
        if uses_node_test_api and not imports_node_test:
            if any(
                token in evidence
                for token in (
                    "referenceerror: describe is not defined",
                    "referenceerror: it is not defined",
                    "referenceerror: test is not defined",
                )
            ):
                diagnostics.append(
                    f"{filename}: Node built-in test API is used but node:test is not imported."
                )

        if re.search(
            r"\b(?:const|let|var)\s*\{\s*assert\s*\}\s*=\s*require\s*\(\s*['\"](?:node:)?assert(?:/strict)?['\"]\s*\)",
            source,
        ):
            diagnostics.append(
                f"{filename}: assert is destructured from the assert module; use a valid assert module binding."
            )

        if (
            "cannot determine intended module format" in evidence
            or "both require() and top-level await" in evidence
        ):
            diagnostics.append(
                f"{filename}: Node rejected the file because CommonJS require()/exports and top-level await were mixed; "
                "keep one module system. For a CommonJS test file, remove top-level await and use async parent callbacks with await t.test(...), "
                "or flatten the existing behavioral cases into independent top-level tests."
            )

        if (
            "cancelledbyparent" in evidence
            or "test did not finish before its parent and was cancelled" in evidence
        ):
            # Generic node:test lifecycle rule: child tests created inside a
            # parent callback must be awaited/returned through the parent test
            # context, or flattened into independent top-level tests.  This is
            # verifier mechanics, not permission to weaken test semantics.
            diagnostics.append(
                f"{filename}: node:test child tests are being cancelled by their parent; "
                "await child tests through an async parent test context (for example await t.test(...)) "
                "or flatten the existing cases to top-level tests while preserving every test case."
            )

    return diagnostics


def _test_contract_snapshot(run):
    snapshot = {}
    for filename in _workspace_names(run):
        if not _is_test_filename(filename):
            continue
        try:
            source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=70000)
        except Exception:
            continue
        snapshot[filename] = {
            "names": _test_names_from_source(source),
            "content": source,
        }
    return snapshot


def _validate_test_change_integrity(run, spec, files):
    changed_tests = {
        item["filename"]: item["content"]
        for item in files
        if _is_test_filename(item["filename"])
    }
    if not changed_tests:
        return

    before = _test_contract_snapshot(run)
    baseline_names = []
    for item in before.values():
        baseline_names.extend(item.get("names") or [])

    candidate_names = []
    all_names = set(before) | set(changed_tests)
    for filename in all_names:
        source = changed_tests.get(filename)
        if source is None:
            source = (before.get(filename) or {}).get("content") or ""
        candidate_names.extend(_test_names_from_source(source))

    # Repair may fix imports/assertion mechanics or add coverage, but must not
    # silently erase the already-established test specification.
    if len(candidate_names) < len(baseline_names):
        raise V3NodeError(
            f"Repair test change would reduce test coverage from {len(baseline_names)} to {len(candidate_names)} tests."
        )

    missing_names = [name for name in baseline_names if name not in candidate_names]
    if missing_names:
        raise V3NodeError(
            "Repair test change would remove existing test case(s): "
            + ", ".join(missing_names[:12])
        )

    minimum = int(spec.get("min_tests") or 0)
    if minimum and len(candidate_names) < minimum:
        raise V3NodeError(
            f"Repair test change still defines only {len(candidate_names)} tests; goal requires at least {minimum}."
        )


def _filter_noop_changes(run, files):
    existing = {name.lower(): name for name in _workspace_names(run)}
    useful = []
    for item in files:
        actual = existing.get(item["filename"].lower())
        if actual:
            try:
                current = read_workspace_file(run["user_id"], run["id"], actual, max_chars=220000)
            except Exception:
                current = None
            if current is not None and str(current).replace("\r\n", "\n") == str(item["content"]).replace("\r\n", "\n"):
                continue
        useful.append(item)
    if not useful:
        raise V3NodeError("Repair candidate contained only no-op file changes.")
    return useful


def _parse_file_set(data, spec, *, require_all_explicit=True, allow_test_changes=True, existing_only=False):
    raw = data.get("files") if isinstance(data, dict) else None
    if raw is None and isinstance(data, dict):
        raw = data.get("changes")
    if not isinstance(raw, list):
        raise V3NodeError("Node engineering model must return a files/changes array.")

    existing = set()
    # existing_only is applied by caller after filename parsing because run isn't available here.
    files = []
    seen = set()
    total = 0
    for entry in raw[:10]:
        if not isinstance(entry, dict):
            continue
        filename = _normalize_filename(entry.get("filename") or entry.get("file"))
        content = entry.get("content")
        if not filename or filename in seen:
            continue
        content = _coerce_generated_content(filename, content)
        lower = filename.lower()
        is_test = _is_test_filename(filename)
        if is_test and not allow_test_changes:
            raise V3NodeError(f"Repair attempted to modify protected test specification: {filename}")
        if len(content.encode("utf-8")) > 200000:
            raise V3NodeError(f"Generated file is too large: {filename}")
        total += len(content)
        if total > 500000:
            raise V3NodeError("Generated project change-set is too large for one v3 transaction.")
        seen.add(filename)
        files.append({
            "filename": filename,
            "content": content,
            "reason": str(entry.get("reason") or "")[:1000],
        })

    if not files:
        raise V3NodeError("Node engineering model produced no usable files.")

    if require_all_explicit:
        generated = {item["filename"].lower() for item in files}
        missing = [
            name for name in spec.get("required_files") or []
            if str(name).lower() not in generated
        ]
        if missing:
            raise V3NodeError("Initial build omitted explicitly required file(s): " + ", ".join(missing))

    if require_all_explicit:
        files = _augment_package_json(files, spec)
        _package_json_from_files(files)  # validate bootstrap manifest after augmentation
    else:
        # Repairs are partial change-sets. Only validate package.json when the
        # repair explicitly chose to change it; never synthesize a replacement
        # manifest merely because package.json is a final required file.
        if any(item["filename"].lower() == "package.json" for item in files):
            package, _ = _package_json_from_files(files)
            _validate_package_requirements(package, spec)
    return files


def _apply_files(run, files):
    changed = []
    for item in files:
        result = write_workspace_file(
            run["user_id"],
            run["id"],
            item["filename"],
            item["content"],
        )
        changed.append({
            "filename": result["filename"],
            "size_bytes": int(result.get("size_bytes") or 0),
            "updated": bool(result.get("updated")),
            "reason": item.get("reason") or "",
        })
    return changed


def bootstrap_project(run, spec):
    system = (
        "You are ATLAS v3 BUILD, a senior Node.js engineer. Construct one coherent initial project from the goal/spec. "
        "This is BUILD, not final acceptance: make the project structurally complete and executable, but a requested deliberate implementation defect may remain so VERIFY can observe it. "
        "Never weaken the user's test requirements. If a fail→repair demonstration is requested, tests must describe the correct behavior and exactly one small defect must be in implementation code, preferably easy to diagnose. "
        "Use the requested npm dependencies rather than replacing them. Return ONLY JSON: {summary, files:[{filename,content,reason}]}. "
        "Each content field is the COMPLETE file. Keep the project small and readable."
    )
    user = (
        "ORIGINAL GOAL:\n" + str(run.get("goal") or "")
        + "\n\nPROJECT SPEC:\n" + json.dumps(spec, ensure_ascii=False, indent=2)
        + "\n\nFRESH WORKSPACE: no prior project files exist."
    )

    last_error = None
    for attempt, tier in enumerate(("worker", "reasoning"), start=1):
        try:
            data, model = run_json(
                run,
                phase="build",
                purpose=f"v3_node_bootstrap_attempt_{attempt}",
                system_prompt=system,
                user_prompt=user + (f"\n\nPREVIOUS CANDIDATE REJECTION:\n{last_error}" if last_error else ""),
                tier=tier,
            )
            files = _parse_file_set(data, spec, require_all_explicit=True, allow_test_changes=True)
            # A minimum test count is structural enough to repair internally on bootstrap,
            # but behavior-name semantics are intentionally NOT a build gate.
            test_count = 0
            for item in files:
                lower = item["filename"].lower()
                if lower.startswith("test") or ".test." in lower or ".spec." in lower:
                    test_count += len(_test_names_from_source(item["content"]))
            minimum = int(spec.get("min_tests") or 0)
            if minimum and test_count < minimum:
                raise V3NodeError(
                    f"Initial build defines {test_count} tests but the explicit goal requires at least {minimum}."
                )
            preflight = validate_candidate(
                run,
                files,
                baseline_execution=None,
                purpose="build",
            )
            return {
                "model": model,
                "summary": str(data.get("summary") or "Initial Node project constructed."),
                "files": files,
                "preflight": preflight,
                "changed": _apply_files(run, files),
            }
        except (V3ModelError, V3NodeError, V3CandidateError) as error:
            last_error = str(error)
            continue
    raise V3NodeError(
        "Fresh Node project build could not produce a coherent executable project inside its worker/reasoning retry budget: "
        + str(last_error or "unknown bootstrap error")
    )


def ensure_environment(run):
    profile = str((get_agent_run_environment(run["user_id"], run["id"]) or {}).get("profile") or "strict")
    status = node_environment_status_for_run(run["user_id"], run["id"])
    if profile != ENV_PROFILE_PROJECT:
        if not status.get("ready"):
            raise V3NodeError(status.get("message") or "Node base runtime is not ready.")
        return {"setup": False, "status": status}

    if status.get("ready"):
        return {"setup": False, "status": status}
    if status.get("failed_current"):
        raise V3NodeError(status.get("last_error") or status.get("message") or "Current Node dependency environment failed.")

    built = setup_node_project_environment(
        run["user_id"],
        run["id"],
        cancel_check=lambda: legacy_runner._control_probe(run),
    )
    return {"setup": True, "build": built, "status": node_environment_status_for_run(run["user_id"], run["id"])}


def _verification_target(run):
    try:
        scripts = list_npm_scripts(run["user_id"], run["id"])
    except Exception:
        scripts = []
    for script in ("test", "check", "lint", "typecheck", "build"):
        if script in scripts:
            return {"kind": "npm", "script": script}

    names = _workspace_names(run)
    for preferred in ("test.js", "tests.js", "index.test.js", "app.test.js", "index.js", "main.js"):
        if preferred in names:
            return {"kind": "node", "filename": preferred}
    for name in names:
        if name.lower().endswith((".js", ".mjs", ".cjs")):
            return {"kind": "node", "filename": name}
    raise V3NodeError("No executable Node verification target exists in the workspace.")


def verify_project(run, step_id=None):
    target = _verification_target(run)
    if target["kind"] == "npm":
        execution = run_npm_script_sandbox(
            run["user_id"],
            run["id"],
            target["script"],
            step_id=step_id,
            cancel_check=lambda: legacy_runner._control_probe(run),
        )
    else:
        execution = run_node_sandbox(
            run["user_id"],
            run["id"],
            target["filename"],
            step_id=step_id,
            cancel_check=lambda: legacy_runner._control_probe(run),
        )
    return execution


def execution_passed(execution):
    return bool(
        execution
        and str(execution.get("status") or "") == "success"
        and int(execution.get("exit_code") or 0) == 0
    )


def failure_fingerprint(execution):
    if not execution or execution_passed(execution):
        return None
    text = "\n".join([str(execution.get("stdout") or ""), str(execution.get("stderr") or "")])
    text = text.replace("/runtime/", "").replace("/workspace/", "")
    text = re.sub(r"\bduration_ms:\s*[0-9.]+", "duration_ms:#", text, flags=re.I)
    text = re.sub(r":\d+:\d+\b", ":#:#", text)
    meaningful = []
    for line in text.splitlines():
        low = line.lower()
        if any(token in low for token in ("not ok", "error", "expected", "actual", "assert", "typeerror", "referenceerror", "syntaxerror")):
            meaningful.append(line.strip())
    payload = "\n".join(meaningful[-30:]) or text[-3500:]
    raw = "|".join([
        str(execution.get("execution_action") or ""),
        str(execution.get("command") or ""),
        payload,
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:18]


def _repair_allowed_test_files(run, spec, execution=None, acceptance_issues=None):
    # Test *semantics* are protected after BUILD. Test files may still be
    # repaired when deterministic evidence shows the verifier/harness itself
    # is broken, or when final acceptance proves coverage is incomplete.
    if _test_harness_diagnostics(run, execution):
        return True
    if len(_test_names(run)) < int(spec.get("min_tests") or 0):
        return True
    for issue in acceptance_issues or []:
        if str(issue.get("type") or "") in {"behavior_unmet", "insufficient_tests"}:
            return True
    return False


def repair_project(run, spec, execution, cycle, acceptance_issues=None, repair_history=None):
    harness_diagnostics = _test_harness_diagnostics(run, execution)
    allow_tests = _repair_allowed_test_files(
        run,
        spec,
        execution=execution,
        acceptance_issues=acceptance_issues,
    )
    evidence = format_execution_observation(execution) if execution else "No sandbox execution yet."
    acceptance_text = json.dumps(acceptance_issues or [], ensure_ascii=False, indent=2)
    harness_text = "\n".join("- " + item for item in harness_diagnostics) or "none"
    history_lines = []
    for item in list(repair_history or [])[-5:]:
        history_lines.append(
            "- "
            + str(item.get("progress_class") or item.get("classification") or "unknown")
            + ": "
            + str(item.get("hypothesis") or item.get("detail") or "")[:700]
        )
    history_text = "\n".join(history_lines) or "none"
    system = (
        "You are ATLAS v3 REPAIR, a senior Node.js engineer. Repair the current project as one coherent bounded change-set using real sandbox evidence. "
        "Do not rewrite the whole project without cause. Preserve working APIs, requested dependencies, and valid test semantics. "
        + (
            "You MAY repair test harness/import/assertion mechanics or add missing coverage, but preserve every existing test case and its intended behavior. "
            if allow_tests
            else
            "Test files are protected specifications in this repair cycle; do not modify them. "
        )
        + "Do not repeat an already-attempted hypothesis against materially unchanged evidence; use the repair history to choose a new explanation when needed. "
        + "Do not change package.json unless sandbox evidence or an acceptance issue actually requires a manifest/script/dependency change. "
        + "Return ONLY JSON: {diagnosis,hypothesis,changes:[{filename,content,reason}]}. Each content is the COMPLETE file. "
        + "For a .json file, content may be either the complete JSON string or a JSON object/array; ATLAS canonicalizes it."
    )
    user = (
        "ORIGINAL GOAL:\n" + str(run.get("goal") or "")
        + "\n\nPROJECT SPEC:\n" + json.dumps(spec, ensure_ascii=False, indent=2)
        + "\n\nLATEST AUTHORITATIVE SANDBOX EVIDENCE:\n" + evidence[-7000:]
        + "\n\nDETERMINISTIC TEST-HARNESS DIAGNOSTICS:\n" + harness_text
        + "\n\nRECENT REPAIR HISTORY / OUTCOMES:\n" + history_text
        + "\n\nFINAL ACCEPTANCE ISSUES (if verification already passed):\n" + acceptance_text[:4000]
        + "\n\nCURRENT WORKSPACE:\n" + _workspace_sources(run, budget=19000, per_file=6500)
    )

    tier = "worker" if int(cycle) <= 1 else "reasoning"
    last_error = None
    # Candidate-format/validation failures are internal retries. They do not
    # consume a committed engineering repair slot in the v3 governor.
    for attempt in range(3):
        try:
            data, model = run_json(
                run,
                phase="repair",
                purpose=f"v3_node_repair_cycle_{cycle}_attempt_{attempt + 1}",
                system_prompt=system,
                user_prompt=user + (f"\n\nPREVIOUS CANDIDATE REJECTION:\n{last_error}" if last_error else ""),
                tier=("reasoning" if attempt >= 1 and tier == "worker" else tier),
            )
            files = _parse_file_set(
                {"changes": data.get("changes") or data.get("files") or []},
                spec,
                require_all_explicit=False,
                allow_test_changes=allow_tests,
            )
            existing_names = _workspace_names(run)
            existing_lower = {name.lower() for name in existing_names}
            required_lower = {str(name).lower() for name in spec.get("required_files") or []}
            for item in files:
                if item["filename"].lower() not in existing_lower and item["filename"].lower() not in required_lower:
                    raise V3NodeError("Repair attempted to invent an unrelated new file: " + item["filename"])

            _validate_test_change_integrity(run, spec, files)
            files = _filter_noop_changes(run, files)
            preflight = validate_candidate(
                run,
                files,
                baseline_execution=execution,
                purpose="repair",
            )

            return {
                "model": model,
                "diagnosis": str(data.get("diagnosis") or "")[:3000],
                "hypothesis": str(data.get("hypothesis") or data.get("diagnosis") or "")[:2000],
                "harness_diagnostics": harness_diagnostics,
                "preflight": preflight,
                "changed": _apply_files(run, files),
            }
        except (V3ModelError, V3NodeError, V3CandidateError) as error:
            last_error = str(error)
    raise V3NodeError("Repair could not produce a validated change-set: " + str(last_error or "unknown error"))


def inject_intentional_defect(run, spec):
    implementation = [
        name for name in _workspace_names(run)
        if name.lower().endswith((".js", ".mjs", ".cjs"))
        and not name.lower().startswith("test")
        and ".test." not in name.lower()
        and ".spec." not in name.lower()
    ]
    if not implementation:
        raise V3NodeError("Goal requested a fail→repair demonstration but no implementation file can safely receive the deliberate defect.")
    system = (
        "You are ATLAS v3 controlled verification setup. The user explicitly requested one deliberate small implementation defect so the real tests fail before repair. "
        "Modify exactly ONE existing implementation file. Do not change tests, package.json, dependencies, or public architecture. Make the defect simple and localized. "
        "Return ONLY JSON: {changes:[{filename,content,reason}]}."
    )
    user = (
        "GOAL:\n" + str(run.get("goal") or "")
        + "\n\nALLOWED IMPLEMENTATION FILES:\n" + "\n".join("- " + name for name in implementation)
        + "\n\nCURRENT WORKSPACE:\n" + _workspace_sources(run, budget=16000, per_file=6000)
    )
    data, model = run_json(
        run,
        phase="intentional_defect",
        purpose="v3_intentional_defect",
        system_prompt=system,
        user_prompt=user,
        tier="worker",
    )
    files = _parse_file_set({"changes": data.get("changes") or []}, spec, require_all_explicit=False, allow_test_changes=False)
    if len(files) != 1 or files[0]["filename"] not in implementation:
        raise V3NodeError("Intentional-defect stage must modify exactly one existing implementation file.")
    return {"model": model, "changed": _apply_files(run, files)}




def _dependency_used(run, dependency):
    dep = str(dependency or "").strip()
    if not dep:
        return True
    patterns = [
        re.compile(r"\bfrom\s+['\"]" + re.escape(dep) + r"(?:/[^'\"]*)?['\"]"),
        re.compile(r"\bimport\s+['\"]" + re.escape(dep) + r"(?:/[^'\"]*)?['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]" + re.escape(dep) + r"(?:/[^'\"]*)?['\"]\s*\)"),
    ]
    for filename in _workspace_names(run):
        if not filename.lower().endswith((".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")):
            continue
        try:
            source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=80000)
        except Exception:
            continue
        if any(pattern.search(source) for pattern in patterns):
            return True
    return False

def _hard_acceptance(run, spec, verified):
    issues = []
    names = set(_workspace_names(run))
    lower_names = {name.lower() for name in names}
    for required in spec.get("required_files") or []:
        if str(required).lower() not in lower_names:
            issues.append({"type": "missing_file", "item": required})

    package = {}
    if "package.json" in lower_names:
        actual = next(name for name in names if name.lower() == "package.json")
        try:
            package = json.loads(read_workspace_file(run["user_id"], run["id"], actual, max_chars=30000))
        except Exception:
            issues.append({"type": "invalid_package_json", "item": "package.json"})
            package = {}
    deps = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(package.get(section), dict):
            deps.update({str(key).lower(): value for key, value in package[section].items()})
    for dependency in spec.get("required_dependencies") or []:
        if str(dependency).lower() not in deps:
            issues.append({"type": "missing_dependency", "item": dependency})
        elif not _dependency_used(run, dependency):
            issues.append({"type": "dependency_not_used", "item": dependency})

    if spec.get("forbid_external_dependencies") and deps:
        issues.append({
            "type": "forbidden_dependency",
            "item": ", ".join(sorted(deps.keys())[:20]),
        })

    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    for script in spec.get("required_scripts") or []:
        if str(script) not in scripts:
            issues.append({"type": "missing_script", "item": script})

    tests = _test_names(run)
    minimum = int(spec.get("min_tests") or 0)
    if minimum and len(tests) < minimum:
        issues.append({"type": "insufficient_tests", "required": minimum, "actual": len(tests)})

    if spec.get("requires_verification") and not verified:
        issues.append({"type": "verification_required", "item": "sandbox"})

    if spec.get("requires_fail_then_repair"):
        executions = list_agent_sandbox_executions(run["user_id"], run["id"], limit=100)
        had_real_failure = any(
            str(item.get("runtime") or "") == "node"
            and str(item.get("status") or "") in {"failed", "timeout"}
            for item in executions
        )
        if not had_real_failure:
            issues.append({"type": "required_failure_not_observed", "item": "fail_then_repair"})

    return issues, tests


def _acceptance_result(*, hard_issues, semantic_issues, test_names, criteria, execution_issues,
                       execution_checks, platform_issues, platform_evidence, platform_checks,
                       model=None, notes="", unknown_model_ids=None):
    hard_issues = list(hard_issues or [])
    semantic_issues = list(semantic_issues or [])
    execution_issues = list(execution_issues or [])
    platform_issues = list(platform_issues or [])
    criteria = dict(criteria or {})

    user_ok = not hard_issues and not semantic_issues
    execution_ok = not execution_issues
    platform_ok = not platform_issues

    result = {
        "satisfied": bool(user_ok and execution_ok and platform_ok),
        "hard_issues": hard_issues,
        "semantic_issues": semantic_issues,
        "execution_issues": execution_issues,
        "platform_issues": platform_issues,
        "test_names": list(test_names or []),
        "criteria": criteria,
        "layers": {
            KIND_USER: {
                "satisfied": user_ok,
                "criteria_count": len(criteria.get(KIND_USER) or []),
                "issues": hard_issues + semantic_issues,
            },
            KIND_EXECUTION: {
                "satisfied": execution_ok,
                "criteria_count": len(criteria.get(KIND_EXECUTION) or []),
                "issues": execution_issues,
                "checks": list(execution_checks or []),
            },
            KIND_PLATFORM: {
                "satisfied": platform_ok,
                "criteria_count": len(criteria.get(KIND_PLATFORM) or []),
                "issues": platform_issues,
                "checks": list(platform_checks or []),
                "evidence": dict(platform_evidence or {}),
            },
        },
        "platform_evidence": dict(platform_evidence or {}),
        "model": model,
        "notes": str(notes or "")[:2000],
        "ignored_unknown_model_ids": list(unknown_model_ids or [])[:20],
    }
    result["repairable_issues"] = repairable_acceptance_issues(result)
    result["layer_summary"] = acceptance_layers_summary(result)
    return result


def evaluate_acceptance(run, spec, execution):
    """Evaluate final acceptance using three evidence domains.

    Project source owns user-deliverable behavior. Authoritative sandbox output
    owns execution facts. ATLAS runtime policy owns platform guarantees. A model
    may judge only project-owned semantic criteria, and its returned IDs are
    strictly whitelisted against the persistent project spec.
    """
    verified = execution_passed(execution)
    hard_issues, test_names = _hard_acceptance(run, spec, verified)

    criteria = partition_criteria(criteria_from_spec(spec))
    execution_issues, execution_checks = evaluate_execution_criteria(
        criteria.get(KIND_EXECUTION) or [],
        execution,
    )
    platform_issues, platform_evidence, platform_checks = evaluate_platform_criteria(
        criteria.get(KIND_PLATFORM) or [],
        run,
        execution,
    )

    user_behaviors = criteria.get(KIND_USER) or []

    # Hard project requirements are deterministic and should be repaired before
    # spending a semantic model call. Platform/execution blockers are never
    # handed to project repair as though source code could implement them.
    if hard_issues or execution_issues or platform_issues:
        return _acceptance_result(
            hard_issues=hard_issues,
            semantic_issues=[],
            test_names=test_names,
            criteria=criteria,
            execution_issues=execution_issues,
            execution_checks=execution_checks,
            platform_issues=platform_issues,
            platform_evidence=platform_evidence,
            platform_checks=platform_checks,
        )

    if not user_behaviors:
        return _acceptance_result(
            hard_issues=[],
            semantic_issues=[],
            test_names=test_names,
            criteria=criteria,
            execution_issues=[],
            execution_checks=execution_checks,
            platform_issues=[],
            platform_evidence=platform_evidence,
            platform_checks=platform_checks,
            notes="No project-owned semantic behavior required a model acceptance judgment.",
        )

    # Semantic acceptance is deliberately compact and happens only AFTER real
    # execution passes. The model can judge only IDs that already exist in the
    # stored user-deliverable criteria. Unknown/invented IDs are diagnostic data,
    # never new requirements.
    system = (
        "You are the ATLAS v3 USER-DELIVERABLE acceptance evaluator. Authoritative execution already passed. "
        "Evaluate ONLY the supplied project-owned behavior criteria against test names and current source. "
        "Do not evaluate sandbox isolation, Docker/network policy, permissions, read-only mounts, or test-execution requirements; ATLAS evaluates those separately. "
        "You MUST NOT invent criteria or IDs. Return ONLY JSON: "
        "{satisfied:boolean, unmet_behavior_ids:[string], notes:string}. Every unmet_behavior_id must be copied exactly from ALLOWED BEHAVIOR IDS."
    )
    allowed_ids = [str(item.get("id") or "") for item in user_behaviors if str(item.get("id") or "").strip()]
    user = (
        "ORIGINAL GOAL:\n" + str(run.get("goal") or "")
        + "\n\nPROJECT-OWNED BEHAVIOR CRITERIA:\n" + json.dumps(user_behaviors, ensure_ascii=False, indent=2)
        + "\n\nALLOWED BEHAVIOR IDS:\n" + json.dumps(allowed_ids, ensure_ascii=False)
        + "\n\nTEST NAMES:\n" + json.dumps(test_names, ensure_ascii=False)
        + "\n\nCURRENT SOURCE SNAPSHOT:\n" + _workspace_sources(run, budget=13000, per_file=4500)
    )
    try:
        data, model = run_json(
            run,
            phase="acceptance",
            purpose="v3_node_user_deliverable_acceptance",
            system_prompt=system,
            user_prompt=user,
            tier="worker",
            prompt_budget_chars=19000,
        )
        raw_unmet = data.get("unmet_behavior_ids") or []
        if not isinstance(raw_unmet, list):
            raw_unmet = []

        known_unmet, unknown_ids = filter_model_unmet_ids(raw_unmet, allowed_ids)

        semantic_issues = [
            {"type": "behavior_unmet", "item": item}
            for item in known_unmet
        ]

        notes = str(data.get("notes") or "")[:2000]
        if unknown_ids:
            suffix = (
                " Acceptance model returned unknown criterion IDs that ATLAS ignored because they are not part of the persistent user-deliverable contract: "
                + ", ".join(unknown_ids[:8])
            )
            notes = (notes + suffix).strip()[:2000]

        # Known unmet IDs are authoritative. A bare model boolean cannot create
        # an unnamed blocker because that would recreate the requirement-drift
        # failure v3.2 exposed with `sandbox_isolation`.
        return _acceptance_result(
            hard_issues=[],
            semantic_issues=semantic_issues,
            test_names=test_names,
            criteria=criteria,
            execution_issues=[],
            execution_checks=execution_checks,
            platform_issues=[],
            platform_evidence=platform_evidence,
            platform_checks=platform_checks,
            model=model,
            notes=notes,
            unknown_model_ids=unknown_ids,
        )
    except V3ModelError as error:
        return _acceptance_result(
            hard_issues=[],
            semantic_issues=[{"type": "semantic_acceptance_unavailable", "item": str(error)}],
            test_names=test_names,
            criteria=criteria,
            execution_issues=[],
            execution_checks=execution_checks,
            platform_issues=[],
            platform_evidence=platform_evidence,
            platform_checks=platform_checks,
            notes="Semantic user-deliverable acceptance model was unavailable.",
        )


def acceptance_summary(acceptance):
    layers = acceptance.get("layer_summary") or acceptance_layers_summary(acceptance)
    if acceptance.get("satisfied"):
        platform_evidence = acceptance.get("platform_evidence") or {}
        lines = [
            "Goal acceptance: SATISFIED.",
            f"Tests detected: {len(acceptance.get('test_names') or [])}.",
            "Acceptance layers: user_deliverable=pass, execution=pass, platform=pass.",
        ]
        if platform_evidence.get("execution_observed"):
            lines.append(
                "Platform evidence: Docker sandbox execution observed; execution network disabled; durable source read-only; disposable runtime enabled."
            )
        unknown = acceptance.get("ignored_unknown_model_ids") or []
        if unknown:
            lines.append("Ignored model-invented acceptance IDs: " + ", ".join(str(item) for item in unknown[:8]))
        return "\n".join(lines)

    issues = (
        list(acceptance.get("hard_issues") or [])
        + list(acceptance.get("semantic_issues") or [])
        + list(acceptance.get("execution_issues") or [])
        + list(acceptance.get("platform_issues") or [])
    )
    lines = [
        "Goal acceptance: INCOMPLETE.",
        (
            "Acceptance layers: "
            f"user_deliverable={'pass' if layers.get('user_deliverable') else 'incomplete'}, "
            f"execution={'pass' if layers.get('execution') else 'incomplete'}, "
            f"platform={'pass' if layers.get('platform') else 'incomplete'}."
        ),
        f"Outstanding items: {len(issues)}",
    ]
    for issue in issues[:12]:
        lines.append("- " + str(issue))
    return "\n".join(lines)

