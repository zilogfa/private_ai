"""
ATLAS v2.2 - dependency-aware Python project environments.

Security model:
- normal execution remains network-disabled
- durable user workspace remains read-only to executed code
- dependency setup is a separate, explicit capability/profile
- setup sends only sanitized dependency names/specifiers to the package index
- project source code is NEVER copied into the dependency-build context
- only binary wheels are accepted by default (no source builds/setup.py)
- the resulting dependency image is content-addressed and reusable

This is intentionally a project-environment foundation, not a Flask-specific fix.
"""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from app.database import get_connection, user_has_permission
from app.services.agents import AgentStoreError, get_agent_run, utc_iso
from app.services.agent_sandbox import (
    SANDBOX_IMAGE,
    AgentSandboxError,
    list_agent_sandbox_executions,
    list_workspace_files,
    read_workspace_file,
    sandbox_status,
    write_workspace_file,
)


AGENT_ENVIRONMENT_PERMISSION = "agent.environment.setup"

ENV_PROFILE_STRICT = "strict"
ENV_PROFILE_PROJECT = "project"
VALID_ENV_PROFILES = {
    ENV_PROFILE_STRICT,
    ENV_PROFILE_PROJECT,
}

ENV_REQUIREMENTS_FILE = "requirements.txt"
ENV_IMAGE_PREFIX = os.environ.get(
    "PRIVATE_AI_AGENT_ENV_IMAGE_PREFIX",
    "atlas-python-env",
).strip().lower() or "atlas-python-env"
ENV_SETUP_TIMEOUT_SECONDS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_ENV_SETUP_TIMEOUT_SECONDS",
        "420",
    )
)
ENV_MAX_DEPENDENCIES = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_ENV_MAX_DEPENDENCIES",
        "24",
    )
)
ENV_MAX_REQUIREMENTS_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_ENV_MAX_REQUIREMENTS_CHARS",
        "4000",
    )
)
ENV_MAX_BUILD_LOG_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_ENV_MAX_BUILD_LOG_CHARS",
        "18000",
    )
)
ENV_ONLY_BINARY = (
    os.environ.get(
        "PRIVATE_AI_AGENT_ENV_ONLY_BINARY",
        "1",
    )
    != "0"
)

_STORAGE_READY = False


class AgentEnvironmentError(AgentSandboxError):
    pass


class AgentEnvironmentNotReady(AgentEnvironmentError):
    pass


# Common import-name -> PyPI-distribution mappings. Unknown safe import names
# fall back to their normalized module name; explicit requirements.txt remains
# authoritative and can always override this inference.
_IMPORT_PACKAGE_MAP = {
    "flask": "Flask",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "requests": "requests",
    "httpx": "httpx",
    "numpy": "numpy",
    "pandas": "pandas",
    "matplotlib": "matplotlib",
    "sqlalchemy": "SQLAlchemy",
    "pytest": "pytest",
    "pydantic": "pydantic",
    "jinja2": "Jinja2",
    "werkzeug": "Werkzeug",
    "yaml": "PyYAML",
    "pil": "Pillow",
    "pillow": "Pillow",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "cv2": "opencv-python-headless",
    "dotenv": "python-dotenv",
}

# Conservative PEP-508 subset: package/extras + ordinary version constraints.
# Direct URLs, VCS, local paths, editable installs, custom indexes and markers
# are intentionally rejected in the Project profile.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
    r"(?:(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+"
    r"(?:,(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+)*)?$"
)


