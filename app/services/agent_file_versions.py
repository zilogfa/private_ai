"""
ATLAS v2.2.2 - persistent workspace file history.

Every Agent workspace file remains available as the current mutable working
copy, while immutable snapshots are stored separately for:
- preview
- diff
- provenance
- restore/rollback
- future QA / multi-Agent review

Restoring an old file invalidates a previously completed run's verification and
puts the run into a resumable paused state. ATLAS must re-test/re-finalize before
claiming that the restored workspace is verified.
"""

import difflib
import hashlib
import json
import os
import uuid
from pathlib import Path

import app.config as config

from app.database import get_connection
from app.services.agents import (
    AgentStoreError,
    get_agent_artifact,
    get_agent_artifact_path,
    get_agent_run,
    utc_iso,
)
from app.services.markdown import render_markdown


FILE_PREVIEW_MAX_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_FILE_PREVIEW_MAX_CHARS",
        "120000",
    )
)

FILE_DIFF_MAX_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_FILE_DIFF_MAX_CHARS",
        "160000",
    )
)

_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".html",
    ".css",
    ".js",
    ".py",
}

_ACTIVE_RUN_STATES = {
    "queued",
    "running",
    "pausing",
    "waiting_input",
}

_STORAGE_READY = False


class AgentFileVersionError(Exception):
    pass


def initialize_agent_file_version_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_artifact_versions (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent_write',
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (artifact_id)
                REFERENCES agent_artifacts(id)
                ON DELETE CASCADE,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            UNIQUE(artifact_id, version_number)
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_artifact_versions_artifact
        ON agent_artifact_versions(
            artifact_id,
            version_number DESC
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_artifact_versions_run
        ON agent_artifact_versions(
            run_id,
            created_at
        )
        """
    )

    # External workspace mutations are intentionally separate from Agent steps.
    # A user restore, future manual edit, QA handoff, or multi-Agent merge may
    # change the current workspace without producing write_file/project_repair.
    # Verification must still become stale immediately.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_workspace_mutations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            artifact_id TEXT,
            filename TEXT,
            mutation_type TEXT NOT NULL,
            version_id TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (artifact_id)
                REFERENCES agent_artifacts(id)
                ON DELETE SET NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_workspace_mutations_run
        ON agent_workspace_mutations(
            run_id,
            created_at
        )
        """
    )

    conn.commit()
    conn.close()

    _STORAGE_READY = True


def record_workspace_mutation(
    user_id,
    run_id,
    *,
    artifact_id=None,
    filename=None,
    mutation_type="external_change",
    version_id=None,
    note=None,
):
    """
    Record a workspace change that did NOT occur through a normal Agent step.

    This is a verification primitive, not merely UI history. The coding runner
    compares these mutations to the latest sandbox execution and refuses to
    trust an older successful test after a later mutation.
    """
    initialize_agent_file_version_storage()

    run = get_agent_run(
        user_id,
        run_id,
    )

    if not run:
        raise AgentFileVersionError(
            "Agent run was not found."
        )

    mutation_id = uuid.uuid4().hex
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO agent_workspace_mutations (
            id,
            run_id,
            user_id,
            artifact_id,
            filename,
            mutation_type,
            version_id,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mutation_id,
            str(run_id),
            int(user_id),
            (
                str(artifact_id)
                if artifact_id
                else None
            ),
            (
                str(filename)[:240]
                if filename
                else None
            ),
            str(
                mutation_type
                or "external_change"
            )[:80],
            (
                str(version_id)
                if version_id
                else None
            ),
            (
                str(note)[:2000]
                if note
                else None
            ),
            timestamp,
        ),
    )

    conn.commit()
    conn.close()

    return {
        "id": mutation_id,
        "run_id": str(run_id),
        "user_id": int(user_id),
        "artifact_id": (
            str(artifact_id)
            if artifact_id
            else None
        ),
        "filename": (
            str(filename)
            if filename
            else None
        ),
        "mutation_type": str(
            mutation_type
            or "external_change"
        ),
        "version_id": (
            str(version_id)
            if version_id
            else None
        ),
        "note": (
            str(note)
            if note
            else None
        ),
        "created_at": timestamp,
    }


