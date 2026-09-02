"""
ATLAS v2.3 - multi-runtime Agent execution foundation.

Runtime selection is intentionally separate from sandbox profile:

    runtime:   auto | python | node
    profile:   strict | project

Examples:
    Python + Strict   -> python:3.11-slim, no dependency download
    Python + Project  -> isolated pip environment
    Node + Strict     -> node:22-slim, no dependency download
    Node + Project    -> isolated npm environment

This module only selects/describes runtimes. Actual execution remains in
agent_sandbox.py and dependency setup remains runtime-specific.
"""

import os
import re
import shutil
import subprocess

from app.database import get_connection
from app.services.agents import (
    AgentStoreError,
    get_agent_run,
    utc_iso,
)


RUNTIME_AUTO = "auto"
RUNTIME_PYTHON = "python"
RUNTIME_NODE = "node"
VALID_RUNTIMES = {
    RUNTIME_AUTO,
    RUNTIME_PYTHON,
    RUNTIME_NODE,
}

PYTHON_BASE_IMAGE = os.environ.get(
    "PRIVATE_AI_AGENT_SANDBOX_IMAGE",
    "python:3.11-slim",
).strip() or "python:3.11-slim"

NODE_BASE_IMAGE = os.environ.get(
    "PRIVATE_AI_AGENT_NODE_IMAGE",
    "node:22-slim",
).strip() or "node:22-slim"

_STORAGE_READY = False

_NODE_GOAL_RE = re.compile(
    r"\b(?:node(?:\.js)?|npm|javascript|typescript|react|vite|vue|express|"
    r"frontend|front-end|browser app|web game|javascript game|js app|js game)\b",
    re.IGNORECASE,
)

_PYTHON_GOAL_RE = re.compile(
    r"\b(?:python|flask|fastapi|django|pytest|pandas|numpy|pip|requirements\.txt)\b",
    re.IGNORECASE,
)

_NODE_SOURCE_SUFFIXES = {
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
}


