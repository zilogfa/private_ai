import json
import mimetypes
import shutil
import uuid

from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

import app.config as config

from app.database import get_connection


AGENT_PERMISSION = "agent.use"
VALID_AGENT_STATES = {
    "queued",
    "running",
    "pausing",
    "paused",
    "waiting_input",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
}
VALID_EVIDENCE_STATES = {
    "confirmed",
    "likely",
    "unverified",
    "conflicting",
    "rejected",
}
VALID_MODEL_MODES = {
    "auto",
    "fast",
    "default",
    "deep",
}

ALLOWED_ARTIFACT_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".py": "text/x-python",
}


class AgentStoreError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(value=None):
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _safe_json_load(value, default=None):
    if default is None:
        default = {}

    if isinstance(value, dict):
        return dict(value)

    if isinstance(value, list):
        return list(value)

    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default.copy() if hasattr(default, "copy") else default

    return parsed


def _json(value):
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
    )


def _workspace_root():
    root = getattr(
        config,
        "AGENT_WORKSPACE_DIR",
        config.GENERATED_DIR / "agent_workspaces",
    )
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _run_workspace(user_id, run_id, create=True):
    root = _workspace_root()
    candidate = (
        root
        / f"user_{int(user_id)}"
        / str(run_id)
    ).resolve()

    if candidate != root and root not in candidate.parents:
        raise AgentStoreError("Invalid agent workspace path.")

    if create:
        for name in ("files", "artifacts", "logs"):
            (candidate / name).mkdir(parents=True, exist_ok=True)

    return candidate


def _relative_workspace(user_id, run_id):
    path = _run_workspace(user_id, run_id, create=True)
    return str(path.relative_to(config.GENERATED_DIR.resolve()))


