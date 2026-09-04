"""ATLAS v3.2 staged mutation pipeline for Node/JavaScript candidates.

Model output is never promoted directly into the durable Agent workspace.
A candidate change-set is first overlaid onto an isolated temporary project,
validated structurally, and (when the current dependency image can represent
it) executed inside the same network-disabled Docker security boundary used by
normal Agent verification.

This layer is intentionally language-adapter infrastructure: future Python,
TypeScript and frontend adapters can implement equivalent candidate validators
without changing orchestration policy.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import app.config as config
from app.services import agent_runner as legacy_runner
from app.services.agent_node_environment import resolve_node_execution_image
from app.services.agent_sandbox import (
    SANDBOX_CPUS,
    SANDBOX_MEMORY,
    SANDBOX_NODE_RUNTIME_TMPFS,
    list_workspace_files,
    read_workspace_file,
)
from app.services.agent_v3_repair_governor import compare_evidence
from app.services.agent_v3_storage import record_candidate_validation


class V3CandidateError(Exception):
    pass


_JS_SUFFIXES = (".js", ".mjs", ".cjs")


def _docker_env():
    return {
        key: value
        for key, value in {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME"),
            "DOCKER_HOST": os.environ.get("DOCKER_HOST"),
            "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT"),
        }.items()
        if value
    }


def _snapshot_workspace(run):
    result = {}
    for item in list_workspace_files(run["user_id"], run["id"]):
        name = str(item.get("filename") or "")
        if not name:
            continue
        try:
            result[name] = read_workspace_file(
                run["user_id"], run["id"], name, max_chars=240000
            )
        except Exception:
            continue
    return result


def _candidate_files(run, changes):
    files = _snapshot_workspace(run)
    for item in changes or []:
        files[str(item["filename"])] = str(item["content"])
    return files


def _package(files):
    raw = files.get("package.json")
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as error:
        raise V3CandidateError("Candidate package.json is not valid JSON: " + str(error)) from error
    if not isinstance(parsed, dict):
        raise V3CandidateError("Candidate package.json must contain one JSON object.")
    return parsed


def _dependency_signature(package):
    return json.dumps(
        {
            "dependencies": package.get("dependencies") or {},
            "devDependencies": package.get("devDependencies") or {},
            "optionalDependencies": package.get("optionalDependencies") or {},
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _static_module_issues(files, package):
    issues = []
    package_type = str(package.get("type") or "").strip().lower()
    for name, source in files.items():
        lower = name.lower()
        if not lower.endswith(_JS_SUFFIXES):
            continue
        text = str(source or "")
        has_require = bool(re.search(r"\brequire\s*\(", text))
        has_cjs_export = bool(re.search(r"\b(?:module\.exports|exports\.)", text))
        has_esm = bool(re.search(r"(?m)^\s*(?:import|export)\b", text))
        # A full JS parser belongs in future language intelligence, but this
        # deterministic rule catches Node's ambiguous CommonJS/top-level-await
        # failure before the candidate can touch the durable workspace.
        top_level_await_hint = bool(re.search(r"(?m)^await\s+", text))

        if lower.endswith(".cjs") and top_level_await_hint:
            issues.append(f"{name}: .cjs cannot use top-level await.")
        if lower.endswith(".js") and package_type != "module" and top_level_await_hint and (has_require or has_cjs_export):
            issues.append(
                f"{name}: candidate mixes CommonJS require/exports with top-level await; Node cannot determine one module format."
            )
        if lower.endswith(".js") and package_type == "module" and (has_require or has_cjs_export) and not has_esm:
            issues.append(
                f"{name}: package.json declares ESM but this file is written as CommonJS."
            )
    return issues


def _test_command(package, files):
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    if str(scripts.get("test") or "").strip():
        return "npm run test"
    for preferred in ("test.js", "tests.js", "index.test.js", "app.test.js"):
        if preferred in files:
            return f"node --test {preferred}"
    js_files = [name for name in files if name.lower().endswith(_JS_SUFFIXES)]
    if js_files:
        joined = " && ".join('node --check "' + name.replace('"', '') + '"' for name in js_files[:20])
        return joined
    return None


def _run_candidate_container(run, files, command_text, timeout_seconds=45):
    if not command_text:
        return {
            "status": "skipped",
            "exit_code": None,
            "duration_ms": 0,
            "stdout": "",
            "stderr": "No executable Node candidate preflight target was available.",
            "runtime": "node",
            "execution_action": "candidate_preflight",
            "command": None,
        }

    try:
        image = resolve_node_execution_image(run["user_id"], run["id"])
    except Exception as error:
        # Environment may legitimately need rebuilding because package.json was
        # changed. Structural validation still ran, so return an explicit skip
        # rather than pretending the candidate passed execution.
        return {
            "status": "skipped",
            "exit_code": None,
            "duration_ms": 0,
            "stdout": "",
            "stderr": "Candidate execution preflight deferred until dependency environment refresh: " + str(error),
            "runtime": "node",
            "execution_action": "candidate_preflight",
            "command": command_text,
        }

    stage_parent = (config.GENERATED_DIR / "_v3_candidate_staging").resolve()
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix="atlas-v3-candidate-", dir=str(stage_parent))).resolve()
    try:
        for name, content in files.items():
            target = (stage_root / name).resolve()
            if stage_root.resolve() not in target.parents:
                raise V3CandidateError("Candidate staging path escaped its temporary workspace.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            try:
                target.chmod(0o444)
            except OSError:
                pass

        container_name = "atlas-v3-candidate-" + str(run["id"])[:8] + "-" + uuid.uuid4().hex[:8]
        runtime_script = (
            "set -eu; cp -R /workspace/. /runtime/; "
            "find /runtime -mindepth 1 -exec chmod u+rwX {} +; "
            "if [ -d /opt/atlas/node_modules ] && [ ! -e /runtime/node_modules ]; "
            "then ln -s /opt/atlas/node_modules /runtime/node_modules; fi; "
            "cd /runtime; " + command_text
        )
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", "none",
            "--memory", SANDBOX_MEMORY,
            "--cpus", SANDBOX_CPUS,
            "--pids-limit", "64",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--tmpfs", f"/runtime:rw,exec,nosuid,nodev,size={SANDBOX_NODE_RUNTIME_TMPFS},mode=1777",
            "--env", "HOME=/tmp",
            "--env", "TMPDIR=/tmp",
            "--env", "NPM_CONFIG_UPDATE_NOTIFIER=false",
            "--env", "NPM_CONFIG_FUND=false",
            "--env", "NPM_CONFIG_AUDIT=false",
            "--mount", f"type=bind,source={stage_root},target=/workspace,readonly",
            "--workdir", "/runtime",
            "--user", "65534:65534",
            image,
            "sh", "-lc", runtime_script,
        ]

        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_docker_env(),
        )
        deadline = started + max(5, int(timeout_seconds))
        timed_out = False
        try:
            while process.poll() is None:
                legacy_runner._control_probe(run)
                if time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                    except Exception:
                        pass
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
                time.sleep(0.20)
        except Exception:
            try:
                subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            except Exception:
                pass
            try:
                process.kill()
            except OSError:
                pass
            raise
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        status = "timeout" if timed_out else ("success" if process.returncode == 0 else "failed")
        return {
            "status": status,
            "exit_code": process.returncode,
            "duration_ms": duration_ms,
            "stdout": str(stdout or "")[:12000],
            "stderr": str(stderr or "")[:12000],
            "runtime": "node",
            "execution_action": "candidate_preflight",
            "command": command_text,
            "image": image,
        }
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def validate_candidate(run, changes, *, baseline_execution=None, purpose="repair"):
    """Validate a complete staged candidate before durable workspace mutation.

    Repair candidates must not regress the latest authoritative execution. A
    changed-but-not-yet-green failure is acceptable because the outer repair
    governor still decides whether another committed repair is justified.
    """
    candidate = _candidate_files(run, changes)
    package = _package(candidate)
    issues = _static_module_issues(candidate, package)
    if issues:
        result = {
            "accepted": False,
            "purpose": purpose,
            "structural_issues": issues,
            "execution": None,
            "progress": None,
            "detail": "; ".join(issues),
        }
        record_candidate_validation(run, result)
        raise V3CandidateError(result["detail"])

    current = _snapshot_workspace(run)
    current_package = _package(current) if "package.json" in current else {}
    dependencies_changed = _dependency_signature(package) != _dependency_signature(current_package)

    command_text = _test_command(package, candidate)
    execution = None
    if not dependencies_changed:
        execution = _run_candidate_container(run, candidate, command_text)
    else:
        execution = {
            "status": "skipped",
            "exit_code": None,
            "duration_ms": 0,
            "stdout": "",
            "stderr": "Candidate changed npm dependencies; execution preflight deferred until isolated environment rebuild.",
            "runtime": "node",
            "execution_action": "candidate_preflight",
            "command": command_text,
        }

    progress = None
    accepted = True
    detail = "Candidate passed structural staging validation."
    if purpose == "repair" and baseline_execution and execution.get("status") != "skipped":
        comparable_execution = dict(execution)
        # Candidate preflight is a different control-plane action, but progress
        # comparison must fingerprint the project failure itself rather than the
        # fact that it ran in staging.
        comparable_execution["execution_action"] = baseline_execution.get("execution_action")
        comparable_execution["command"] = baseline_execution.get("command") or execution.get("command")
        progress = compare_evidence(baseline_execution, comparable_execution)
        classification = str(progress.get("classification") or "")
        if classification in {"regression", "stalled"}:
            accepted = False
            detail = (
                "Staged candidate was rejected before workspace mutation because its preflight was "
                + classification
                + ": "
                + str(progress.get("reason") or "candidate did not improve authoritative evidence")
            )
        else:
            detail = "Staged candidate preflight classified as " + classification + "."

    result = {
        "accepted": accepted,
        "purpose": purpose,
        "dependencies_changed": dependencies_changed,
        "structural_issues": [],
        "execution": execution,
        "progress": progress,
        "detail": detail,
    }
    record_candidate_validation(run, result)
    if not accepted:
        raise V3CandidateError(detail + "\n" + str((execution or {}).get("stderr") or "")[-3000:])
    return result