def list_workspace_mutations_after(
    user_id,
    run_id,
    created_after=None,
    limit=100,
):
    """
    Return external workspace changes after an optional UTC ISO timestamp.

    ISO timestamps are generated by the same utc_iso() helper throughout ATLAS,
    so lexical SQLite comparison is stable here.
    """
    initialize_agent_file_version_storage()

    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT
            id,
            run_id,
            user_id,
            artifact_id,
            filename,
            mutation_type,
            version_id,
            note,
            created_at
        FROM agent_workspace_mutations
        WHERE
            run_id = ?
            AND user_id = ?
    """

    params = [
        str(run_id),
        int(user_id),
    ]

    if created_after:
        query += " AND created_at > ?"
        params.append(
            str(created_after)
        )

    query += """
        ORDER BY created_at ASC
        LIMIT ?
    """

    params.append(
        max(
            1,
            min(
                500,
                int(limit),
            ),
        )
    )

    cursor.execute(
        query,
        tuple(params),
    )

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": row[0],
            "run_id": row[1],
            "user_id": row[2],
            "artifact_id": row[3],
            "filename": row[4],
            "mutation_type": row[5],
            "version_id": row[6],
            "note": row[7],
            "created_at": row[8],
        }
        for row in rows
    ]


def _require_workspace_artifact(
    user_id,
    artifact_id,
):
    artifact = get_agent_artifact(
        user_id,
        artifact_id,
    )

    if not artifact:
        raise AgentFileVersionError(
            "Agent file was not found."
        )

    if str(
        artifact.get(
            "kind"
        )
        or ""
    ) != "workspace_file":
        raise AgentFileVersionError(
            "Version history is available for Agent workspace files."
        )

    artifact, path = get_agent_artifact_path(
        user_id,
        artifact_id,
    )

    if not artifact or not path:
        raise AgentFileVersionError(
            "Agent file is missing on disk."
        )

    return artifact, path


def _version_dir(
    user_id,
    artifact,
):
    run = get_agent_run(
        user_id,
        artifact[
            "run_id"
        ],
    )

    if not run:
        raise AgentFileVersionError(
            "Agent run was not found."
        )

    root = config.GENERATED_DIR.resolve()
    workspace = (
        config.GENERATED_DIR
        / str(
            run.get(
                "workspace_rel_path"
            )
            or ""
        )
    ).resolve()

    if (
        workspace == root
        or root not in workspace.parents
    ):
        raise AgentFileVersionError(
            "Invalid Agent workspace path."
        )

    versions_root = (
        workspace
        / "versions"
        / str(
            artifact[
                "id"
            ]
        )
    ).resolve()

    if workspace not in versions_root.parents:
        raise AgentFileVersionError(
            "Invalid Agent version path."
        )

    versions_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    return versions_root


def _version_from_row(row):
    if not row:
        return None

    return {
        "id":
            row[0],
        "artifact_id":
            row[1],
        "run_id":
            row[2],
        "user_id":
            row[3],
        "version_number":
            int(
                row[4]
            ),
        "relative_path":
            row[5],
        "size_bytes":
            int(
                row[6]
                or 0
            ),
        "sha256":
            row[7],
        "source":
            row[8],
        "note":
            row[9],
        "created_at":
            row[10],
    }


def _latest_version(
    user_id,
    artifact_id,
):
    initialize_agent_file_version_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            artifact_id,
            run_id,
            user_id,
            version_number,
            relative_path,
            size_bytes,
            sha256,
            source,
            note,
            created_at
        FROM agent_artifact_versions
        WHERE
            artifact_id = ?
            AND user_id = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (
            str(
                artifact_id
            ),
            int(
                user_id
            ),
        ),
    )

    version = _version_from_row(
        cursor.fetchone()
    )

    conn.close()

    return version


def _version_by_id(
    user_id,
    artifact_id,
    version_id,
):
    initialize_agent_file_version_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            artifact_id,
            run_id,
            user_id,
            version_number,
            relative_path,
            size_bytes,
            sha256,
            source,
            note,
            created_at
        FROM agent_artifact_versions
        WHERE
            id = ?
            AND artifact_id = ?
            AND user_id = ?
        """,
        (
            str(
                version_id
            ),
            str(
                artifact_id
            ),
            int(
                user_id
            ),
        ),
    )

    version = _version_from_row(
        cursor.fetchone()
    )

    conn.close()

    return version