def initialize_agent_storage():
    """Create only additive agent tables, permission, and indexes."""

    _workspace_root()

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
            AGENT_PERMISSION,
            "Create and manage personal iterative agent runs.",
            timestamp,
            timestamp,
        ),
    )

    cursor.execute(
        "SELECT id FROM permissions WHERE name = ?",
        (AGENT_PERMISSION,),
    )
    permission_row = cursor.fetchone()

    if permission_row:
        permission_id = permission_row[0]
        cursor.execute(
            """
            SELECT id
            FROM roles
            WHERE name IN ('owner', 'admin', 'user')
            """
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
                (role_id, permission_id, timestamp),
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            model_mode TEXT NOT NULL DEFAULT 'auto',
            allow_web INTEGER NOT NULL DEFAULT 0,
            allow_rag INTEGER NOT NULL DEFAULT 0,
            allow_memory INTEGER NOT NULL DEFAULT 0,
            max_steps INTEGER NOT NULL DEFAULT 6,
            state TEXT NOT NULL DEFAULT 'queued',
            current_step INTEGER NOT NULL DEFAULT 0,
            result TEXT,
            error TEXT,
            pending_question TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            pause_requested INTEGER NOT NULL DEFAULT 0,
            workspace_rel_path TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            step_index INTEGER NOT NULL,
            phase TEXT NOT NULL,
            action TEXT,
            tool_name TEXT,
            status TEXT NOT NULL,
            reason TEXT,
            input_json TEXT NOT NULL DEFAULT '{}',
            output_text TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            UNIQUE(run_id, step_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            query TEXT,
            title TEXT,
            url TEXT NOT NULL,
            domain TEXT,
            published_at TEXT,
            snippet TEXT,
            content TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            UNIQUE(run_id, source_key),
            UNIQUE(run_id, url)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_document_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            attachment_id TEXT,
            document_name TEXT NOT NULL,
            page_number INTEGER,
            chunk_id INTEGER,
            content TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            UNIQUE(run_id, source_key)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            claim TEXT NOT NULL,
            status TEXT NOT NULL,
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
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
        CREATE TABLE IF NOT EXISTS agent_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'artifact',
            size_bytes INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS agent_inputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
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
        CREATE INDEX IF NOT EXISTS idx_agent_runs_user
        ON agent_runs(user_id, updated_at, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_runs_state
        ON agent_runs(state, updated_at)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_steps_run
        ON agent_steps(run_id, step_index)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_sources_run
        ON agent_sources(run_id, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_documents_run
        ON agent_document_sources(run_id, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_evidence_run
        ON agent_evidence(run_id, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run
        ON agent_artifacts(run_id, created_at)
        """
    )

    conn.commit()
    conn.close()


def recover_stale_agent_runs():
    """
    A restarted Flask process cannot continue an in-memory worker safely.
    Preserve all run data and make the run explicitly resumable.
    """

    initialize_agent_storage()
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'interrupted',
            cancel_requested = 0,
            pause_requested = 0,
            error = COALESCE(
                error,
                'Agent run was interrupted because Private AI restarted.'
            ),
            updated_at = ?
        WHERE state IN ('queued', 'running', 'pausing')
        """,
        (timestamp,),
    )
    recovered = cursor.rowcount

    cursor.execute(
        """
        UPDATE agent_steps
        SET
            status = 'interrupted',
            finished_at = ?
        WHERE status = 'running'
        """,
        (timestamp,),
    )

    conn.commit()
    conn.close()
    return recovered


def _normalize_goal(value):
    goal = str(value or "").strip()
    if not goal:
        raise AgentStoreError("Agent goal is required.")
    if len(goal) > 8000:
        raise AgentStoreError("Agent goal is too long.")
    return goal


def _normalize_title(value, goal):
    title = " ".join(str(value or "").split())
    if not title:
        title = " ".join(goal.split())[:72]
    if len(title) > 120:
        title = title[:120].rstrip()
    return title or "Agent run"


def _normalize_model_mode(value):
    mode = str(value or "auto").strip().lower()
    if mode not in VALID_MODEL_MODES:
        raise AgentStoreError("Unsupported agent model mode.")
    return mode


def _normalize_max_steps(value):
    default = int(getattr(config, "AGENT_DEFAULT_MAX_STEPS", 6))
    maximum = int(getattr(config, "AGENT_MAX_STEPS", 10))
    try:
        steps = int(value if value is not None else default)
    except (TypeError, ValueError) as error:
        raise AgentStoreError("Invalid agent step budget.") from error
    return max(2, min(maximum, steps))


def _run_from_row(row):
    if not row:
        return None

    return {
        "id": row[0],
        "user_id": row[1],
        "title": row[2],
        "goal": row[3],
        "model_mode": row[4],
        "allow_web": bool(row[5]),
        "allow_rag": bool(row[6]),
        "allow_memory": bool(row[7]),
        "max_steps": int(row[8] or 6),
        "state": row[9],
        "current_step": int(row[10] or 0),
        "result": row[11],
        "error": row[12],
        "pending_question": row[13],
        "cancel_requested": bool(row[14]),
        "pause_requested": bool(row[15]),
        "workspace_rel_path": row[16],
        "started_at": row[17],
        "finished_at": row[18],
        "created_at": row[19],
        "updated_at": row[20],
    }


def _run_select_sql():
    return """
        SELECT
            id,
            user_id,
            title,
            goal,
            model_mode,
            allow_web,
            allow_rag,
            allow_memory,
            max_steps,
            state,
            current_step,
            result,
            error,
            pending_question,
            cancel_requested,
            pause_requested,
            workspace_rel_path,
            started_at,
            finished_at,
            created_at,
            updated_at
        FROM agent_runs
    """


def create_agent_run(user_id, payload):
    initialize_agent_storage()
    payload = dict(payload or {})
    goal = _normalize_goal(payload.get("goal"))
    title = _normalize_title(payload.get("title"), goal)
    model_mode = _normalize_model_mode(payload.get("model_mode"))
    max_steps = _normalize_max_steps(payload.get("max_steps"))

    run_id = uuid.uuid4().hex
    workspace_rel = _relative_workspace(user_id, run_id)
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_runs (
            id,
            user_id,
            title,
            goal,
            model_mode,
            allow_web,
            allow_rag,
            allow_memory,
            max_steps,
            state,
            current_step,
            result,
            error,
            pending_question,
            cancel_requested,
            pause_requested,
            workspace_rel_path,
            started_at,
            finished_at,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            'queued', 0, NULL, NULL, NULL,
            0, 0, ?, NULL, NULL, ?, ?
        )
        """,
        (
            run_id,
            int(user_id),
            title,
            goal,
            model_mode,
            int(bool(payload.get("allow_web"))),
            int(bool(payload.get("allow_rag"))),
            int(bool(payload.get("allow_memory"))),
            max_steps,
            workspace_rel,
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    conn.close()
    return get_agent_run(user_id, run_id)


def get_agent_run(user_id, run_id):
    initialize_agent_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        _run_select_sql()
        + " WHERE id = ? AND user_id = ?",
        (str(run_id), int(user_id)),
    )
    run = _run_from_row(cursor.fetchone())
    conn.close()
    return run


def list_agent_runs(user_id, limit=50):
    initialize_agent_storage()
    limit = max(1, min(100, int(limit)))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        _run_select_sql()
        + " WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (int(user_id), limit),
    )
    rows = [_run_from_row(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def mark_agent_running(user_id, run_id):
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'running',
            started_at = COALESCE(started_at, ?),
            finished_at = NULL,
            error = NULL,
            pending_question = NULL,
            cancel_requested = 0,
            pause_requested = 0,
            updated_at = ?
        WHERE id = ? AND user_id = ?
          AND state IN ('queued', 'paused', 'interrupted', 'cancelled', 'failed')
        """,
        (timestamp, timestamp, str(run_id), int(user_id)),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def get_agent_controls(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT state, cancel_requested, pause_requested
        FROM agent_runs
        WHERE id = ? AND user_id = ?
        """,
        (str(run_id), int(user_id)),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "state": row[0],
        "cancel_requested": bool(row[1]),
        "pause_requested": bool(row[2]),
    }


def request_agent_cancel(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()

    if run["state"] == "queued":
        cursor.execute(
            """
            UPDATE agent_runs
            SET state = 'cancelled', cancel_requested = 0,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (timestamp, timestamp, str(run_id), int(user_id)),
        )
    elif run["state"] in {"running", "pausing"}:
        cursor.execute(
            """
            UPDATE agent_runs
            SET cancel_requested = 1, state = 'running', updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (timestamp, str(run_id), int(user_id)),
        )
    elif run["state"] in {"paused", "waiting_input", "interrupted"}:
        cursor.execute(
            """
            UPDATE agent_runs
            SET state = 'cancelled', cancel_requested = 0,
                pause_requested = 0, finished_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (timestamp, timestamp, str(run_id), int(user_id)),
        )
    else:
        conn.close()
        return run

    conn.commit()
    conn.close()
    return get_agent_run(user_id, run_id)


def request_agent_pause(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    if run["state"] == "queued":
        state = "paused"
        pause_requested = 0
    elif run["state"] == "running":
        state = "pausing"
        pause_requested = 1
    else:
        return run

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_runs
        SET state = ?, pause_requested = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            state,
            pause_requested,
            utc_iso(),
            str(run_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()
    return get_agent_run(user_id, run_id)


def queue_agent_resume(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    if run["state"] not in {"paused", "interrupted", "cancelled", "failed"}:
        raise AgentStoreError("This agent run is not resumable from its current state.")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'queued',
            cancel_requested = 0,
            pause_requested = 0,
            error = NULL,
            finished_at = NULL,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (utc_iso(), str(run_id), int(user_id)),
    )
    conn.commit()
    conn.close()
    return get_agent_run(user_id, run_id)


def add_agent_input(user_id, run_id, content):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")
    if run["state"] != "waiting_input":
        raise AgentStoreError("This agent is not waiting for user input.")

    text = str(content or "").strip()
    if not text:
        raise AgentStoreError("Input cannot be empty.")
    if len(text) > 6000:
        raise AgentStoreError("Input is too long.")

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_inputs (
            run_id, user_id, content, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (str(run_id), int(user_id), text, timestamp),
    )
    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'queued',
            pending_question = NULL,
            cancel_requested = 0,
            pause_requested = 0,
            finished_at = NULL,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (timestamp, str(run_id), int(user_id)),
    )
    conn.commit()
    conn.close()
    return get_agent_run(user_id, run_id)


def list_agent_inputs(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, content, created_at
        FROM agent_inputs
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC
        """,
        (str(run_id), int(user_id)),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": row[0], "content": row[1], "created_at": row[2]}
        for row in rows
    ]


def begin_agent_step(
    user_id,
    run_id,
    phase,
    action=None,
    tool_name=None,
    reason=None,
    input_data=None,
):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    step_index = int(run["current_step"] or 0) + 1
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_steps (
            run_id, user_id, step_index, phase, action, tool_name,
            status, reason, input_json, output_text,
            started_at, finished_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL, ?, NULL, ?)
        """,
        (
            str(run_id),
            int(user_id),
            step_index,
            str(phase or "action"),
            action,
            tool_name,
            reason,
            _json(input_data or {}),
            timestamp,
            timestamp,
        ),
    )
    step_id = cursor.lastrowid
    cursor.execute(
        """
        UPDATE agent_runs
        SET current_step = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (step_index, timestamp, str(run_id), int(user_id)),
    )
    conn.commit()
    conn.close()
    return {
        "id": step_id,
        "step_index": step_index,
    }


def finish_agent_step(user_id, step_id, status, output_text=None):
    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_steps
        SET status = ?, output_text = ?, finished_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            str(status or "completed"),
            None if output_text is None else str(output_text),
            timestamp,
            int(step_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()


def list_agent_steps(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            id, step_index, phase, action, tool_name, status,
            reason, input_json, output_text, started_at, finished_at
        FROM agent_steps
        WHERE run_id = ? AND user_id = ?
        ORDER BY step_index ASC
        """,
        (str(run_id), int(user_id)),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": row[0],
            "step_index": row[1],
            "phase": row[2],
            "action": row[3],
            "tool_name": row[4],
            "status": row[5],
            "reason": row[6],
            "input": _safe_json_load(row[7], {}),
            "output": row[8],
            "started_at": row[9],
            "finished_at": row[10],
        }
        for row in rows
    ]


def save_agent_source(user_id, run_id, query, source):
    url = str(source.get("url") or "").strip()
    if not url:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, source_key
        FROM agent_sources
        WHERE run_id = ? AND user_id = ? AND url = ?
        """,
        (str(run_id), int(user_id), url),
    )
    existing = cursor.fetchone()

    if existing:
        cursor.execute(
            """
            UPDATE agent_sources
            SET
                query = COALESCE(?, query),
                title = COALESCE(NULLIF(?, ''), title),
                domain = COALESCE(NULLIF(?, ''), domain),
                published_at = COALESCE(?, published_at),
                snippet = COALESCE(NULLIF(?, ''), snippet),
                content = COALESCE(NULLIF(?, ''), content)
            WHERE id = ?
            """,
            (
                query,
                str(source.get("title") or ""),
                str(source.get("domain") or ""),
                source.get("published_at"),
                str(source.get("snippet") or "")[:2000],
                str(source.get("content") or "")[:7000],
                existing[0],
            ),
        )
        conn.commit()
        conn.close()
        return existing[1]

    cursor.execute(
        "SELECT COUNT(*) FROM agent_sources WHERE run_id = ?",
        (str(run_id),),
    )
    source_key = f"S{int(cursor.fetchone()[0] or 0) + 1}"

    cursor.execute(
        """
        INSERT INTO agent_sources (
            run_id, user_id, source_key, query, title, url, domain,
            published_at, snippet, content, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run_id),
            int(user_id),
            source_key,
            query,
            str(source.get("title") or "Web source")[:500],
            url,
            str(source.get("domain") or "")[:255],
            source.get("published_at"),
            str(source.get("snippet") or "")[:2000],
            str(source.get("content") or "")[:7000],
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return source_key


def update_agent_source_content(user_id, run_id, source_key, source):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_sources
        SET
            title = COALESCE(NULLIF(?, ''), title),
            url = COALESCE(NULLIF(?, ''), url),
            domain = COALESCE(NULLIF(?, ''), domain),
            content = COALESCE(NULLIF(?, ''), content)
        WHERE run_id = ? AND user_id = ? AND source_key = ?
        """,
        (
            str(source.get("title") or "")[:500],
            str(source.get("url") or ""),
            str(source.get("domain") or "")[:255],
            str(source.get("content") or "")[:7000],
            str(run_id),
            int(user_id),
            str(source_key),
        ),
    )
    conn.commit()
    conn.close()


def get_agent_source(user_id, run_id, source_key):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, source_key, query, title, url, domain,
               published_at, snippet, content, created_at
        FROM agent_sources
        WHERE run_id = ? AND user_id = ? AND source_key = ?
        """,
        (str(run_id), int(user_id), str(source_key)),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "source_key": row[1],
        "query": row[2],
        "title": row[3],
        "url": row[4],
        "domain": row[5],
        "published_at": row[6],
        "snippet": row[7],
        "content": row[8],
        "created_at": row[9],
    }


def list_agent_sources(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, source_key, query, title, url, domain,
               published_at, snippet, content, created_at
        FROM agent_sources
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC
        """,
        (str(run_id), int(user_id)),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "source_key": row[1],
            "query": row[2],
            "title": row[3],
            "url": row[4],
            "domain": row[5],
            "published_at": row[6],
            "snippet": row[7],
            "content": row[8],
            "created_at": row[9],
        }
        for row in rows
    ]


def save_agent_document_source(user_id, run_id, item):
    chunk_id = item.get("chunk_id")
    conn = get_connection()
    cursor = conn.cursor()

    if chunk_id is not None:
        cursor.execute(
            """
            SELECT source_key
            FROM agent_document_sources
            WHERE run_id = ? AND user_id = ? AND chunk_id = ?
            LIMIT 1
            """,
            (str(run_id), int(user_id), int(chunk_id)),
        )
        existing = cursor.fetchone()
        if existing:
            conn.close()
            return existing[0]

    cursor.execute(
        "SELECT COUNT(*) FROM agent_document_sources WHERE run_id = ?",
        (str(run_id),),
    )
    source_key = f"D{int(cursor.fetchone()[0] or 0) + 1}"

    cursor.execute(
        """
        INSERT INTO agent_document_sources (
            run_id, user_id, source_key, attachment_id, document_name,
            page_number, chunk_id, content, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run_id),
            int(user_id),
            source_key,
            item.get("attachment_id"),
            str(item.get("name") or "document")[:500],
            item.get("page_number"),
            chunk_id,
            str(item.get("content") or "")[:5000],
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return source_key


def list_agent_document_sources(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, source_key, attachment_id, document_name,
               page_number, chunk_id, content, created_at
        FROM agent_document_sources
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC
        """,
        (str(run_id), int(user_id)),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "source_key": row[1],
            "attachment_id": row[2],
            "document_name": row[3],
            "page_number": row[4],
            "chunk_id": row[5],
            "content": row[6],
            "created_at": row[7],
        }
        for row in rows
    ]


def replace_agent_evidence(user_id, run_id, evidence_items):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM agent_evidence WHERE run_id = ? AND user_id = ?",
        (str(run_id), int(user_id)),
    )

    cursor.execute(
        "SELECT source_key FROM agent_sources WHERE run_id = ? AND user_id = ?",
        (str(run_id), int(user_id)),
    )
    valid_refs = {str(row[0]) for row in cursor.fetchall()}
    cursor.execute(
        "SELECT source_key FROM agent_document_sources WHERE run_id = ? AND user_id = ?",
        (str(run_id), int(user_id)),
    )
    valid_refs.update(str(row[0]) for row in cursor.fetchall())

    timestamp = utc_iso()
    for item in evidence_items or []:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        status = str(item.get("status") or "unverified").strip().lower()
        if not claim or status not in VALID_EVIDENCE_STATES:
            continue

        refs = item.get("source_refs") or []
        if not isinstance(refs, list):
            refs = []
        refs = [
            str(ref)[:32]
            for ref in refs[:8]
            if str(ref).strip() in valid_refs
        ]

        cursor.execute(
            """
            INSERT INTO agent_evidence (
                run_id, user_id, claim, status,
                source_refs_json, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run_id),
                int(user_id),
                claim[:4000],
                status,
                json.dumps(refs, ensure_ascii=False),
                str(item.get("notes") or "")[:3000] or None,
                timestamp,
            ),
        )

    conn.commit()
    conn.close()


def list_agent_evidence(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, claim, status, source_refs_json, notes, created_at
        FROM agent_evidence
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC
        """,
        (str(run_id), int(user_id)),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "claim": row[1],
            "status": row[2],
            "source_refs": _safe_json_load(row[3], []),
            "notes": row[4],
            "created_at": row[5],
        }
        for row in rows
    ]


def _safe_artifact_name(filename):
    value = secure_filename(str(filename or "").strip())
    if not value:
        value = "artifact.md"

    suffix = Path(value).suffix.lower()
    if suffix not in ALLOWED_ARTIFACT_EXTENSIONS:
        value = value + ".txt"
        suffix = ".txt"

    return value[:180], suffix


def create_agent_artifact(
    user_id,
    run_id,
    filename,
    content,
    kind="artifact",
    folder="artifacts",
):
    run = get_agent_run(user_id, run_id)
    if not run:
        raise AgentStoreError("Agent run was not found.")

    existing = list_agent_artifacts(user_id, run_id)
    max_artifacts = int(getattr(config, "AGENT_MAX_ARTIFACTS", 6))
    if len(existing) >= max_artifacts:
        raise AgentStoreError("Agent artifact limit reached for this run.")

    text = str(content or "")
    encoded = text.encode("utf-8")
    max_bytes = int(getattr(config, "AGENT_MAX_ARTIFACT_BYTES", 256 * 1024))
    if len(encoded) > max_bytes:
        raise AgentStoreError("Agent artifact is larger than the configured limit.")

    safe_name, suffix = _safe_artifact_name(filename)
    artifact_id = uuid.uuid4().hex
    workspace = _run_workspace(user_id, run_id, create=True)
    folder_name = "files" if folder == "files" else "artifacts"
    target_dir = (workspace / folder_name).resolve()
    if workspace not in target_dir.parents:
        raise AgentStoreError("Invalid artifact directory.")

    target_name = f"{artifact_id[:8]}_{safe_name}"
    path = (target_dir / target_name).resolve()
    if target_dir not in path.parents:
        raise AgentStoreError("Invalid artifact path.")

    path.write_bytes(encoded)
    relative_path = str(path.relative_to(config.GENERATED_DIR.resolve()))
    mime_type = ALLOWED_ARTIFACT_EXTENSIONS.get(
        suffix,
        mimetypes.guess_type(safe_name)[0] or "text/plain",
    )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_artifacts (
            id, run_id, user_id, filename, relative_path,
            mime_type, kind, size_bytes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            str(run_id),
            int(user_id),
            safe_name,
            relative_path,
            mime_type,
            str(kind or "artifact")[:80],
            len(encoded),
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()

    return get_agent_artifact(user_id, artifact_id)


def get_agent_artifact(user_id, artifact_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, run_id, filename, relative_path, mime_type,
               kind, size_bytes, created_at
        FROM agent_artifacts
        WHERE id = ? AND user_id = ?
        """,
        (str(artifact_id), int(user_id)),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "run_id": row[1],
        "filename": row[2],
        "relative_path": row[3],
        "mime_type": row[4],
        "kind": row[5],
        "size_bytes": row[6],
        "created_at": row[7],
    }


def list_agent_artifacts(user_id, run_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, run_id, filename, relative_path, mime_type,
               kind, size_bytes, created_at
        FROM agent_artifacts
        WHERE run_id = ? AND user_id = ?
        ORDER BY created_at ASC
        """,
        (str(run_id), int(user_id)),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "run_id": row[1],
            "filename": row[2],
            "relative_path": row[3],
            "mime_type": row[4],
            "kind": row[5],
            "size_bytes": row[6],
            "created_at": row[7],
        }
        for row in rows
    ]


def get_agent_artifact_path(user_id, artifact_id):
    artifact = get_agent_artifact(user_id, artifact_id)
    if not artifact:
        return None, None

    root = config.GENERATED_DIR.resolve()
    path = (config.GENERATED_DIR / artifact["relative_path"]).resolve()
    if path != root and root not in path.parents:
        return artifact, None
    if not path.is_file():
        return artifact, None
    return artifact, path


def write_agent_log(user_id, run_id, filename, payload):
    workspace = _run_workspace(user_id, run_id, create=True)
    logs = (workspace / "logs").resolve()
    safe = secure_filename(str(filename or "log.json")) or "log.json"
    if not safe.endswith(".json"):
        safe += ".json"
    path = (logs / safe[:180]).resolve()
    if logs not in path.parents:
        return
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass


def mark_agent_paused(user_id, run_id):
    _set_terminalish_state(
        user_id,
        run_id,
        state="paused",
        finished=False,
        error=None,
        pending_question=None,
    )


def mark_agent_waiting_input(user_id, run_id, question):
    _set_terminalish_state(
        user_id,
        run_id,
        state="waiting_input",
        finished=False,
        error=None,
        pending_question=str(question or "")[:5000],
    )


def mark_agent_completed(user_id, run_id, result):
    _set_terminalish_state(
        user_id,
        run_id,
        state="completed",
        finished=True,
        result=str(result or "")[:20000],
        error=None,
        pending_question=None,
    )


def mark_agent_failed(user_id, run_id, error):
    _set_terminalish_state(
        user_id,
        run_id,
        state="failed",
        finished=True,
        error=str(error or "Agent run failed.")[:5000],
        pending_question=None,
    )


def mark_agent_cancelled(user_id, run_id):
    _set_terminalish_state(
        user_id,
        run_id,
        state="cancelled",
        finished=True,
        error=None,
        pending_question=None,
    )


def _set_terminalish_state(
    user_id,
    run_id,
    state,
    finished,
    result=None,
    error=None,
    pending_question=None,
):
    if state not in VALID_AGENT_STATES:
        raise AgentStoreError("Invalid agent state.")

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = ?,
            result = COALESCE(?, result),
            error = ?,
            pending_question = ?,
            cancel_requested = 0,
            pause_requested = 0,
            finished_at = ?,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            state,
            result,
            error,
            pending_question,
            timestamp if finished else None,
            timestamp,
            str(run_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()


def delete_agent_run(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        return False
    if run["state"] in {"queued", "running", "pausing"}:
        raise AgentStoreError("Stop the agent before deleting its run.")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM agent_runs WHERE id = ? AND user_id = ?",
        (str(run_id), int(user_id)),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted:
        workspace = _run_workspace(user_id, run_id, create=False)
        try:
            shutil.rmtree(workspace, ignore_errors=True)
        except OSError:
            pass

    return deleted > 0
