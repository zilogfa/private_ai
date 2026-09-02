import mimetypes
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import app.config as config

from app.database import get_connection, user_has_permission
from app.services.agents import (
    AgentStoreError,
    ALLOWED_ARTIFACT_EXTENSIONS,
    get_agent_artifact_path,
    get_agent_run,
    list_agent_artifacts,
    utc_iso,
)

AGENT_CODE_PERMISSION = "agent.code.execute"
SANDBOX_IMAGE = os.environ.get(
    "PRIVATE_AI_AGENT_SANDBOX_IMAGE", "python:3.11-slim"
).strip()
SANDBOX_TIMEOUT_SECONDS = int(
    os.environ.get("PRIVATE_AI_AGENT_SANDBOX_TIMEOUT_SECONDS", "30")
)
SANDBOX_MEMORY = os.environ.get(
    "PRIVATE_AI_AGENT_SANDBOX_MEMORY", "512m"
).strip()
SANDBOX_CPUS = os.environ.get(
    "PRIVATE_AI_AGENT_SANDBOX_CPUS", "1.0"
).strip()
SANDBOX_RUNTIME_TMPFS = os.environ.get(
    "PRIVATE_AI_AGENT_SANDBOX_RUNTIME_TMPFS", "128m"
).strip()
SANDBOX_MAX_WORKSPACE_FILES = int(
    os.environ.get("PRIVATE_AI_AGENT_SANDBOX_MAX_FILES", "20")
)
SANDBOX_MAX_READ_CHARS = 30000
SANDBOX_MAX_STDOUT_CHARS = 12000
SANDBOX_MAX_STDERR_CHARS = 12000

_STATUS_CACHE = {"checked": 0.0, "value": None}
_STORAGE_READY = False


class AgentSandboxError(Exception):
    pass


class AgentSandboxUnavailable(AgentSandboxError):
    pass


def initialize_agent_sandbox_storage():
    global _STORAGE_READY
    if _STORAGE_READY:
        return

    conn = get_connection()
    cur = conn.cursor()
    ts = utc_iso()

    cur.execute(
        """
        INSERT OR IGNORE INTO permissions (
            name, description, created_at, updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            AGENT_CODE_PERMISSION,
            "Allow opted-in agent runs to execute Python inside the local Docker sandbox.",
            ts,
            ts,
        ),
    )
    cur.execute("SELECT id FROM permissions WHERE name = ?", (AGENT_CODE_PERMISSION,))
    row = cur.fetchone()
    if row:
        permission_id = row[0]
        cur.execute("SELECT id FROM roles WHERE name IN ('owner', 'admin', 'user')")
        for (role_id,) in cur.fetchall():
            cur.execute(
                """
                INSERT OR IGNORE INTO role_permissions (
                    role_id, permission_id, granted_at
                ) VALUES (?, ?, ?)
                """,
                (role_id, permission_id, ts),
            )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_capabilities (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            allow_code INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_sandbox_executions (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            step_id INTEGER,
            filename TEXT NOT NULL,
            image TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            stdout_text TEXT,
            stderr_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (step_id) REFERENCES agent_steps(id) ON DELETE SET NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_sandbox_exec_run
        ON agent_sandbox_executions(run_id, created_at)
        """
    )
    conn.commit()
    conn.close()
    _STORAGE_READY = True


