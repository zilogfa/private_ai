"""ATLAS v3 native Node/JavaScript coding adapter.

This is the first native language adapter for the unified v3 orchestrator.  It
owns project construction, dependency setup, authoritative verification,
evidence extraction, bounded repair, and final goal acceptance for Node work.

Important design rule: BUILD validity and FINAL acceptance are different gates.
The initial build may still contain ordinary accidental defects, but the user-requested
demonstration defect is lifecycle-owned and is never inserted during BUILD.  Final
acceptance is evaluated only after real sandbox evidence exists.
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
from app.services.agent_v3_action_protocol import (
    BUILD_ACTION_SCHEMA,
    DEFECT_ACTION_SCHEMA,
    REPAIR_ACTION_SCHEMA,
)
from app.services.agent_v3_candidate_pipeline import V3CandidateError, validate_candidate
from app.services.agent_v3_mutation_testing import (
    V3MutationError,
    is_legitimate_failing_execution,
    select_failing_node_mutant,
)
from app.services.agent_v3_node_evidence import (
    fact_values,
    has_fact,
    implicated_lines,
    normalize_node_execution,
)
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
    """Return normalized verifier/test-harness mechanics diagnostics.

    Policy consumes structured Node failure facts instead of depending on one
    exact stderr/TAP rendering.  The same undefined identifier may appear as
    ``ReferenceError: assert is not defined`` or TAP ``error: 'assert is not
    defined'``; both must grant the same bounded mechanics authority.
    """
    diagnostics = []
    normalized = normalize_node_execution(execution)

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
        missing_test_bindings = {
            str(value).lower()
            for value in fact_values(normalized, "undefined_identifier", "identifier")
            if str(value).lower() in {"describe", "it", "test"}
        }
        missing_test_bindings.update(
            str(value).lower()
            for value in fact_values(normalized, "missing_node_test_binding", "identifier")
        )
        if uses_node_test_api and not imports_node_test and missing_test_bindings:
            diagnostics.append(
                f"{filename}: Node built-in test API binding is missing ({', '.join(sorted(missing_test_bindings))}); import node:test without changing behavioral cases."
            )

        if re.search(
            r"\b(?:const|let|var)\s*\{\s*assert\s*\}\s*=\s*require\s*\(\s*['\"](?:node:)?assert(?:/strict)?['\"]\s*\)",
            source,
        ):
            diagnostics.append(
                f"{filename}: assert is destructured from the assert module; use a valid assert module binding."
            )

        if (
            has_fact(normalized, "undefined_identifier", identifier="assert")
            and re.search(r"\bassert\s*\.", source)
            and not _has_assert_binding(source)
        ):
            diagnostics.append(
                f"{filename}: authoritative Node evidence proves assert is used without a valid built-in assert binding."
            )

        if (
            has_fact(normalized, "sync_callback_used_with_assert_rejects")
            and re.search(r"\bassert\.rejects\s*\(", source)
        ):
            diagnostics.append(
                f"{filename}: Node assert.rejects received a callback that threw synchronously in the real run; "
                "preserve the intended error assertion but use assert.throws for the implicated synchronous call(s)."
            )

        if has_fact(normalized, "module_format_conflict"):
            diagnostics.append(
                f"{filename}: Node rejected mixed module mechanics; keep one coherent CommonJS/ESM style without changing test semantics."
            )

        if has_fact(normalized, "cancelled_child_tests"):
            diagnostics.append(
                f"{filename}: node:test child tests are being cancelled by their parent; await child tests or flatten the same behavioral cases without removing assertions."
            )

    return diagnostics


def _test_assertion_count(source):
    text = str(source or "")
    # Count assertion invocations rather than imports/bindings.  This is a
    # conservative semantic floor: test-harness repair may change assertion
    # mechanics (rejects -> throws) but must not silently reduce checks.
    return len(re.findall(r"\bassert(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+\s*\(", text))

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
    baseline_assertions = 0
    for item in before.values():
        baseline_names.extend(item.get("names") or [])
        baseline_assertions += _test_assertion_count(item.get("content") or "")

    candidate_names = []
    candidate_assertions = 0
    all_names = set(before) | set(changed_tests)
    for filename in all_names:
        source = changed_tests.get(filename)
        if source is None:
            source = (before.get(filename) or {}).get("content") or ""
        candidate_names.extend(_test_names_from_source(source))
        candidate_assertions += _test_assertion_count(source)

    # Test semantics are protected even when harness mechanics are repairable.
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

    if candidate_assertions < baseline_assertions:
        raise V3NodeError(
            f"Repair test change would reduce assertion coverage from {baseline_assertions} to {candidate_assertions}."
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



def _initial_build_contract_gaps(files, spec):
    """Return user-contract gaps that BUILD may defer to convergence.

    BUILD owns coherent project construction, not final goal acceptance.  A
    structurally valid initial project may therefore be committed even when a
    deterministic user-contract requirement such as minimum test count is not
    complete yet.  VERIFY + the baseline contract gate own that convergence.
    """
    gaps = []
    test_count = 0
    for item in files or []:
        lower = str(item.get("filename") or "").lower()
        if lower.startswith("test") or ".test." in lower or ".spec." in lower:
            test_count += len(_test_names_from_source(item.get("content") or ""))

    minimum = int(spec.get("min_tests") or 0)
    if minimum and test_count < minimum:
        gaps.append({
            "type": "insufficient_tests",
            "required": minimum,
            "actual": test_count,
            "owner": "baseline_contract_gate",
        })
    return gaps


def bootstrap_project(run, spec):
    system = (
        "You are ATLAS v3 BUILD, a senior Node.js engineer. Construct one coherent initial project from the goal/spec. "
        "BUILD must target an intended-correct baseline. Do NOT deliberately insert the user's requested demonstration defect during BUILD; "
        "the orchestrator owns that later lifecycle stage only after a clean baseline verification exists. "
        "The initial project may still contain ordinary mistakes, which VERIFY/REPAIR will diagnose, but do not manufacture them. "
        "Never weaken the user's test requirements. Tests must always describe the correct intended behavior. "
        "Use the requested npm dependencies rather than replacing them. Dependency version strings are implementation suggestions unless the user explicitly pinned a version; "
        "ATLAS validates them against registry metadata during the controlled environment phase. "
        "Return ONLY JSON: {summary, files:[{filename,content,reason}]}. "
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
                schema=BUILD_ACTION_SCHEMA,
                schema_name="node_build_action_v1",
            )
            files = _parse_file_set(data, spec, require_all_explicit=True, allow_test_changes=True)
            # BUILD is a construction gate, not final acceptance. Deterministic
            # user-contract gaps (for example 5 tests when the goal requires 6)
            # are recorded and converged after real execution evidence exists.
            # This prevents a nearly-complete model candidate from being thrown
            # away before ATLAS can use its normal verify/repair machinery.
            contract_gaps = _initial_build_contract_gaps(files, spec)
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
                "contract_gaps": contract_gaps,
                "changed": _apply_files(run, files),
            }
        except (V3ModelError, V3NodeError, V3CandidateError) as error:
            last_error = str(error)
            continue
    raise V3NodeError(
        "Fresh Node project build could not produce a coherent executable project inside its worker/reasoning retry budget: "
        + str(last_error or "unknown bootstrap error")
    )


def ensure_environment(run, spec=None):
    profile = str((get_agent_run_environment(run["user_id"], run["id"]) or {}).get("profile") or "strict")
    status = node_environment_status_for_run(run["user_id"], run["id"])
    if profile != ENV_PROFILE_PROJECT:
        if not status.get("ready"):
            raise V3NodeError(status.get("message") or "Node base runtime is not ready.")
        return {"setup": False, "status": status, "dependency_resolutions": []}

    if status.get("ready"):
        return {"setup": False, "status": status, "dependency_resolutions": []}

    # v3.5: a previously failed dependency image is not automatically terminal.
    # setup_node_project_environment() first validates model/project package
    # versions against registry metadata and can rewrite a hallucinated unpinned
    # version before computing the new environment hash. Explicit user pins are
    # never silently changed.
    built = setup_node_project_environment(
        run["user_id"],
        run["id"],
        cancel_check=lambda: legacy_runner._control_probe(run),
        dependency_constraints=dict((spec or {}).get("dependency_constraints") or {}),
    )
    resolutions = list(built.get("dependency_resolutions") or [])
    if resolutions:
        try:
            from app.services.agent_v3_storage import record_dependency_resolutions
            record_dependency_resolutions(run, resolutions)
        except Exception:
            pass
    return {
        "setup": True,
        "build": built,
        "status": node_environment_status_for_run(run["user_id"], run["id"]),
        "dependency_resolutions": resolutions,
    }


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


def _test_repair_authority(run, spec, execution=None, acceptance_issues=None):
    """Return explicit bounded authority for test-file mutation.

    Tests are user/project specifications.  ATLAS may mutate a test file only
    when normalized runtime evidence proves a harness/mechanics defect, or when
    final acceptance proves that requested coverage itself is missing.
    """
    normalized = normalize_node_execution(execution)
    permissions = []

    diagnostics = _test_harness_diagnostics(run, execution)
    implicated_files = {
        str(item.get("filename") or "").lower()
        for item in (normalized or {}).get("locations") or []
        if str(item.get("filename") or "")
    }
    if diagnostics:
        for filename in _workspace_names(run):
            if not _is_test_filename(filename):
                continue
            try:
                source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=70000)
            except Exception:
                continue
            kinds = []
            location_matches = not implicated_files or filename.lower() in implicated_files
            if location_matches and has_fact(normalized, "undefined_identifier", identifier="assert") and re.search(r"\bassert\s*\.", source) and not _has_assert_binding(source):
                kinds.append("missing_assert_binding")
            if location_matches and has_fact(normalized, "sync_callback_used_with_assert_rejects") and re.search(r"\bassert\.rejects\s*\(", source):
                kinds.append("sync_assertion_mechanics")
            if location_matches and has_fact(normalized, "module_format_conflict"):
                kinds.append("module_format_mechanics")
            if location_matches and has_fact(normalized, "cancelled_child_tests"):
                kinds.append("node_test_lifecycle")
            missing_test_api = {str(v).lower() for v in fact_values(normalized, "undefined_identifier", "identifier")} & {"describe", "it", "test"}
            if missing_test_api:
                kinds.append("missing_node_test_binding")
            if kinds:
                permissions.append({
                    "filename": filename,
                    "scope": "mechanics_only",
                    "kinds": sorted(set(kinds)),
                    "implicated_lines": implicated_lines(normalized, filename),
                })

    # Coverage creation is a separate authority. It is not mechanics-only, but
    # the semantic-preservation gate still prevents deleting established cases.
    # Grant this authority to actual/required test files rather than using a
    # global wildcard.  A coverage gap must never authorize an unrelated
    # implementation rewrite merely because tests are incomplete.
    test_targets = [name for name in _workspace_names(run) if _is_test_filename(name)]
    if not test_targets:
        test_targets = [
            str(name) for name in (spec.get("required_files") or [])
            if _is_test_filename(name)
        ]
    if len(_test_names(run)) < int(spec.get("min_tests") or 0):
        for filename in (test_targets or ["test.js"]):
            permissions.append({"filename": filename, "scope": "coverage_addition", "kinds": ["insufficient_tests"], "implicated_lines": []})
    for issue in acceptance_issues or []:
        if str(issue.get("type") or "") in {"behavior_unmet", "insufficient_tests"}:
            for filename in (test_targets or ["test.js"]):
                permissions.append({"filename": filename, "scope": "coverage_addition", "kinds": [str(issue.get("type"))], "implicated_lines": []})

    # Deduplicate while preserving diagnostic order.
    result = []
    seen = set()
    for item in permissions:
        key = (str(item.get("filename")), str(item.get("scope")), tuple(item.get("kinds") or []))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _repair_allowed_test_files(run, spec, execution=None, acceptance_issues=None):
    return bool(_test_repair_authority(run, spec, execution, acceptance_issues))


def _acceptance_repair_directive(run, spec, acceptance_issues=None):
    """Return a bounded source-authority directive for contract convergence.

    A green process result does not authorize arbitrary source edits when the
    persistent user contract is still incomplete.  In particular, an
    ``insufficient_tests`` blocker is owned by the test contract: implementation
    files may not be rewritten to chase an already-green sandbox result.
    """
    issues = [dict(item) for item in (acceptance_issues or []) if isinstance(item, dict)]
    types = {str(item.get("type") or "") for item in issues if str(item.get("type") or "")}
    if not issues:
        return {"kind": "runtime_repair", "allowed_files": [], "issues": []}

    if types and types <= {"insufficient_tests"}:
        targets = [name for name in _workspace_names(run) if _is_test_filename(name)]
        if not targets:
            targets = [
                str(name) for name in (spec.get("required_files") or [])
                if _is_test_filename(name)
            ]
        return {
            "kind": "test_contract_convergence",
            "allowed_files": targets or ["test.js"],
            "issues": issues,
            "current_test_count": len(_test_names(run)),
            "required_test_count": int(spec.get("min_tests") or 0),
            "instruction": (
                "Close the persistent test-coverage contract only. Modify test files, not implementation files. "
                "Register real Node built-in test cases (node:test test()/it() semantics) and use real assertions. "
                "Top-level scripts or console messages such as 'Test N passed' do not count as registered tests. "
                "It is acceptable for truthful newly-registered tests to expose a latent implementation failure; "
                "that failure belongs to the subsequent baseline repair campaign."
            ),
        }

    manifest_types = {"invalid_package_json", "missing_dependency", "forbidden_dependency", "missing_script"}
    if types and types <= manifest_types:
        return {
            "kind": "manifest_contract_convergence",
            "allowed_files": ["package.json"],
            "issues": issues,
            "instruction": "Repair only package.json contract metadata required by the listed acceptance blockers.",
        }

    return {
        "kind": "general_acceptance_convergence",
        "allowed_files": [],
        "issues": issues,
        "instruction": "Address only the listed original-goal acceptance blockers; avoid unrelated rewrites.",
    }


def _projected_test_count(run, changes):
    """Count semantic Node test registrations after overlaying a candidate."""
    changed = {str(item.get("filename") or ""): str(item.get("content") or "") for item in (changes or [])}
    names = set(_workspace_names(run)) | set(changed)
    total = 0
    for filename in names:
        if not _is_test_filename(filename):
            continue
        source = changed.get(filename)
        if source is None:
            try:
                source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=70000)
            except Exception:
                source = ""
        total += len(_test_names_from_source(source))
    return total


def _validate_acceptance_repair_scope(run, spec, files, directive):
    """Ensure an acceptance repair actually moves the contract it owns."""
    directive = dict(directive or {})
    kind = str(directive.get("kind") or "runtime_repair")
    allowed = {str(name).lower() for name in (directive.get("allowed_files") or []) if str(name)}
    changed = [str(item.get("filename") or "") for item in (files or [])]

    if allowed:
        unrelated = [name for name in changed if name.lower() not in allowed]
        if unrelated:
            raise V3NodeError(
                f"Contract-directed repair scope {kind} does not authorize unrelated file(s): "
                + ", ".join(unrelated)
            )

    if kind == "test_contract_convergence":
        non_tests = [name for name in changed if not _is_test_filename(name)]
        if non_tests:
            raise V3NodeError(
                "Insufficient-test convergence may modify only test files; candidate changed: "
                + ", ".join(non_tests)
            )
        before = int(directive.get("current_test_count") or len(_test_names(run)))
        after = _projected_test_count(run, files)
        required = int(directive.get("required_test_count") or spec.get("min_tests") or 0)
        if after <= before:
            raise V3NodeError(
                f"Test-contract candidate did not increase registered Node test coverage ({before} -> {after}; required {required}). "
                "Use actual node:test test()/it() registrations rather than console-only checks."
            )
        return {
            "kind": kind,
            "improved": True,
            "before_test_count": before,
            "after_test_count": after,
            "required_test_count": required,
            "detail": f"Registered Node test coverage improved {before} -> {after} (required {required}).",
        }

    return {"kind": kind, "improved": False}

def _node_package_type(run):
    try:
        raw = read_workspace_file(run["user_id"], run["id"], "package.json", max_chars=30000)
        package = json.loads(raw)
        return str(package.get("type") or "").strip().lower() if isinstance(package, dict) else ""
    except Exception:
        return ""


def _has_assert_binding(source):
    text = str(source or "")
    return bool(
        re.search(r"\b(?:const|let|var)\s+assert\s*=\s*require\s*\(\s*['\"](?:node:)?assert(?:/strict)?['\"]\s*\)", text)
        or re.search(r"\bimport\s+assert\s+from\s+['\"](?:node:)?assert(?:/strict)?['\"]", text)
    )


def _has_module_binding(source, variable, package):
    text = str(source or "")
    variable = re.escape(str(variable or ""))
    package = re.escape(str(package or ""))
    return bool(
        re.search(rf"\b(?:const|let|var)\s+{variable}\s*=\s*require\s*\(\s*['\"]{package}(?:/[^'\"]*)?['\"]\s*\)", text)
        or re.search(rf"\bimport\s+{variable}\s+from\s+['\"]{package}(?:/[^'\"]*)?['\"]", text)
        or re.search(rf"\bimport\s+\*\s+as\s+{variable}\s+from\s+['\"]{package}(?:/[^'\"]*)?['\"]", text)
    )


def _insert_top_level_binding(source, binding_line):
    text = str(source or "")
    if binding_line in text:
        return text
    lines = text.splitlines()
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    # Keep the import/require header together.  This deliberately does not try
    # to rewrite arbitrary source structure; it only inserts one proven binding.
    while insert_at < len(lines):
        stripped = lines[insert_at].strip()
        if not stripped:
            if insert_at:
                break
            insert_at += 1
            continue
        if (
            stripped.startswith("const ")
            or stripped.startswith("let ")
            or stripped.startswith("var ")
            or stripped.startswith("import ")
        ) and ("require(" in stripped or stripped.startswith("import ")):
            insert_at += 1
            continue
        break
    lines.insert(insert_at, binding_line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _repair_sync_rejects_mechanics(source, implicated):
    """Convert only evidence-implicated sync assert.rejects calls to throws.

    Node's stack normally points at or immediately inside the assert.rejects
    callback.  A small line-radius keeps the deterministic edit scoped to the
    assertions actually observed failing.
    """
    lines = str(source or "").splitlines()
    implicated = {int(value) for value in (implicated or []) if int(value or 0) > 0}
    changed = False
    for index, line in enumerate(lines):
        if not re.search(r"\bawait\s+assert\.rejects\s*\(", line):
            continue
        line_number = index + 1
        if implicated and not any(abs(line_number - value) <= 4 for value in implicated):
            continue
        lines[index] = re.sub(r"\bawait\s+assert\.rejects\s*\(", "assert.throws(", line, count=1)
        changed = True
    if not changed:
        return None
    return "\n".join(lines) + ("\n" if str(source or "").endswith("\n") else "")


def _deterministic_mechanical_repair(run, spec, execution, test_authority=None):
    """Return one evidence-proven mechanical repair or None.

    The lane operates on normalized Node facts and explicit test-repair
    authority.  Renderer-specific strings are never the trust boundary.
    Business logic and ambiguous changes remain model work.
    """
    if not execution:
        return None
    normalized = normalize_node_execution(execution)
    package_type = _node_package_type(run)
    authority = list(test_authority or [])
    authorized_test_files = {
        str(item.get("filename") or "").lower()
        for item in authority
        if str(item.get("scope") or "") == "mechanics_only"
    }

    candidates = []
    diagnosis = "Authoritative runtime evidence proved a mechanical module/test-harness defect."
    hypothesis = "Apply only the evidence-proven mechanics correction and preserve all behavior/test semantics."

    # Missing Node assert binding.  TAP may report this as a ReferenceError or
    # as `error: 'assert is not defined'`; normalized facts make both identical.
    if has_fact(normalized, "undefined_identifier", identifier="assert"):
        for filename in _workspace_names(run):
            if not _is_test_filename(filename) or filename.lower() not in authorized_test_files:
                continue
            try:
                source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=70000)
            except Exception:
                continue
            if not re.search(r"\bassert\s*\.", source) or _has_assert_binding(source):
                continue
            esm = filename.lower().endswith(".mjs") or package_type == "module" or bool(re.search(r"(?m)^\s*import\b", source))
            binding = "import assert from 'node:assert/strict';" if esm else "const assert = require('node:assert/strict');"
            content = _insert_top_level_binding(source, binding)
            candidates.append({
                "filename": filename,
                "content": content,
                "reason": "Normalized authoritative Node evidence proves the test harness uses assert without importing the built-in assert module.",
            })
            diagnosis = "Normalized Node evidence proved the test harness is missing the built-in assert binding."
            hypothesis = "Add only the missing assert binding; preserve every existing test and assertion."
            break

    # Synchronous callback passed to assert.rejects.  The real Node stack proves
    # waitForActual/Function.rejects observed a synchronous throw.  Convert only
    # implicated assertion calls, not arbitrary async behavior.
    if not candidates and has_fact(normalized, "sync_callback_used_with_assert_rejects"):
        for filename in _workspace_names(run):
            if not _is_test_filename(filename) or filename.lower() not in authorized_test_files:
                continue
            try:
                source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=70000)
            except Exception:
                continue
            content = _repair_sync_rejects_mechanics(
                source,
                implicated_lines(normalized, filename),
            )
            if content is None:
                continue
            candidates.append({
                "filename": filename,
                "content": content,
                "reason": "Authoritative Node assert stack proves the implicated callbacks throw synchronously; use assert.throws while preserving the same error expectations.",
            })
            diagnosis = "Normalized Node evidence proved synchronous callbacks are being asserted with assert.rejects."
            hypothesis = "Change only the implicated assertion mechanics from rejects to throws; preserve test names, callbacks, messages and assertion count."
            break

    # Required package referenced as an undefined bare identifier without an
    # import.  Normalize TAP/ReferenceError variants before matching the user's
    # required dependency contract.
    if not candidates:
        undefined = [str(value) for value in fact_values(normalized, "undefined_identifier", "identifier")]
        required = [str(item) for item in spec.get("required_dependencies") or []]
        missing_var = next((value for value in undefined if value in required and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value)), "")
        package = missing_var or None
        if package:
            for filename in _workspace_names(run):
                if not filename.lower().endswith((".js", ".mjs", ".cjs")):
                    continue
                try:
                    source = read_workspace_file(run["user_id"], run["id"], filename, max_chars=70000)
                except Exception:
                    continue
                if not re.search(rf"\b{re.escape(missing_var)}\s*\.", source) or _has_module_binding(source, missing_var, package):
                    continue
                esm = filename.lower().endswith(".mjs") or package_type == "module" or bool(re.search(r"(?m)^\s*import\b", source))
                binding = f"import {missing_var} from '{package}';" if esm else f"const {missing_var} = require('{package}');"
                content = _insert_top_level_binding(source, binding)
                candidates.append({
                    "filename": filename,
                    "content": content,
                    "reason": f"Normalized authoritative Node evidence proves required dependency {package} is referenced without a module binding.",
                })
                diagnosis = f"Normalized Node evidence proved required dependency {package} is missing its module binding."
                hypothesis = f"Add only the missing {package} binding and preserve project behavior."
                break

    if not candidates:
        return None

    if any(_is_test_filename(item["filename"]) for item in candidates):
        _validate_test_change_integrity(run, spec, candidates)
    candidates = _filter_noop_changes(run, candidates)
    preflight = validate_candidate(
        run,
        candidates,
        baseline_execution=execution,
        purpose="repair",
    )
    changed = _apply_files(run, candidates)
    return {
        "model": "deterministic",
        "lane": "deterministic_mechanical",
        "diagnosis": diagnosis,
        "hypothesis": hypothesis,
        "harness_diagnostics": _test_harness_diagnostics(run, execution),
        "test_repair_authority": authority,
        "preflight": preflight,
        "changed": changed,
    }

def repair_project(run, spec, execution, cycle, acceptance_issues=None, repair_history=None):
    harness_diagnostics = _test_harness_diagnostics(run, execution)
    test_authority = _test_repair_authority(
        run,
        spec,
        execution=execution,
        acceptance_issues=acceptance_issues,
    )
    repair_directive = _acceptance_repair_directive(run, spec, acceptance_issues)
    allow_tests = bool(test_authority)
    deterministic_rejection = None
    try:
        deterministic = _deterministic_mechanical_repair(
            run,
            spec,
            execution,
            test_authority,
        )
    except (V3NodeError, V3CandidateError) as error:
        # A deterministic candidate is held to the same staged validation gate
        # as model candidates.  Preserve its exact rejection as evidence for a
        # subsequent reasoning lane rather than silently discarding it.
        deterministic = None
        deterministic_rejection = str(error)
    if deterministic:
        return deterministic
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
            "You MAY modify test files only within the explicit TEST REPAIR AUTHORITY supplied by ATLAS. Preserve every existing test case, expected error/message, and assertion count; repair mechanics rather than weakening semantics. "
            if allow_tests
            else
            "Test files are protected specifications in this repair cycle; do not modify them. "
        )
        + "Do not repeat an already-attempted hypothesis against materially unchanged evidence; use the repair history to choose a new explanation when needed. "
        + "Do not change package.json unless sandbox evidence or an acceptance issue actually requires a manifest/script/dependency change. "
        + "When ATLAS supplies a CONTRACT-DIRECTED REPAIR SCOPE, that scope is authoritative: address that contract gap only and do not edit unrelated files simply because the current sandbox command is green. "
        + "Return ONLY JSON: {diagnosis,hypothesis,changes:[{filename,content,reason}]}. Each content is the COMPLETE file. "
        + "For a .json file, content may be either the complete JSON string or a JSON object/array; ATLAS canonicalizes it."
    )
    user = (
        "ORIGINAL GOAL:\n" + str(run.get("goal") or "")
        + "\n\nPROJECT SPEC:\n" + json.dumps(spec, ensure_ascii=False, indent=2)
        + "\n\nLATEST AUTHORITATIVE SANDBOX EVIDENCE:\n" + evidence[-7000:]
        + "\n\nDETERMINISTIC TEST-HARNESS DIAGNOSTICS:\n" + harness_text
        + "\n\nTEST REPAIR AUTHORITY:\n" + json.dumps(test_authority, ensure_ascii=False, indent=2)[:5000]
        + "\n\nDETERMINISTIC CANDIDATE REJECTION (if any):\n" + str(deterministic_rejection or "none")[:3000]
        + "\n\nRECENT REPAIR HISTORY / OUTCOMES:\n" + history_text
        + "\n\nFINAL ACCEPTANCE ISSUES (if verification already passed):\n" + acceptance_text[:4000]
        + "\n\nCONTRACT-DIRECTED REPAIR SCOPE:\n" + json.dumps(repair_directive, ensure_ascii=False, indent=2)[:5000]
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
                schema=REPAIR_ACTION_SCHEMA,
                schema_name="node_repair_action_v1",
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
            contract_progress = _validate_acceptance_repair_scope(
                run, spec, files, repair_directive
            )
            files = _filter_noop_changes(run, files)
            preflight_purpose = (
                "contract_convergence"
                if bool(contract_progress.get("improved"))
                else "repair"
            )
            preflight = validate_candidate(
                run,
                files,
                baseline_execution=execution,
                purpose=preflight_purpose,
            )

            return {
                "model": model,
                "lane": "model_reasoning",
                "diagnosis": str(data.get("diagnosis") or "")[:3000],
                "hypothesis": str(data.get("hypothesis") or data.get("diagnosis") or "")[:2000],
                "harness_diagnostics": harness_diagnostics,
                "test_repair_authority": test_authority,
                "deterministic_rejection": deterministic_rejection,
                "repair_directive": repair_directive,
                "contract_progress": contract_progress,
                "preflight": preflight,
                "changed": _apply_files(run, files),
            }
        except (V3ModelError, V3NodeError, V3CandidateError) as error:
            last_error = str(error)
    raise V3NodeError("Repair could not produce a validated change-set: " + str(last_error or "unknown error"))


def inject_intentional_defect(run, spec, baseline_execution=None):
    """Prepare one proven failing implementation mutant for a requested demo.

    The primary lane is deterministic mutation testing.  A model is consulted
    only when no safe deterministic operator matches the target, and even then
    its proposal must fail in staged sandbox execution before durable promotion.
    """
    implementation = [
        name for name in _workspace_names(run)
        if name.lower().endswith((".js", ".mjs", ".cjs"))
        and not name.lower().startswith("test")
        and ".test." not in name.lower()
        and ".spec." not in name.lower()
    ]
    target = str(spec.get("intentional_defect_target") or "").strip()
    if target:
        matched = [name for name in implementation if name.lower() == target.lower()]
        if not matched:
            raise V3NodeError(
                f"Goal requires the controlled defect in {target}, but that implementation file is not present/eligible."
            )
        implementation = matched
    if not implementation:
        raise V3NodeError(
            "Goal requested a fail→repair demonstration but no implementation file can safely receive the deliberate defect."
        )

    # Prefer deterministic mutation testing: no model latency, no protocol
    # serialization risk, and the candidate is proven to fail before promotion.
    deterministic_errors = []
    for filename in implementation:
        try:
            selected = select_failing_node_mutant(
                run,
                filename,
                baseline_execution,
            )
            changed = _apply_files(run, selected.get("files") or [])
            return {
                "model": "deterministic",
                "lane": selected.get("lane") or "deterministic_mutation_testing",
                "operator": selected.get("operator"),
                "detail": selected.get("detail"),
                "preflight": selected.get("preflight") or {},
                "trials": selected.get("trials") or [],
                "changed": changed,
            }
        except V3MutationError as error:
            deterministic_errors.append(f"{filename}: {error}")

    # Generic fallback for projects where the conservative mutation library has
    # no matching operator.  This path is deliberately shorter than ordinary
    # reasoning calls because fault creation is not worth a multi-minute local
    # inference.  The proposed fault still must be proven in staging before it
    # can touch the durable workspace.
    system = (
        "You are ATLAS v3 controlled verification setup. The user explicitly requested one deliberate small implementation defect so the real tests fail before repair. "
        "Modify exactly ONE existing implementation file. If ATLAS supplies only one allowed file, that is an explicit user target and must be used. "
        "Do not change tests, package.json, dependencies, imports, exports, or public architecture. Make one syntax-valid localized behavioral defect. "
        "Return ONLY the structured action requested by the schema."
    )
    user = (
        "GOAL:\n" + str(run.get("goal") or "")
        + "\n\nALLOWED IMPLEMENTATION FILES:\n" + "\n".join("- " + name for name in implementation)
        + "\n\nDETERMINISTIC MUTATION RESULT:\n"
        + ("\n".join("- " + item for item in deterministic_errors) or "No deterministic operator matched.")
        + "\n\nCURRENT WORKSPACE:\n" + _workspace_sources(run, budget=16000, per_file=6000)
    )
    try:
        data, model = run_json(
            run,
            phase="intentional_defect",
            purpose="v3_intentional_defect_fallback",
            system_prompt=system,
            user_prompt=user,
            tier="worker",
            total_timeout_seconds=120,
            schema=DEFECT_ACTION_SCHEMA,
            schema_name="node_intentional_defect_action_v1",
        )
    except V3ModelError as error:
        raise V3NodeError(
            "Controlled defect preparation could not find a deterministic failing mutant, and the bounded model fallback did not complete: "
            + str(error)
        ) from error

    files = _parse_file_set(
        {"changes": data.get("changes") or []},
        spec,
        require_all_explicit=False,
        allow_test_changes=False,
    )
    if len(files) != 1 or files[0]["filename"] not in implementation:
        raise V3NodeError(
            "Intentional-defect fallback must modify exactly one existing implementation file."
        )

    try:
        preflight = validate_candidate(
            run,
            files,
            baseline_execution=None,
            purpose="intentional_defect:model_fallback",
        )
    except V3CandidateError as error:
        raise V3NodeError(
            "Intentional-defect fallback candidate failed staged validation: " + str(error)
        ) from error

    staged_execution = preflight.get("execution") or {}
    if not is_legitimate_failing_execution(staged_execution):
        raise V3NodeError(
            "Intentional-defect fallback candidate was not promoted because staged tests did not produce a real failure."
        )

    return {
        "model": model,
        "lane": "model_fallback_staged",
        "operator": "model_proposed",
        "detail": "Model fallback produced a staged/proven failing implementation mutation.",
        "preflight": preflight,
        "trials": [],
        "changed": _apply_files(run, files),
    }




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
        # "No external dependencies" means no dependencies beyond packages the
        # user explicitly required. A required package can never simultaneously
        # be classified as forbidden by the same user-owned contract.
        allowed = {str(item).lower() for item in (spec.get("required_dependencies") or [])}
        forbidden = sorted(name for name in deps.keys() if name not in allowed)
        if forbidden:
            issues.append({
                "type": "forbidden_dependency",
                "item": ", ".join(forbidden[:20]),
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
        try:
            from app.services.agent_v3_storage import demonstration_status
            demo = demonstration_status(run["user_id"], run["id"])
        except Exception:
            demo = {"satisfied": False}
        if not demo.get("satisfied"):
            issues.append({
                "type": "required_failure_not_observed",
                "item": "fail_then_repair",
                "detail": (
                    "ATLAS requires lifecycle-owned evidence of baseline pass → controlled defect → observed failure → repaired pass. "
                    "Ordinary bootstrap or accidental repair failures do not satisfy this demonstration."
                ),
            })

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



def evaluate_baseline_acceptance(run, spec, execution):
    """Evaluate intended-correct baseline readiness before fault injection.

    When the user explicitly requested a fail→repair demonstration, that
    lifecycle-owned evidence cannot exist until after a clean baseline has been
    proven.  The baseline gate therefore evaluates the complete original user
    contract with only ``requires_fail_then_repair`` deferred.  Missing files,
    dependencies, test coverage and semantic behaviors remain fully enforced.
    """
    baseline_spec = dict(spec or {})
    baseline_spec["requires_fail_then_repair"] = False
    result = evaluate_acceptance(run, baseline_spec, execution)
    result["baseline_gate"] = True
    result["deferred_lifecycle_requirements"] = (
        ["fail_then_repair"] if bool((spec or {}).get("requires_fail_then_repair")) else []
    )
    return result


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