def initialize_agent_environment_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()
    timestamp = utc_iso()

    cursor.execute(
        """
        INSERT OR IGNORE INTO permissions (
            name,
            description,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            AGENT_ENVIRONMENT_PERMISSION,
            (
                "Allow opted-in Agent project environments to download sanitized "
                "Python dependencies during an isolated Docker build phase."
            ),
            timestamp,
            timestamp,
        ),
    )

    cursor.execute(
        "SELECT id FROM permissions WHERE name = ?",
        (AGENT_ENVIRONMENT_PERMISSION,),
    )
    row = cursor.fetchone()

    if row:
        permission_id = row[0]
        cursor.execute(
            "SELECT id FROM roles WHERE name IN ('owner', 'admin', 'user')"
        )
        for (role_id,) in cursor.fetchall():
            cursor.execute(
                """
                INSERT OR IGNORE INTO role_permissions (
                    role_id,
                    permission_id,
                    granted_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    role_id,
                    permission_id,
                    timestamp,
                ),
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_environments (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            profile TEXT NOT NULL DEFAULT 'strict',
            requirements_hash TEXT,
            image_tag TEXT,
            status TEXT NOT NULL DEFAULT 'base',
            requested_requirements_json TEXT NOT NULL DEFAULT '[]',
            resolved_manifest_json TEXT NOT NULL DEFAULT '[]',
            last_error TEXT,
            build_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_environment_builds (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            requirements_hash TEXT NOT NULL,
            base_image TEXT NOT NULL,
            image_tag TEXT NOT NULL,
            status TEXT NOT NULL,
            cached INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            requested_requirements_json TEXT NOT NULL DEFAULT '[]',
            resolved_manifest_json TEXT NOT NULL DEFAULT '[]',
            stdout_text TEXT,
            stderr_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_environment_builds_run
        ON agent_environment_builds(
            run_id,
            created_at
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_environment_activity (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            build_id TEXT,
            status TEXT NOT NULL DEFAULT 'idle',
            stage TEXT NOT NULL DEFAULT 'idle',
            detail TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_environment_activity_user
        ON agent_environment_activity(
            user_id,
            updated_at
        )
        """
    )

    conn.commit()
    conn.close()
    _STORAGE_READY = True


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _environment_from_row(row):
    if not row:
        return None
    return {
        "run_id": row[0],
        "user_id": row[1],
        "profile": row[2],
        "requirements_hash": row[3],
        "image_tag": row[4],
        "status": row[5],
        "requested_requirements": _json_list(row[6]),
        "resolved_manifest": _json_list(row[7]),
        "last_error": row[8],
        "build_count": int(row[9] or 0),
        "created_at": row[10],
        "updated_at": row[11],
    }


def _get_environment_row(user_id, run_id):
    initialize_agent_environment_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            run_id,
            user_id,
            profile,
            requirements_hash,
            image_tag,
            status,
            requested_requirements_json,
            resolved_manifest_json,
            last_error,
            build_count,
            created_at,
            updated_at
        FROM agent_run_environments
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )
    result = _environment_from_row(cursor.fetchone())
    conn.close()
    return result


def set_agent_run_environment_profile(user_id, run_id, profile):
    initialize_agent_environment_storage()

    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    selected = str(profile or ENV_PROFILE_STRICT).strip().lower()
    if selected not in VALID_ENV_PROFILES:
        raise AgentEnvironmentError("Unknown sandbox environment profile.")

    if (
        selected == ENV_PROFILE_PROJECT
        and not user_has_permission(user_id, AGENT_ENVIRONMENT_PERMISSION)
    ):
        raise AgentEnvironmentError(
            "This account does not have dependency-enabled project environment permission."
        )

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_run_environments (
            run_id,
            user_id,
            profile,
            requirements_hash,
            image_tag,
            status,
            requested_requirements_json,
            resolved_manifest_json,
            last_error,
            build_count,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, NULL, NULL, 'base', '[]', '[]', NULL, 0, ?, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            profile = excluded.profile,
            updated_at = excluded.updated_at
        """,
        (
            str(run_id),
            int(user_id),
            selected,
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    conn.close()

    return get_agent_run_environment(user_id, run_id)


def get_agent_run_environment(user_id, run_id):
    initialize_agent_environment_storage()
    row = _get_environment_row(user_id, run_id)
    if row:
        return row

    # Old/pre-v2.2 runs safely default to Strict without mutating their state
    # unless they are later explicitly continued with a profile change.
    return {
        "run_id": str(run_id),
        "user_id": int(user_id),
        "profile": ENV_PROFILE_STRICT,
        "requirements_hash": None,
        "image_tag": None,
        "status": "base",
        "requested_requirements": [],
        "resolved_manifest": [],
        "last_error": None,
        "build_count": 0,
        "created_at": None,
        "updated_at": None,
    }


def project_environment_allowed(user_id, run_id):
    environment = get_agent_run_environment(user_id, run_id)
    return bool(
        environment.get("profile") == ENV_PROFILE_PROJECT
        and user_has_permission(user_id, AGENT_ENVIRONMENT_PERMISSION)
    )


def sanitize_requirements(text):
    source = str(text or "")[:ENV_MAX_REQUIREMENTS_CHARS]
    result = []
    seen = set()

    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Allow an ordinary trailing comment only after whitespace.
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        compact = line.replace(" ", "")

        forbidden = (
            compact.startswith("-"),
            "@" in compact,
            "://" in compact,
            "git+" in compact.lower(),
            "file:" in compact.lower(),
            "/" in compact,
            "\\" in compact,
            ";" in compact,
        )
        if any(forbidden):
            raise AgentEnvironmentError(
                "Project requirements may contain only PyPI package names/extras and "
                "ordinary version constraints. URLs, VCS, local paths, markers and "
                "pip options are not allowed in this profile."
            )

        if not _REQUIREMENT_RE.fullmatch(compact):
            raise AgentEnvironmentError(
                f"Unsupported dependency requirement: {line[:160]}"
            )

        key = compact.lower()
        if key in seen:
            continue

        seen.add(key)
        result.append(compact)

        if len(result) > ENV_MAX_DEPENDENCIES:
            raise AgentEnvironmentError(
                f"Project environment supports at most {ENV_MAX_DEPENDENCIES} direct dependencies."
            )

    return sorted(result, key=str.lower)


def _requirements_text(user_id, run_id):
    names = {
        str(item.get("filename") or "").lower(): item.get("filename")
        for item in list_workspace_files(user_id, run_id)
    }
    actual = names.get(ENV_REQUIREMENTS_FILE)
    if not actual:
        return ""
    return read_workspace_file(
        user_id,
        run_id,
        actual,
        max_chars=ENV_MAX_REQUIREMENTS_CHARS,
    )


def current_requirements(user_id, run_id):
    return sanitize_requirements(
        _requirements_text(user_id, run_id)
    )


def _requirements_hash(requirements):
    payload = json.dumps(
        {
            "base_image": SANDBOX_IMAGE,
            "requirements": list(requirements),
            "binary_only": ENV_ONLY_BINARY,
            "schema": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _image_tag(requirements_hash):
    return f"{ENV_IMAGE_PREFIX}:{requirements_hash[:16]}"


def _docker_image_exists(image_tag):
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", str(image_tag)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def environment_status_for_run(user_id, run_id):
    environment = get_agent_run_environment(user_id, run_id)
    activity = get_environment_activity(
        user_id,
        run_id,
    )
    profile = environment.get("profile") or ENV_PROFILE_STRICT

    if profile != ENV_PROFILE_PROJECT:
        return {
            **environment,
            "activity": activity,
            "ready": True,
            "stale": False,
            "execution_image": SANDBOX_IMAGE,
            "current_requirements": [],
            "message": "Strict sandbox: base Python image, execution network disabled.",
        }

    try:
        requirements = current_requirements(user_id, run_id)
    except AgentEnvironmentError as error:
        return {
            **environment,
            "activity": activity,
            "ready": False,
            "stale": True,
            "failed_current": True,
            "status": "invalid_manifest",
            "execution_image": None,
            "current_requirements": [],
            "last_error": str(error),
            "message": (
                "requirements.txt is not valid for the controlled Project profile: "
                + str(error)
            ),
        }

    if not requirements:
        return {
            **environment,
            "activity": activity,
            "ready": True,
            "stale": False,
            "status": "base",
            "execution_image": SANDBOX_IMAGE,
            "current_requirements": [],
            "message": (
                "Project sandbox is enabled. No requirements.txt dependencies are "
                "declared, so the base Python image will be used."
            ),
        }

    current_hash = _requirements_hash(requirements)
    image_tag = _image_tag(current_hash)
    matches = (
        environment.get("requirements_hash") == current_hash
        and environment.get("image_tag") == image_tag
        and environment.get("status") in {"ready", "cached"}
        and _docker_image_exists(image_tag)
    )

    failed_current = (
        environment.get("requirements_hash") == current_hash
        and environment.get("status") == "failed"
    )

    return {
        **environment,
        "activity": activity,
        "ready": bool(matches),
        "stale": not matches,
        "failed_current": bool(failed_current),
        "execution_image": image_tag if matches else None,
        "current_requirements": requirements,
        "current_requirements_hash": current_hash,
        "current_image_tag": image_tag,
        "message": (
            "Project dependency image is ready; normal execution remains network-disabled."
            if matches
            else (
                "The current requirements.txt dependency build failed. Revise the manifest "
                "or switch to Strict mode."
                if failed_current
                else "Project dependencies require an isolated setup/build step."
            )
        ),
    }


def environment_needs_setup(user_id, run_id):
    if not project_environment_allowed(user_id, run_id):
        return False
    status = environment_status_for_run(user_id, run_id)
    return bool(
        status.get("current_requirements")
        and not status.get("ready")
        and not status.get("failed_current")
    )


def resolve_execution_image(user_id, run_id):
    status = environment_status_for_run(user_id, run_id)
    if status.get("ready"):
        return status.get("execution_image") or SANDBOX_IMAGE
    raise AgentEnvironmentNotReady(
        status.get("message")
        or "Project dependencies are not ready."
    )


def _declared_requirement_names(user_id, run_id):
    try:
        requirements = current_requirements(
            user_id,
            run_id,
        )
    except AgentEnvironmentError:
        return set()

    names = set()

    for requirement in requirements:
        name = re.split(
            r"[<>=!~\[]",
            requirement,
            maxsplit=1,
        )[0].strip().lower()

        if name:
            names.add(name)

    return names


def static_workspace_dependencies(user_id, run_id):
    """
    Discover likely third-party imports directly from current Python source.

    This lets Project mode react immediately after a file such as app.py is
    created, instead of wasting a sandbox execution just to learn that Flask,
    pandas, etc. are absent.

    The scan is deterministic and local:
    - Python AST only
    - local workspace modules excluded
    - Python stdlib excluded
    - known import->distribution mappings applied
    - unknown safe imports fall back to normalized package names

    It does NOT contact PyPI.
    """
    files = list_workspace_files(
        user_id,
        run_id,
    )

    python_files = [
        item
        for item in files
        if str(
            item.get("filename")
            or ""
        ).lower().endswith(".py")
    ]

    local_modules = {
        Path(
            str(
                item.get("filename")
                or ""
            )
        ).stem.lower()
        for item in python_files
    }

    stdlib = {
        str(name).lower()
        for name in getattr(
            sys,
            "stdlib_module_names",
            set(),
        )
    }

    discovered = {}

    for item in python_files:
        filename = str(
            item.get("filename")
            or ""
        )

        try:
            source = read_workspace_file(
                user_id,
                run_id,
                filename,
                max_chars=120000,
            )
            tree = ast.parse(
                source,
                filename=filename,
            )
        except Exception:
            continue

        modules = set()

        for node in ast.walk(tree):
            if isinstance(
                node,
                ast.Import,
            ):
                for alias in node.names:
                    modules.add(
                        str(
                            alias.name
                            or ""
                        ).split(".")[0]
                    )

            elif isinstance(
                node,
                ast.ImportFrom,
            ):
                # Relative imports are local project imports.
                if int(
                    getattr(
                        node,
                        "level",
                        0,
                    )
                    or 0
                ) > 0:
                    continue

                module = str(
                    node.module
                    or ""
                ).split(".")[0]

                if module:
                    modules.add(
                        module
                    )

        for module in modules:
            normalized = module.strip().lower()

            if (
                not normalized
                or normalized in local_modules
                or normalized in stdlib
            ):
                continue

            package = _IMPORT_PACKAGE_MAP.get(
                normalized,
                normalized.replace(
                    "_",
                    "-",
                ),
            )

            discovered.setdefault(
                package.lower(),
                {
                    "module": module,
                    "package": package,
                    "kind": "third_party",
                    "source": "static_import",
                    "filename": filename,
                },
            )

    return list(
        discovered.values()
    )


def undeclared_static_dependency_for_run(
    user_id,
    run_id,
):
    if not project_environment_allowed(
        user_id,
        run_id,
    ):
        return None

    declared = _declared_requirement_names(
        user_id,
        run_id,
    )

    for dependency in static_workspace_dependencies(
        user_id,
        run_id,
    ):
        package = str(
            dependency.get(
                "package"
            )
            or ""
        ).lower()

        if (
            package
            and package not in declared
        ):
            return dependency

    return None


def missing_dependency_for_run(user_id, run_id):
    executions = list_agent_sandbox_executions(
        user_id,
        run_id,
        limit=8,
    )
    if not executions:
        return None

    stderr = str(
        executions[-1].get("stderr")
        or ""
    )

    match = re.search(
        r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]",
        stderr,
    )
    if not match:
        return None

    module = match.group(1).split(".")[0].strip()
    if not module:
        return None

    local_modules = {
        Path(str(item.get("filename") or "")).stem.lower()
        for item in list_workspace_files(user_id, run_id)
        if str(item.get("filename") or "").lower().endswith(".py")
    }
    if module.lower() in local_modules:
        return None

    stdlib = getattr(sys, "stdlib_module_names", set())
    if module in stdlib or module.lower() in {str(x).lower() for x in stdlib}:
        return {
            "module": module,
            "package": None,
            "kind": "stdlib_missing_from_image",
        }

    package = _IMPORT_PACKAGE_MAP.get(
        module.lower(),
        module.replace("_", "-"),
    )

    return {
        "module": module,
        "package": package,
        "kind": "third_party",
    }


def dependency_manifest_needs_update(user_id, run_id):
    if not project_environment_allowed(user_id, run_id):
        return False

    # Prefer deterministic static discovery so Project mode can declare Flask,
    # pandas, etc. immediately after source creation and before the first failed
    # sandbox import.
    static_missing = undeclared_static_dependency_for_run(
        user_id,
        run_id,
    )

    if static_missing:
        return True

    missing = missing_dependency_for_run(
        user_id,
        run_id,
    )

    if not missing or missing.get("kind") != "third_party":
        return False

    package = str(
        missing.get("package")
        or ""
    ).lower()

    if not package:
        return False

    declared = _declared_requirement_names(
        user_id,
        run_id,
    )

    return package not in declared


def add_missing_dependency_to_manifest(user_id, run_id):
    if not project_environment_allowed(user_id, run_id):
        raise AgentEnvironmentError(
            "Dependency planning requires the Project sandbox profile."
        )

    # Static source analysis is preferred. A failed sandbox import remains a
    # fallback for dynamic imports or cases the AST scan cannot see.
    missing = undeclared_static_dependency_for_run(
        user_id,
        run_id,
    ) or missing_dependency_for_run(
        user_id,
        run_id,
    )

    if not missing:
        raise AgentEnvironmentError(
            "No undeclared third-party dependency was detected in the current "
            "workspace or latest sandbox failure."
        )

    if missing.get("kind") != "third_party":
        raise AgentEnvironmentError(
            f"{missing.get('module')} appears to be a standard-library/runtime image limitation, "
            "not a PyPI dependency."
        )

    package = str(missing.get("package") or "").strip()
    if not package:
        raise AgentEnvironmentError(
            "ATLAS could not infer a safe PyPI package name for the missing import."
        )

    existing = current_requirements(user_id, run_id)
    merged = sanitize_requirements(
        "\n".join(existing + [package])
    )

    content = (
        "# ATLAS project dependencies\n"
        "# Dependency setup is isolated; normal execution remains network-disabled.\n"
        + "\n".join(merged)
        + "\n"
    )

    result = write_workspace_file(
        user_id,
        run_id,
        ENV_REQUIREMENTS_FILE,
        content,
    )

    return {
        "module": missing["module"],
        "package": package,
        "source": missing.get("source") or "sandbox_failure",
        "detected_in": missing.get("filename"),
        "requirements": merged,
        "file": result,
    }



def _set_environment_activity(
    user_id,
    run_id,
    *,
    build_id=None,
    status,
    stage,
    detail=None,
    progress=0,
    started=False,
    finished=False,
):
    """
    Persist a small heartbeat/progress record that the Agent UI can poll while
    Docker is doing long-running dependency work.

    This is intentionally separate from final build history:
    - activity = live/ephemeral status for one run
    - agent_environment_builds = durable completed build provenance
    """
    initialize_agent_environment_storage()

    timestamp = utc_iso()
    progress_value = max(
        0,
        min(
            100,
            int(
                progress
                or 0
            ),
        ),
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO agent_environment_activity (
            run_id,
            user_id,
            build_id,
            status,
            stage,
            detail,
            progress,
            started_at,
            updated_at,
            finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            build_id = excluded.build_id,
            status = excluded.status,
            stage = excluded.stage,
            detail = excluded.detail,
            progress = excluded.progress,
            started_at = CASE
                WHEN ? THEN excluded.started_at
                ELSE agent_environment_activity.started_at
            END,
            updated_at = excluded.updated_at,
            finished_at = CASE
                WHEN ? THEN excluded.finished_at
                ELSE NULL
            END
        """,
        (
            str(run_id),
            int(user_id),
            str(build_id or "") or None,
            str(status or "idle")[:40],
            str(stage or "idle")[:80],
            str(detail or "")[:1200] or None,
            progress_value,
            timestamp if started else None,
            timestamp,
            timestamp if finished else None,
            int(bool(started)),
            int(bool(finished)),
        ),
    )

    conn.commit()
    conn.close()


def get_environment_activity(user_id, run_id):
    initialize_agent_environment_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            run_id,
            build_id,
            status,
            stage,
            detail,
            progress,
            started_at,
            updated_at,
            finished_at
        FROM agent_environment_activity
        WHERE
            run_id = ?
            AND user_id = ?
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )

    row = cursor.fetchone()
    conn.close()

    if not row:
        return {
            "run_id": str(run_id),
            "build_id": None,
            "status": "idle",
            "stage": "idle",
            "detail": None,
            "progress": 0,
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
        }

    return {
        "run_id": row[0],
        "build_id": row[1],
        "status": row[2],
        "stage": row[3],
        "detail": row[4],
        "progress": int(row[5] or 0),
        "started_at": row[6],
        "updated_at": row[7],
        "finished_at": row[8],
    }


def _classify_build_progress(line, current_progress=25):
    """
    Translate Docker/pip build text into stable user-facing phases.

    Docker output varies by version/BuildKit, so these are deliberately broad
    hints rather than a fake exact percentage.
    """
    text = str(
        line
        or ""
    ).strip()

    lowered = text.lower()

    if not lowered:
        return (
            "building",
            "Building isolated dependency image…",
            max(
                25,
                int(
                    current_progress
                    or 25
                ),
            ),
        )

    rules = (
        (
            (
                "load build definition",
                "load metadata",
            ),
            "preparing",
            "Preparing dependency build context…",
            20,
        ),
        (
            (
                "load build context",
                "transferring context",
            ),
            "preparing",
            "Preparing sanitized requirements context…",
            30,
        ),
        (
            (
                "collecting ",
                "obtaining dependency information",
            ),
            "downloading",
            "Resolving and downloading dependency wheels…",
            48,
        ),
        (
            (
                "downloading ",
                "using cached ",
            ),
            "downloading",
            "Downloading/reusing dependency wheels…",
            58,
        ),
        (
            (
                "installing collected packages",
                "installing ",
            ),
            "installing",
            "Installing dependencies into the isolated image…",
            72,
        ),
        (
            (
                "successfully installed",
            ),
            "installing",
            "Dependencies installed; finalizing environment…",
            84,
        ),
        (
            (
                "exporting to image",
                "exporting layers",
                "writing image",
            ),
            "finalizing",
            "Exporting the reusable project environment…",
            91,
        ),
        (
            (
                "naming to ",
            ),
            "finalizing",
            "Saving the content-addressed environment image…",
            96,
        ),
    )

    for markers, stage, detail, progress in rules:
        if any(
            marker in lowered
            for marker in markers
        ):
            return (
                stage,
                detail,
                max(
                    int(
                        current_progress
                        or 0
                    ),
                    progress,
                ),
            )

    return (
        "building",
        text[-500:],
        max(
            25,
            int(
                current_progress
                or 25
            ),
        ),
    )


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


def _run_cancellable(
    command,
    timeout,
    cancel_check=None,
    progress_callback=None,
):
    """
    Run a child process while:
    - streaming stdout/stderr into bounded in-memory buffers
    - emitting progress/heartbeat callbacks
    - polling Agent cancellation
    - enforcing a hard setup timeout

    Reading pipes continuously also prevents a verbose docker build from
    blocking because an OS pipe buffer filled before communicate().
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=_docker_env(),
    )

    stdout_lines = []
    stderr_lines = []
    output_lock = threading.Lock()

    def _reader(stream, sink):
        try:
            for line in iter(
                stream.readline,
                "",
            ):
                with output_lock:
                    sink.append(
                        line
                    )

                    # Keep memory bounded while retaining the useful tail.
                    if len(
                        sink
                    ) > 4000:
                        del sink[
                            :2000
                        ]
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threads = [
        threading.Thread(
            target=_reader,
            args=(
                process.stdout,
                stdout_lines,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_reader,
            args=(
                process.stderr,
                stderr_lines,
            ),
            daemon=True,
        ),
    ]

    for thread in threads:
        thread.start()

    started = time.monotonic()
    deadline = (
        started
        + max(
            10,
            int(
                timeout
            ),
        )
    )

    timed_out = False
    last_progress_text = None
    last_heartbeat = 0.0

    try:
        while process.poll() is None:
            if cancel_check:
                cancel_check()

            now_mono = time.monotonic()

            if progress_callback:
                with output_lock:
                    latest = (
                        stderr_lines[-1]
                        if stderr_lines
                        else (
                            stdout_lines[-1]
                            if stdout_lines
                            else ""
                        )
                    )

                latest = str(
                    latest
                    or ""
                ).strip()

                if (
                    latest
                    and latest
                    != last_progress_text
                ):
                    progress_callback(
                        latest,
                        False,
                    )
                    last_progress_text = latest
                    last_heartbeat = now_mono

                elif (
                    now_mono
                    - last_heartbeat
                    >= 1.5
                ):
                    progress_callback(
                        None,
                        True,
                    )
                    last_heartbeat = now_mono

            if now_mono >= deadline:
                timed_out = True
                try:
                    process.terminate()
                except OSError:
                    pass

                try:
                    process.wait(
                        timeout=3
                    )
                except Exception:
                    try:
                        process.kill()
                    except OSError:
                        pass

                break

            time.sleep(
                0.25
            )

    except Exception:
        try:
            process.terminate()
        except OSError:
            pass

        try:
            process.wait(
                timeout=2
            )
        except Exception:
            try:
                process.kill()
            except OSError:
                pass

        raise

    finally:
        for thread in threads:
            thread.join(
                timeout=2
            )

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass

    try:
        process.wait(
            timeout=3
        )
    except Exception:
        pass

    with output_lock:
        stdout = "".join(
            stdout_lines
        )
        stderr = "".join(
            stderr_lines
        )

    return {
        "returncode":
            process.returncode,
        "stdout":
            stdout or "",
        "stderr":
            stderr or "",
        "timed_out":
            timed_out,
        "duration_ms":
            int(
                (
                    time.monotonic()
                    - started
                )
                * 1000
            ),
    }


def _freeze_manifest(image_tag):
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", "none",
                "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
                image_tag,
                "python", "-m", "pip", "freeze", "--all",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_docker_env(),
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ][:400]


def _record_build(
    user_id,
    run_id,
    build_id,
    requirements_hash,
    image_tag,
    status,
    cached,
    duration_ms,
    requested,
    resolved,
    stdout_text,
    stderr_text,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_environment_builds (
            id,
            run_id,
            user_id,
            requirements_hash,
            base_image,
            image_tag,
            status,
            cached,
            duration_ms,
            requested_requirements_json,
            resolved_manifest_json,
            stdout_text,
            stderr_text,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            build_id,
            str(run_id),
            int(user_id),
            requirements_hash,
            SANDBOX_IMAGE,
            image_tag,
            status,
            int(bool(cached)),
            int(duration_ms),
            json.dumps(requested, ensure_ascii=False),
            json.dumps(resolved, ensure_ascii=False),
            str(stdout_text or "")[-ENV_MAX_BUILD_LOG_CHARS:],
            str(stderr_text or "")[-ENV_MAX_BUILD_LOG_CHARS:],
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()


def _update_environment_state(
    user_id,
    run_id,
    requirements_hash,
    image_tag,
    status,
    requested,
    resolved=None,
    last_error=None,
    increment_build=False,
):
    initialize_agent_environment_storage()
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_run_environments (
            run_id,
            user_id,
            profile,
            requirements_hash,
            image_tag,
            status,
            requested_requirements_json,
            resolved_manifest_json,
            last_error,
            build_count,
            created_at,
            updated_at
        )
        VALUES (?, ?, 'project', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            profile = 'project',
            requirements_hash = excluded.requirements_hash,
            image_tag = excluded.image_tag,
            status = excluded.status,
            requested_requirements_json = excluded.requested_requirements_json,
            resolved_manifest_json = excluded.resolved_manifest_json,
            last_error = excluded.last_error,
            build_count = agent_run_environments.build_count + ?,
            updated_at = excluded.updated_at
        """,
        (
            str(run_id),
            int(user_id),
            requirements_hash,
            image_tag,
            status,
            json.dumps(requested, ensure_ascii=False),
            json.dumps(resolved or [], ensure_ascii=False),
            str(last_error or "")[:6000] or None,
            int(bool(increment_build)),
            timestamp,
            timestamp,
            int(bool(increment_build)),
        ),
    )
    conn.commit()
    conn.close()


def setup_project_environment(user_id, run_id, cancel_check=None):
    initialize_agent_environment_storage()

    if not project_environment_allowed(user_id, run_id):
        raise AgentEnvironmentError(
            "This run is not using the dependency-enabled Project sandbox profile."
        )

    base_status = sandbox_status(force=True)
    if not base_status.get("ready"):
        raise AgentEnvironmentError(
            base_status.get("message")
            or "Docker sandbox is unavailable."
        )

    _set_environment_activity(
        user_id,
        run_id,
        status="running",
        stage="validating",
        detail="Validating the sanitized dependency manifest…",
        progress=5,
        started=True,
    )

    requirements = current_requirements(user_id, run_id)

    _set_environment_activity(
        user_id,
        run_id,
        status="running",
        stage="cache_check",
        detail="Checking the local content-addressed environment cache…",
        progress=12,
    )

    if not requirements:
        _update_environment_state(
            user_id,
            run_id,
            None,
            None,
            "base",
            [],
            resolved=[],
            last_error=None,
        )
        _set_environment_activity(
            user_id,
            run_id,
            status="ready",
            stage="base",
            detail="No third-party dependencies were declared; using the base Python image.",
            progress=100,
            finished=True,
        )

        return {
            "status": "base",
            "cached": True,
            "image": SANDBOX_IMAGE,
            "requested": [],
            "resolved": [],
            "duration_ms": 0,
        }

    requirements_hash = _requirements_hash(requirements)
    image_tag = _image_tag(requirements_hash)
    build_id = uuid.uuid4().hex

    _set_environment_activity(
        user_id,
        run_id,
        build_id=build_id,
        status="running",
        stage="cache_check",
        detail="Checking whether this exact dependency image already exists locally…",
        progress=16,
    )

    if _docker_image_exists(image_tag):
        resolved = _freeze_manifest(image_tag)
        _update_environment_state(
            user_id,
            run_id,
            requirements_hash,
            image_tag,
            "cached",
            requirements,
            resolved=resolved,
            last_error=None,
        )
        _record_build(
            user_id,
            run_id,
            build_id,
            requirements_hash,
            image_tag,
            "cached",
            True,
            0,
            requirements,
            resolved,
            "Dependency image already existed in the local Docker cache.",
            "",
        )
        _set_environment_activity(
            user_id,
            run_id,
            build_id=build_id,
            status="cached",
            stage="cache_hit",
            detail="Reused the existing content-addressed project environment.",
            progress=100,
            finished=True,
        )

        return {
            "status": "cached",
            "cached": True,
            "image": image_tag,
            "requested": requirements,
            "resolved": resolved,
            "duration_ms": 0,
        }

    _update_environment_state(
        user_id,
        run_id,
        requirements_hash,
        image_tag,
        "building",
        requirements,
        resolved=[],
        last_error=None,
        increment_build=True,
    )

    _set_environment_activity(
        user_id,
        run_id,
        build_id=build_id,
        status="running",
        stage="building",
        detail="Starting isolated dependency image build…",
        progress=22,
    )

    pip_binary_flag = " --only-binary=:all:" if ENV_ONLY_BINARY else ""

    dockerfile = (
        f"FROM {SANDBOX_IMAGE}\n"
        "ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
        "PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
        "USER root\n"
        "COPY requirements.txt /tmp/atlas-requirements.txt\n"
        "RUN python -m pip install --no-cache-dir"
        + pip_binary_flag
        + " -r /tmp/atlas-requirements.txt "
        "&& rm -f /tmp/atlas-requirements.txt\n"
        "USER 65534:65534\n"
        "WORKDIR /runtime\n"
    )

    result = None
    with tempfile.TemporaryDirectory(prefix="atlas-env-build-") as temp_dir:
        root = Path(temp_dir)
        (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (root / "requirements.txt").write_text(
            "\n".join(requirements) + "\n",
            encoding="utf-8",
        )

        command = [
            "docker", "build",
            "--progress=plain",
            "--network", "default",
            "--pull=false",
            "--tag", image_tag,
            "--label", "com.begloo.atlas.environment=python-project",
            "--label", f"com.begloo.atlas.requirements={requirements_hash[:16]}",
            str(root),
        ]

        current_progress = {
            "value": 22,
            "stage": "building",
            "detail": "Building isolated dependency image…",
        }

        def _progress_callback(
            line,
            heartbeat,
        ):
            if heartbeat:
                _set_environment_activity(
                    user_id,
                    run_id,
                    build_id=build_id,
                    status="running",
                    stage=current_progress["stage"],
                    detail=current_progress["detail"],
                    progress=current_progress["value"],
                )
                return

            stage, detail, progress = _classify_build_progress(
                line,
                current_progress["value"],
            )

            current_progress.update({
                "value":
                    progress,
                "stage":
                    stage,
                "detail":
                    detail,
            })

            _set_environment_activity(
                user_id,
                run_id,
                build_id=build_id,
                status="running",
                stage=stage,
                detail=detail,
                progress=progress,
            )

        try:
            result = _run_cancellable(
                command,
                ENV_SETUP_TIMEOUT_SECONDS,
                cancel_check=cancel_check,
                progress_callback=_progress_callback,
            )
        except Exception:
            _set_environment_activity(
                user_id,
                run_id,
                build_id=build_id,
                status="stopped",
                stage="stopped",
                detail="Dependency setup was stopped before completion.",
                progress=current_progress["value"],
                finished=True,
            )
            raise

    build_status = (
        "timeout"
        if result.get("timed_out")
        else (
            "ready"
            if result.get("returncode") == 0
            else "failed"
        )
    )

    resolved = []
    if build_status == "ready":
        _set_environment_activity(
            user_id,
            run_id,
            build_id=build_id,
            status="running",
            stage="resolving",
            detail="Recording resolved dependency versions…",
            progress=97,
        )
        resolved = _freeze_manifest(image_tag)

    error_text = str(result.get("stderr") or "")[-6000:]
    if build_status == "timeout":
        error_text = (
            "Dependency setup exceeded the configured timeout. "
            + error_text
        ).strip()

    _update_environment_state(
        user_id,
        run_id,
        requirements_hash,
        image_tag,
        build_status,
        requirements,
        resolved=resolved,
        last_error=(error_text if build_status != "ready" else None),
    )

    _record_build(
        user_id,
        run_id,
        build_id,
        requirements_hash,
        image_tag,
        build_status,
        False,
        result.get("duration_ms") or 0,
        requirements,
        resolved,
        result.get("stdout") or "",
        result.get("stderr") or "",
    )

    _set_environment_activity(
        user_id,
        run_id,
        build_id=build_id,
        status=(
            "ready"
            if build_status == "ready"
            else build_status
        ),
        stage=(
            "ready"
            if build_status == "ready"
            else build_status
        ),
        detail=(
            "Project environment is ready; normal execution remains network-disabled."
            if build_status == "ready"
            else (
                "Dependency setup timed out."
                if build_status == "timeout"
                else "Dependency setup failed."
            )
        ),
        progress=(
            100
            if build_status == "ready"
            else max(
                1,
                int(
                    current_progress["value"]
                ),
            )
        ),
        finished=True,
    )

    if build_status != "ready":
        suffix = (
            " Project mode accepts binary wheels only by default; source-only packages "
            "require a future Advanced environment profile."
            if ENV_ONLY_BINARY
            else ""
        )
        raise AgentEnvironmentError(
            "Project dependency setup failed. "
            + (error_text[-2200:] or "Docker build failed.")
            + suffix
        )

    return {
        "status": "ready",
        "cached": False,
        "image": image_tag,
        "requested": requirements,
        "resolved": resolved,
        "duration_ms": result.get("duration_ms") or 0,
    }


def list_environment_builds(user_id, run_id, limit=20):
    initialize_agent_environment_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            id,
            requirements_hash,
            base_image,
            image_tag,
            status,
            cached,
            duration_ms,
            requested_requirements_json,
            resolved_manifest_json,
            stdout_text,
            stderr_text,
            created_at
        FROM agent_environment_builds
        WHERE run_id = ? AND user_id = ?
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (
            str(run_id),
            int(user_id),
            max(1, min(100, int(limit))),
        ),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": row[0],
            "requirements_hash": row[1],
            "base_image": row[2],
            "image_tag": row[3],
            "status": row[4],
            "cached": bool(row[5]),
            "duration_ms": int(row[6] or 0),
            "requested": _json_list(row[7]),
            "resolved": _json_list(row[8]),
            "stdout": row[9],
            "stderr": row[10],
            "created_at": row[11],
        }
        for row in rows
    ]


def format_environment_observation(result):
    requested = list(result.get("requested") or [])
    resolved = list(result.get("resolved") or [])
    lines = [
        "ATLAS Project Environment",
        f"Status: {str(result.get('status') or 'unknown').upper()}",
        f"Image: {result.get('image') or SANDBOX_IMAGE}",
        (
            "Network policy: dependency setup may access the package index; "
            "normal project execution remains network-disabled."
        ),
    ]

    if requested:
        lines.append("Requested dependencies: " + ", ".join(requested))
    if resolved:
        lines.append(
            "Resolved environment: "
            + ", ".join(resolved[:24])
            + (" ..." if len(resolved) > 24 else "")
        )
    if result.get("cached"):
        lines.append("Cache: reused an existing content-addressed dependency image.")
    elif result.get("duration_ms") is not None:
        lines.append(f"Setup duration: {int(result.get('duration_ms') or 0)} ms")

    return "\n".join(lines)