def set_agent_run_code_access(user_id, run_id, allowed):
    initialize_agent_sandbox_storage()
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")
    if allowed and not user_has_permission(user_id, AGENT_CODE_PERMISSION):
        raise AgentStoreError(
            "This account does not have sandboxed code-execution permission."
        )

    ts = utc_iso()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_run_capabilities (
            run_id, user_id, allow_code, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            allow_code = excluded.allow_code,
            updated_at = excluded.updated_at
        """,
        (str(run_id), int(user_id), int(bool(allowed)), ts, ts),
    )
    conn.commit()
    conn.close()


def agent_run_allows_code(user_id, run_id):
    initialize_agent_sandbox_storage()
    if not user_has_permission(user_id, AGENT_CODE_PERMISSION):
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT allow_code FROM agent_run_capabilities WHERE run_id = ? AND user_id = ?",
        (str(run_id), int(user_id)),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


def _quick(command, timeout=4):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ), None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def sandbox_runtime_profile(user_id=None, run_id=None):
    """Deterministic execution/setup capability profile."""
    profile_name = "strict"
    dependency_installation = False
    dependency_status = None

    if user_id is not None and run_id is not None:
        try:
            from app.services.agent_environment import (
                ENV_PROFILE_PROJECT,
                environment_status_for_run,
                get_agent_run_environment,
                project_environment_allowed,
            )

            environment = get_agent_run_environment(user_id, run_id)
            profile_name = environment.get("profile") or "strict"
            dependency_installation = bool(
                profile_name == ENV_PROFILE_PROJECT
                and project_environment_allowed(user_id, run_id)
            )
            dependency_status = environment_status_for_run(user_id, run_id)
        except Exception:
            dependency_installation = False

    return {
        "profile": profile_name,
        "source_mount": "/workspace",
        "source_read_only": True,
        "runtime_workdir": "/runtime",
        "runtime_writable": True,
        "runtime_ephemeral": True,
        "runtime_tmpfs": SANDBOX_RUNTIME_TMPFS,
        "tmp_dir": "/tmp",
        "tmp_writable": True,
        "network": False,
        "execution_network": False,
        "setup_network": bool(dependency_installation),
        "runs_as_root": False,
        "python_image": SANDBOX_IMAGE,
        "dependency_installation": dependency_installation,
        "dependency_status": dependency_status,
        "dependency_note": (
            "Project profile may download sanitized requirements during a separate "
            "isolated Docker build; normal execution remains network-disabled."
            if dependency_installation
            else (
                "Strict profile uses only packages already present in the base image. "
                "Third-party dependency download is disabled."
            )
        ),
    }


def sandbox_status(force=False):
    now = time.monotonic()
    if (
        not force
        and _STATUS_CACHE["value"] is not None
        and now - float(_STATUS_CACHE["checked"] or 0.0) < 8.0
    ):
        return dict(_STATUS_CACHE["value"])

    if not shutil.which("docker"):
        value = {
            "ready": False,
            "docker_cli": False,
            "docker_daemon": False,
            "image_ready": False,
            "image": SANDBOX_IMAGE,
            "message": "Docker CLI was not found. Install/start Docker Desktop first.",
        }
    else:
        info, info_error = _quick(
            ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=4
        )
        daemon_ready = bool(info and info.returncode == 0)
        if not daemon_ready:
            detail = ((info.stderr if info else "") or info_error or "").strip()
            value = {
                "ready": False,
                "docker_cli": True,
                "docker_daemon": False,
                "image_ready": False,
                "image": SANDBOX_IMAGE,
                "message": "Docker Desktop is not running. " + detail[:500],
            }
        else:
            image, _ = _quick(["docker", "image", "inspect", SANDBOX_IMAGE], timeout=5)
            image_ready = bool(image and image.returncode == 0)
            value = {
                "ready": image_ready,
                "docker_cli": True,
                "docker_daemon": True,
                "image_ready": image_ready,
                "image": SANDBOX_IMAGE,
                "message": (
                    "Docker Python sandbox is ready. Executions use a writable disposable runtime copy; network access is disabled."
                    if image_ready
                    else f"Sandbox image is not installed. Run: docker pull {SANDBOX_IMAGE}"
                ),
            }

    _STATUS_CACHE["checked"] = now
    _STATUS_CACHE["value"] = dict(value)
    return value


def _workspace(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentSandboxError("Agent run was not found.")
    root = config.GENERATED_DIR.resolve()
    path = (config.GENERATED_DIR / str(run.get("workspace_rel_path") or "")).resolve()
    if path == root or root not in path.parents:
        raise AgentSandboxError("Invalid agent workspace path.")
    for name in ("files", "artifacts", "logs", "sandbox"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(filename):
    from werkzeug.utils import secure_filename
    value = secure_filename(str(filename or "").strip())
    if not value:
        raise AgentSandboxError("A workspace filename is required.")
    suffix = Path(value).suffix.lower()
    if suffix not in ALLOWED_ARTIFACT_EXTENSIONS:
        raise AgentSandboxError("Unsupported workspace file type.")
    return value[:180], suffix


def _latest_files(user_id, run_id):
    latest = {}
    for item in list_agent_artifacts(user_id, run_id):
        if str(item.get("kind") or "") != "workspace_file":
            continue
        name = str(item.get("filename") or "").strip()
        if name:
            latest[name] = item
    return latest


def list_workspace_files(user_id, run_id):
    latest = _latest_files(user_id, run_id)
    return [
        {
            "filename": name,
            "size_bytes": int(latest[name].get("size_bytes") or 0),
            "artifact_id": latest[name].get("id"),
            "created_at": latest[name].get("created_at"),
        }
        for name in sorted(latest, key=str.lower)
    ]


def read_workspace_file(user_id, run_id, filename, max_chars=SANDBOX_MAX_READ_CHARS):
    safe_name, _ = _safe_name(filename)
    item = _latest_files(user_id, run_id).get(safe_name)
    if not item:
        raise AgentSandboxError(f"Workspace file was not found: {safe_name}")
    _, path = get_agent_artifact_path(user_id, item["id"])
    if not path:
        raise AgentSandboxError(f"Workspace file is missing on disk: {safe_name}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AgentSandboxError("Workspace file is not readable UTF-8 text.") from exc
    return text[:max_chars]


def write_workspace_file(user_id, run_id, filename, content):
    safe_name, suffix = _safe_name(filename)
    encoded = str(content if content is not None else "").encode("utf-8")
    max_bytes = int(getattr(config, "AGENT_MAX_ARTIFACT_BYTES", 256 * 1024))
    if len(encoded) > max_bytes:
        raise AgentSandboxError("Workspace file is larger than the configured limit.")

    latest = _latest_files(user_id, run_id)
    existing = latest.get(safe_name)
    if existing is None and len(latest) >= SANDBOX_MAX_WORKSPACE_FILES:
        raise AgentSandboxError("Sandbox workspace file limit reached for this run.")

    ts = utc_iso()
    mime = ALLOWED_ARTIFACT_EXTENSIONS.get(
        suffix, mimetypes.guess_type(safe_name)[0] or "text/plain"
    )

    if existing:
        _, path = get_agent_artifact_path(user_id, existing["id"])
        if not path:
            raise AgentSandboxError("Existing workspace file is missing on disk.")

        # Preserve the previous working copy before the Agent overwrites it.
        # Import locally to avoid coupling the low-level sandbox module during
        # startup and keep legacy workspaces compatible.
        try:
            from app.services.agent_file_versions import (
                ensure_current_artifact_version,
            )

            ensure_current_artifact_version(
                user_id,
                existing["id"],
            )
        except Exception as error:
            raise AgentSandboxError(
                f"Could not snapshot the previous file revision: {error}"
            ) from error

        path.write_bytes(encoded)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE agent_artifacts
            SET mime_type = ?, size_bytes = ?, created_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (mime, len(encoded), ts, existing["id"], int(user_id)),
        )
        conn.commit()
        conn.close()

        try:
            from app.services.agent_file_versions import (
                record_current_artifact_version,
            )

            version = record_current_artifact_version(
                user_id,
                existing["id"],
                source="agent_write",
                note="Workspace file updated by Agent/tool execution.",
            )
        except Exception as error:
            raise AgentSandboxError(
                f"File was updated but version history could not be recorded: {error}"
            ) from error

        return {
            "filename": safe_name,
            "size_bytes": len(encoded),
            "updated": True,
            "artifact_id": existing["id"],
            "version_number": version.get("version_number"),
        }

    workspace = _workspace(user_id, run_id)
    files_dir = (workspace / "files").resolve()
    artifact_id = uuid.uuid4().hex
    path = (files_dir / f"{artifact_id[:8]}_{safe_name}").resolve()
    if files_dir not in path.parents:
        raise AgentSandboxError("Invalid workspace file path.")
    path.write_bytes(encoded)
    relative = str(path.relative_to(config.GENERATED_DIR.resolve()))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_artifacts (
            id, run_id, user_id, filename, relative_path,
            mime_type, kind, size_bytes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'workspace_file', ?, ?)
        """,
        (
            artifact_id,
            str(run_id),
            int(user_id),
            safe_name,
            relative,
            mime,
            len(encoded),
            ts,
        ),
    )
    conn.commit()
    conn.close()

    try:
        from app.services.agent_file_versions import (
            record_current_artifact_version,
        )

        version = record_current_artifact_version(
            user_id,
            artifact_id,
            source="agent_write",
            note="Initial workspace file revision.",
        )
    except Exception as error:
        raise AgentSandboxError(
            f"File was created but version history could not be recorded: {error}"
        ) from error

    return {
        "filename": safe_name,
        "size_bytes": len(encoded),
        "updated": False,
        "artifact_id": artifact_id,
        "version_number": version.get("version_number"),
    }