def _version_path(version):
    root = config.GENERATED_DIR.resolve()

    path = (
        config.GENERATED_DIR
        / str(
            version.get(
                "relative_path"
            )
            or ""
        )
    ).resolve()

    if (
        path == root
        or root not in path.parents
        or not path.is_file()
    ):
        raise AgentFileVersionError(
            "Stored file version is missing on disk."
        )

    return path


def record_current_artifact_version(
    user_id,
    artifact_id,
    *,
    source="agent_write",
    note=None,
):
    """
    Persist the current working copy as an immutable version.

    Duplicate content is not stored twice. This also makes the function safe to
    call when importing legacy/pre-v2.2.2 workspaces or after manual file edits.
    """

    initialize_agent_file_version_storage()

    artifact, current_path = (
        _require_workspace_artifact(
            user_id,
            artifact_id,
        )
    )

    payload = current_path.read_bytes()
    digest = hashlib.sha256(
        payload
    ).hexdigest()

    latest = _latest_version(
        user_id,
        artifact_id,
    )

    if (
        latest
        and latest.get(
            "sha256"
        )
        == digest
    ):
        return {
            **latest,
            "duplicate":
                True,
        }

    next_number = (
        int(
            latest.get(
                "version_number"
            )
            or 0
        )
        + 1
        if latest
        else 1
    )

    version_id = uuid.uuid4().hex
    versions_root = _version_dir(
        user_id,
        artifact,
    )

    safe_suffix = (
        Path(
            artifact[
                "filename"
            ]
        ).suffix
        or ".txt"
    )

    version_path = (
        versions_root
        / (
            f"v{next_number:04d}_"
            f"{version_id[:8]}"
            f"{safe_suffix}"
        )
    ).resolve()

    if versions_root not in version_path.parents:
        raise AgentFileVersionError(
            "Invalid version destination."
        )

    version_path.write_bytes(
        payload
    )

    relative = str(
        version_path.relative_to(
            config.GENERATED_DIR.resolve()
        )
    )

    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO agent_artifact_versions (
            id,
            artifact_id,
            run_id,
            user_id,
            version_number,
            relative_path,
            size_bytes,
            sha256,
            source,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            str(
                artifact_id
            ),
            str(
                artifact[
                    "run_id"
                ]
            ),
            int(
                user_id
            ),
            next_number,
            relative,
            len(
                payload
            ),
            digest,
            str(
                source
                or "agent_write"
            )[
                :40
            ],
            (
                str(
                    note
                )[
                    :1000
                ]
                if note
                else None
            ),
            timestamp,
        ),
    )

    # Keep current artifact metadata accurate even if the file was manually
    # changed outside ATLAS between Agent runs.
    cursor.execute(
        """
        UPDATE agent_artifacts
        SET
            size_bytes = ?,
            created_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            len(
                payload
            ),
            timestamp,
            str(
                artifact_id
            ),
            int(
                user_id
            ),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "id":
            version_id,
        "artifact_id":
            str(
                artifact_id
            ),
        "run_id":
            str(
                artifact[
                    "run_id"
                ]
            ),
        "user_id":
            int(
                user_id
            ),
        "version_number":
            next_number,
        "relative_path":
            relative,
        "size_bytes":
            len(
                payload
            ),
        "sha256":
            digest,
        "source":
            str(
                source
                or "agent_write"
            )[
                :40
            ],
        "note":
            (
                str(
                    note
                )[
                    :1000
                ]
                if note
                else None
            ),
        "created_at":
            timestamp,
        "duplicate":
            False,
    }


def ensure_current_artifact_version(
    user_id,
    artifact_id,
):
    return record_current_artifact_version(
        user_id,
        artifact_id,
        source="current_snapshot",
        note=(
            "Captured current workspace state for version history."
        ),
    )


def list_artifact_versions(
    user_id,
    artifact_id,
):
    ensure_current_artifact_version(
        user_id,
        artifact_id,
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            artifact_id,
            run_id,
            user_id,
            version_number,
            relative_path,
            size_bytes,
            sha256,
            source,
            note,
            created_at
        FROM agent_artifact_versions
        WHERE
            artifact_id = ?
            AND user_id = ?
        ORDER BY version_number DESC
        """,
        (
            str(
                artifact_id
            ),
            int(
                user_id
            ),
        ),
    )

    versions = [
        _version_from_row(
            row
        )
        for row
        in cursor.fetchall()
    ]

    conn.close()

    return versions