def initialize_agent_runtime_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_runtimes (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            selected_runtime TEXT NOT NULL DEFAULT 'auto',
            detected_runtime TEXT,
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

    conn.commit()
    conn.close()

    _STORAGE_READY = True


def _normalize_runtime(value):
    runtime = str(
        value
        or RUNTIME_AUTO
    ).strip().lower()

    aliases = {
        "js": RUNTIME_NODE,
        "javascript": RUNTIME_NODE,
        "nodejs": RUNTIME_NODE,
        "node.js": RUNTIME_NODE,
        "py": RUNTIME_PYTHON,
    }
    runtime = aliases.get(
        runtime,
        runtime,
    )

    if runtime not in VALID_RUNTIMES:
        raise AgentStoreError(
            "Unknown Agent sandbox runtime."
        )

    return runtime


def set_agent_run_runtime(
    user_id,
    run_id,
    runtime,
):
    initialize_agent_runtime_storage()

    run = get_agent_run(
        user_id,
        run_id,
    )
    if not run:
        raise AgentStoreError(
            "Agent run was not found."
        )

    selected = _normalize_runtime(
        runtime
    )
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO agent_run_runtimes (
            run_id,
            user_id,
            selected_runtime,
            detected_runtime,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, NULL, ?, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            selected_runtime = excluded.selected_runtime,
            detected_runtime = NULL,
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

    return get_agent_run_runtime(
        user_id,
        run_id,
    )


def _stored_runtime(
    user_id,
    run_id,
):
    initialize_agent_runtime_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            selected_runtime,
            detected_runtime,
            created_at,
            updated_at
        FROM agent_run_runtimes
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
            "selected_runtime": RUNTIME_AUTO,
            "detected_runtime": None,
            "created_at": None,
            "updated_at": None,
        }

    return {
        "selected_runtime": _normalize_runtime(
            row[0]
        ),
        "detected_runtime": (
            str(row[1]).strip().lower()
            if row[1]
            else None
        ),
        "created_at": row[2],
        "updated_at": row[3],
    }


def _workspace_runtime_signals(
    user_id,
    run_id,
):
    # Lazy import avoids a runtime<->sandbox import cycle.
    from app.services.agent_sandbox import (
        list_workspace_files,
    )

    files = list_workspace_files(
        user_id,
        run_id,
    )

    names = [
        str(
            item.get(
                "filename"
            )
            or ""
        ).strip()
        for item in files
    ]

    lowers = {
        name.lower()
        for name in names
        if name
    }

    node_score = 0
    python_score = 0

    if "package.json" in lowers:
        node_score += 8
    if "package-lock.json" in lowers:
        node_score += 3
    if "requirements.txt" in lowers:
        python_score += 8
    if "pyproject.toml" in lowers:
        python_score += 8

    for name in names:
        lower = name.lower()
        if lower.endswith(".py"):
            python_score += 2
        if any(
            lower.endswith(suffix)
            for suffix in _NODE_SOURCE_SUFFIXES
        ):
            node_score += 2

    return {
        "node": node_score,
        "python": python_score,
        "files": names,
    }


def detect_agent_runtime(
    user_id,
    run_id,
):
    run = get_agent_run(
        user_id,
        run_id,
    )
    if not run:
        raise AgentStoreError(
            "Agent run was not found."
        )

    signals = _workspace_runtime_signals(
        user_id,
        run_id,
    )

    goal = str(
        run.get(
            "goal"
        )
        or ""
    )

    node_goal = bool(
        _NODE_GOAL_RE.search(
            goal
        )
    )
    python_goal = bool(
        _PYTHON_GOAL_RE.search(
            goal
        )
    )

    # Explicit package manifests are strongest because they describe the
    # actual workspace, not merely the wording of the original request.
    if (
        signals["node"] >= 8
        and signals["python"] < 8
    ):
        detected = RUNTIME_NODE
    elif (
        signals["python"] >= 8
        and signals["node"] < 8
    ):
        detected = RUNTIME_PYTHON
    elif node_goal and not python_goal:
        detected = RUNTIME_NODE
    elif python_goal and not node_goal:
        detected = RUNTIME_PYTHON
    elif signals["node"] > signals["python"]:
        detected = RUNTIME_NODE
    else:
        # Backward-compatible default for old Auto runs.
        detected = RUNTIME_PYTHON

    stored = _stored_runtime(
        user_id,
        run_id,
    )

    if (
        stored.get(
            "selected_runtime"
        ) == RUNTIME_AUTO
        and stored.get(
            "detected_runtime"
        ) != detected
    ):
        timestamp = utc_iso()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_run_runtimes (
                run_id,
                user_id,
                selected_runtime,
                detected_runtime,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'auto', ?, ?, ?)
            ON CONFLICT(run_id)
            DO UPDATE SET
                detected_runtime = excluded.detected_runtime,
                updated_at = excluded.updated_at
            """,
            (
                str(run_id),
                int(user_id),
                detected,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        conn.close()

    return detected


def get_agent_run_runtime(
    user_id,
    run_id,
):
    stored = _stored_runtime(
        user_id,
        run_id,
    )

    selected = stored[
        "selected_runtime"
    ]

    if selected == RUNTIME_AUTO:
        effective = detect_agent_runtime(
            user_id,
            run_id,
        )
        detected = effective
    else:
        effective = selected
        detected = stored.get(
            "detected_runtime"
        )

    return {
        **stored,
        "selected_runtime": selected,
        "effective_runtime": effective,
        "detected_runtime": detected,
        "label": runtime_label(
            effective
        ),
        "base_image": runtime_base_image(
            effective
        ),
    }


def effective_runtime(
    run,
):
    if not run:
        return RUNTIME_PYTHON

    return get_agent_run_runtime(
        run["user_id"],
        run["id"],
    )[
        "effective_runtime"
    ]


def runtime_label(runtime):
    runtime = _normalize_runtime(
        runtime
    )

    return {
        RUNTIME_AUTO: "Auto",
        RUNTIME_PYTHON: "Python",
        RUNTIME_NODE: "Node.js",
    }[
        runtime
    ]


def runtime_base_image(runtime):
    runtime = _normalize_runtime(
        runtime
    )

    if runtime == RUNTIME_NODE:
        return NODE_BASE_IMAGE

    # Auto cannot execute directly; its effective runtime is resolved first.
    return PYTHON_BASE_IMAGE


def _docker_image_exists(image):
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                str(image),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        return False

    return result.returncode == 0


def docker_engine_status():
    if not shutil.which(
        "docker"
    ):
        return {
            "ready": False,
            "docker_cli": False,
            "docker_daemon": False,
            "message": "Docker CLI was not found.",
        }

    try:
        result = subprocess.run(
            [
                "docker",
                "info",
                "--format",
                "{{.ServerVersion}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        return {
            "ready": False,
            "docker_cli": True,
            "docker_daemon": False,
            "message": str(
                error
            ),
        }

    ready = (
        result.returncode
        == 0
    )

    return {
        "ready": ready,
        "docker_cli": True,
        "docker_daemon": ready,
        "server_version": (
            result.stdout.strip()
            if ready
            else None
        ),
        "message": (
            "Docker sandbox engine is ready."
            if ready
            else (
                result.stderr.strip()
                or "Docker Desktop is not running."
            )
        ),
    }


def runtime_status(runtime):
    runtime = _normalize_runtime(
        runtime
    )
    if runtime == RUNTIME_AUTO:
        raise AgentStoreError(
            "Auto does not have a single base image."
        )

    engine = docker_engine_status()
    image = runtime_base_image(
        runtime
    )
    image_ready = bool(
        engine.get(
            "ready"
        )
        and _docker_image_exists(
            image
        )
    )

    return {
        "id": runtime,
        "label": runtime_label(
            runtime
        ),
        "image": image,
        "docker_ready": bool(
            engine.get(
                "ready"
            )
        ),
        "image_ready": image_ready,
        "ready": bool(
            engine.get(
                "ready"
            )
            and image_ready
        ),
        "pull_command": (
            None
            if image_ready
            else f"docker pull {image}"
        ),
        "message": (
            f"{runtime_label(runtime)} runtime image is ready."
            if image_ready
            else (
                f"{runtime_label(runtime)} runtime image is not installed. "
                f"Run: docker pull {image}"
                if engine.get(
                    "ready"
                )
                else engine.get(
                    "message"
                )
            )
        ),
    }


def runtime_catalog_status():
    return [
        {
            "id": RUNTIME_AUTO,
            "label": "Auto",
            "description": (
                "Detect Python or Node.js from the goal and workspace files."
            ),
            "ready": True,
        },
        {
            **runtime_status(
                RUNTIME_PYTHON
            ),
            "description": (
                "Python 3 sandbox with optional controlled pip project environment."
            ),
        },
        {
            **runtime_status(
                RUNTIME_NODE
            ),
            "description": (
                "Node.js sandbox with optional controlled npm project environment."
            ),
        },
    ]