def _bundle(user_id, run_id):
    workspace = _workspace(user_id, run_id)
    execution_id = uuid.uuid4().hex
    root = (workspace / "sandbox").resolve()
    bundle = (root / execution_id).resolve()
    if root not in bundle.parents:
        raise AgentSandboxError("Invalid sandbox bundle path.")
    bundle.mkdir(parents=True, exist_ok=False)

    for name, item in _latest_files(user_id, run_id).items():
        _, source = get_agent_artifact_path(user_id, item["id"])
        if not source:
            continue
        target = (bundle / name).resolve()
        if bundle not in target.parents:
            continue
        shutil.copyfile(source, target)
        try:
            target.chmod(0o444)
        except OSError:
            pass
    return execution_id, bundle


def _record_execution(
    user_id,
    run_id,
    step_id,
    execution_id,
    filename,
    status,
    exit_code,
    duration_ms,
    stdout_text,
    stderr_text,
    image=None,
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_sandbox_executions (
            id, run_id, user_id, step_id, filename, image, status,
            exit_code, duration_ms, stdout_text, stderr_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            execution_id,
            str(run_id),
            int(user_id),
            int(step_id) if step_id else None,
            filename,
            str(image or SANDBOX_IMAGE),
            status,
            exit_code,
            int(duration_ms),
            str(stdout_text or "")[:SANDBOX_MAX_STDOUT_CHARS],
            str(stderr_text or "")[:SANDBOX_MAX_STDERR_CHARS],
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()


def list_agent_sandbox_executions(user_id, run_id, limit=20):
    initialize_agent_sandbox_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, step_id, filename, image, status, exit_code,
               duration_ms, stdout_text, stderr_text, created_at
        FROM agent_sandbox_executions
        WHERE run_id = ? AND user_id = ?
        ORDER BY created_at ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(100, int(limit)))),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "step_id": row[1],
            "filename": row[2],
            "image": row[3],
            "status": row[4],
            "exit_code": row[5],
            "duration_ms": int(row[6] or 0),
            "stdout": row[7],
            "stderr": row[8],
            "created_at": row[9],
        }
        for row in rows
    ]