def version_counts_for_run(
    user_id,
    run_id,
):
    initialize_agent_file_version_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            artifact_id,
            COUNT(*)
        FROM agent_artifact_versions
        WHERE
            run_id = ?
            AND user_id = ?
        GROUP BY artifact_id
        """,
        (
            str(
                run_id
            ),
            int(
                user_id
            ),
        ),
    )

    counts = {
        row[0]:
            int(
                row[1]
                or 0
            )
        for row
        in cursor.fetchall()
    }

    conn.close()

    return counts


def _decode_preview(
    payload,
    filename,
):
    suffix = Path(
        filename
    ).suffix.lower()

    if suffix not in _TEXT_EXTENSIONS:
        raise AgentFileVersionError(
            "Preview is not available for this file type."
        )

    try:
        text = payload.decode(
            "utf-8"
        )
    except UnicodeDecodeError as error:
        raise AgentFileVersionError(
            "This file is not UTF-8 text and cannot be previewed safely."
        ) from error

    truncated = (
        len(
            text
        )
        > FILE_PREVIEW_MAX_CHARS
    )

    visible = text[
        :FILE_PREVIEW_MAX_CHARS
    ]

    pretty = visible

    if suffix == ".json":
        try:
            parsed = json.loads(
                visible
            )
            pretty = json.dumps(
                parsed,
                ensure_ascii=False,
                indent=2,
            )
        except Exception:
            pretty = visible

    return {
        "text":
            pretty,
        "truncated":
            truncated,
        "extension":
            suffix,
        "rendered_html":
            (
                render_markdown(
                    visible
                )
                if suffix == ".md"
                else None
            ),
        "preview_mode":
            (
                "markdown"
                if suffix == ".md"
                else "source"
            ),
        "security_note":
            (
                "HTML/JavaScript are shown as inert source code; they are not "
                "executed in the ATLAS origin."
                if suffix
                in {
                    ".html",
                    ".js",
                }
                else None
            ),
    }


def preview_artifact(
    user_id,
    artifact_id,
    *,
    version_id=None,
):
    artifact, current_path = (
        _require_workspace_artifact(
            user_id,
            artifact_id,
        )
    )

    versions = list_artifact_versions(
        user_id,
        artifact_id,
    )

    latest = (
        versions[0]
        if versions
        else None
    )

    if version_id:
        version = _version_by_id(
            user_id,
            artifact_id,
            version_id,
        )

        if not version:
            raise AgentFileVersionError(
                "File version was not found."
            )

        path = _version_path(
            version
        )
        payload = path.read_bytes()
        selected = version
        is_current = bool(
            latest
            and latest[
                "id"
            ]
            == version[
                "id"
            ]
        )
    else:
        payload = current_path.read_bytes()
        selected = latest
        is_current = True

    preview = _decode_preview(
        payload,
        artifact[
            "filename"
        ],
    )

    return {
        "artifact": {
            **artifact,
            "version_count":
                len(
                    versions
                ),
        },
        "selected_version":
            selected,
        "current_version":
            latest,
        "is_current":
            is_current,
        **preview,
    }


def diff_version_to_current(
    user_id,
    artifact_id,
    version_id,
):
    artifact, current_path = (
        _require_workspace_artifact(
            user_id,
            artifact_id,
        )
    )

    versions = list_artifact_versions(
        user_id,
        artifact_id,
    )

    version = _version_by_id(
        user_id,
        artifact_id,
        version_id,
    )

    if not version:
        raise AgentFileVersionError(
            "File version was not found."
        )

    version_path = _version_path(
        version
    )

    try:
        old_text = version_path.read_text(
            encoding="utf-8"
        )
        current_text = current_path.read_text(
            encoding="utf-8"
        )
    except UnicodeDecodeError as error:
        raise AgentFileVersionError(
            "Diff is available only for UTF-8 text files."
        ) from error

    diff_lines = difflib.unified_diff(
        old_text.splitlines(
            keepends=True
        ),
        current_text.splitlines(
            keepends=True
        ),
        fromfile=(
            f"{artifact['filename']} · "
            f"v{version['version_number']}"
        ),
        tofile=(
            f"{artifact['filename']} · current"
        ),
    )

    diff_text = "".join(
        diff_lines
    )

    truncated = (
        len(
            diff_text
        )
        > FILE_DIFF_MAX_CHARS
    )

    return {
        "artifact":
            artifact,
        "version":
            version,
        "current_version":
            (
                versions[0]
                if versions
                else None
            ),
        "diff":
            diff_text[
                :FILE_DIFF_MAX_CHARS
            ],
        "truncated":
            truncated,
        "changed":
            bool(
                diff_text
            ),
    }


def get_version_file_path(
    user_id,
    artifact_id,
    version_id,
):
    artifact, _ = _require_workspace_artifact(
        user_id,
        artifact_id,
    )

    version = _version_by_id(
        user_id,
        artifact_id,
        version_id,
    )

    if not version:
        return artifact, None, None

    return (
        artifact,
        version,
        _version_path(
            version
        ),
    )


def restore_artifact_version(
    user_id,
    artifact_id,
    version_id,
):
    artifact, current_path = (
        _require_workspace_artifact(
            user_id,
            artifact_id,
        )
    )

    run = get_agent_run(
        user_id,
        artifact[
            "run_id"
        ],
    )

    if not run:
        raise AgentFileVersionError(
            "Agent run was not found."
        )

    if run[
        "state"
    ] in _ACTIVE_RUN_STATES:
        raise AgentFileVersionError(
            "Stop, pause, or finish the Agent run before restoring a file version."
        )

    target = _version_by_id(
        user_id,
        artifact_id,
        version_id,
    )

    if not target:
        raise AgentFileVersionError(
            "File version was not found."
        )

    # Preserve any current/manual workspace state before rollback.
    ensure_current_artifact_version(
        user_id,
        artifact_id,
    )

    target_path = _version_path(
        target
    )

    target_payload = target_path.read_bytes()
    current_payload = current_path.read_bytes()

    if (
        hashlib.sha256(
            target_payload
        ).hexdigest()
        == hashlib.sha256(
            current_payload
        ).hexdigest()
    ):
        return {
            "restored":
                False,
            "no_change":
                True,
            "artifact":
                artifact,
            "version":
                target,
            "run":
                run,
        }

    current_path.write_bytes(
        target_payload
    )

    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_artifacts
        SET
            size_bytes = ?,
            created_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            len(
                target_payload
            ),
            timestamp,
            str(
                artifact_id
            ),
            int(
                user_id
            ),
        ),
    )

    feedback = (
        "[WORKSPACE RESTORE]\n"
        f"The user restored {artifact['filename']} "
        f"to historical version v{target['version_number']}. "
        "Inspect the current workspace and re-verify affected behavior before "
        "claiming completion."
    )

    cursor.execute(
        """
        INSERT INTO agent_inputs (
            run_id,
            user_id,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            str(
                artifact[
                    "run_id"
                ]
            ),
            int(
                user_id
            ),
            feedback,
            timestamp,
        ),
    )

    # A restored workspace no longer matches the old final verification.
    cursor.execute(
        """
        UPDATE agent_runs
        SET
            state = 'paused',
            finished_at = NULL,
            pending_question = NULL,
            cancel_requested = 0,
            pause_requested = 0,
            error = ?,
            updated_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            (
                f"Workspace file {artifact['filename']} was restored to "
                f"v{target['version_number']}. Previous verification is stale; "
                "resume or Continue / Revise to re-verify the current workspace."
            ),
            timestamp,
            str(
                artifact[
                    "run_id"
                ]
            ),
            int(
                user_id
            ),
        ),
    )

    conn.commit()
    conn.close()

    restored_version = record_current_artifact_version(
        user_id,
        artifact_id,
        source="user_restore",
        note=(
            f"Restored from historical version "
            f"v{target['version_number']}."
        ),
    )

    mutation = record_workspace_mutation(
        user_id,
        artifact[
            "run_id"
        ],
        artifact_id=
            artifact_id,
        filename=
            artifact[
                "filename"
            ],
        mutation_type=
            "user_restore",
        version_id=
            restored_version.get(
                "id"
            ),
        note=(
            f"User restored {artifact['filename']} "
            f"from historical version v{target['version_number']}. "
            "Any earlier sandbox verification is stale until the current "
            "workspace is re-tested."
        ),
    )

    refreshed_run = get_agent_run(
        user_id,
        artifact[
            "run_id"
        ],
    )

    return {
        "restored":
            True,
        "no_change":
            False,
        "artifact":
            get_agent_artifact(
                user_id,
                artifact_id,
            ),
        "version":
            target,
        "restored_as_version":
            restored_version,
        "workspace_mutation":
            mutation,
        "run":
            refreshed_run,
    }