def _remove_container(name):
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_python_sandbox(user_id, run_id, filename, step_id=None, cancel_check=None):
    initialize_agent_sandbox_storage()
    if not agent_run_allows_code(user_id, run_id):
        raise AgentSandboxError("This agent run was not allowed to execute code.")

    status = sandbox_status(force=True)
    if not status.get("ready"):
        raise AgentSandboxUnavailable(status.get("message") or "Docker sandbox unavailable.")

    safe_name, suffix = _safe_name(filename)
    if suffix != ".py":
        raise AgentSandboxError("ATLAS sandbox execution currently supports Python entry files only.")
    if safe_name not in _latest_files(user_id, run_id):
        raise AgentSandboxError(f"Python workspace file was not found: {safe_name}")

    from app.services.agent_environment import resolve_execution_image

    execution_image = resolve_execution_image(user_id, run_id)

    execution_id, bundle = _bundle(user_id, run_id)
    if not (bundle / safe_name).is_file():
        shutil.rmtree(bundle, ignore_errors=True)
        raise AgentSandboxError(f"Python workspace file was not materialized: {safe_name}")

    container_name = f"private-ai-agent-{str(run_id)[:8]}-{execution_id[:8]}"
    # Security model:
    # - /workspace is the immutable host-backed source snapshot.
    # - /runtime is a disposable tmpfs copy owned by the unprivileged container user.
    #   Programs may create JSON, SQLite DBs, caches, generated files, Flask runtime
    #   files, etc. without ever mutating the durable host workspace.
    # - /tmp is independently writable for tempfile and test fixtures.
    runtime_script = (
        "set -eu; "
        "cp -R /workspace/. /runtime/; "
        # /runtime itself is the Docker-created tmpfs root and is owned by root.
        # The unprivileged execution user must not chmod that mountpoint.
        # Files/directories copied INTO it are owned by the execution user and
        # can safely be made writable.
        "find /runtime -mindepth 1 -exec chmod u+rwX {} +; "
        "cd /runtime; "
        "exec python -B \"$1\""
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
        "--tmpfs", f"/runtime:rw,exec,nosuid,nodev,size={SANDBOX_RUNTIME_TMPFS},mode=1777",
        "--env", "HOME=/tmp",
        "--env", "TMPDIR=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONUNBUFFERED=1",
        "--mount", f"type=bind,source={bundle},target=/workspace,readonly",
        "--workdir", "/runtime",
        "--user", "65534:65534",
        execution_image,
        "sh", "-lc", runtime_script, "atlas-runtime", safe_name,
    ]

    started = time.monotonic()
    stdout_text = ""
    stderr_text = ""
    exit_code = None
    exec_status = "error"
    process = None

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                key: value
                for key, value in {
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME"),
                    "DOCKER_HOST": os.environ.get("DOCKER_HOST"),
                    "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT"),
                }.items()
                if value
            },
        )
        deadline = started + max(2, SANDBOX_TIMEOUT_SECONDS)

        while process.poll() is None:
            if cancel_check:
                try:
                    cancel_check()
                except Exception:
                    _remove_container(container_name)
                    try:
                        process.kill()
                    except OSError:
                        pass
                    raise
            if time.monotonic() >= deadline:
                _remove_container(container_name)
                try:
                    process.kill()
                except OSError:
                    pass
                exec_status = "timeout"
                break
            time.sleep(0.20)

        try:
            stdout_text, stderr_text = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            stdout_text, stderr_text = process.communicate()

        exit_code = process.returncode
        if exec_status != "timeout":
            exec_status = "success" if exit_code == 0 else "failed"

    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            _record_execution(
                user_id,
                run_id,
                step_id,
                execution_id,
                safe_name,
                exec_status,
                exit_code,
                duration_ms,
                stdout_text,
                stderr_text,
                image=execution_image,
            )
        finally:
            shutil.rmtree(bundle, ignore_errors=True)

    return {
        "id": execution_id,
        "filename": safe_name,
        "status": exec_status,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout": stdout_text[:SANDBOX_MAX_STDOUT_CHARS],
        "stderr": stderr_text[:SANDBOX_MAX_STDERR_CHARS],
        "image": execution_image,
    }


def format_execution_observation(execution):
    status = str(execution.get("status") or "unknown").upper()
    lines = [
        f"Sandbox Python execution: {execution.get('filename')}",
        (
            f"Status: {status}"
            + (f" · exit code {execution.get('exit_code')}" if execution.get("exit_code") is not None else "")
            + (f" · {execution.get('duration_ms')} ms" if execution.get("duration_ms") is not None else "")
        ),
        "Security: Docker container, execution network disabled, durable source mounted read-only; execution runs from a writable disposable runtime copy.",
        f"Environment image: {execution.get('image')}",
        "\nSTDOUT:\n" + (str(execution.get("stdout") or "").strip() or "[empty]"),
    ]
    stderr_text = str(execution.get("stderr") or "").strip()
    if stderr_text:
        lines.append("\nSTDERR:\n" + stderr_text)
    if status in {"FAILED", "TIMEOUT"}:
        lines.append(
            "\nThe program did not pass execution. Inspect the output, revise the workspace file(s), and re-run when useful."
        )
    return "\n".join(lines)
