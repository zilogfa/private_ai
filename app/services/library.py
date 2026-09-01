import hashlib
import json
import mimetypes
import os
import uuid

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from werkzeug.utils import secure_filename

import app.config as config

from app.database import (
    get_connection,
    user_has_permission,
)


LIBRARY_PERMISSION = "library.use"

LIBRARY_ROOT = (
    config.DATA_DIR
    / "library"
)

LIBRARY_MAX_UPLOAD_BYTES = int(
    os.environ.get(
        "PRIVATE_AI_LIBRARY_MAX_UPLOAD_BYTES",
        str(250 * 1024 * 1024),
    )
)

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".aiff",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".mkv",
    ".avi",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".doc",
    ".docx",
    ".rtf",
    ".odt",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}

CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sh",
    ".zsh",
    ".bash",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".java",
    ".go",
    ".rs",
    ".swift",
    ".sql",
    ".toml",
    ".ini",
}

DATA_EXTENSIONS = {
    ".csv",
    ".json",
    ".jsonl",
    ".xml",
    ".yaml",
    ".yml",
    ".parquet",
}

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".7z",
}

INLINE_SAFE_KINDS = {
    "image",
    "audio",
    "video",
}

_STORAGE_READY = False


class LibraryError(Exception):
    pass


def _now():
    return datetime.now().isoformat()


def _safe_json(value):
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    try:
        parsed = json.loads(value)
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return {}

    return (
        parsed
        if isinstance(parsed, dict)
        else {}
    )


def _classify_name(
    filename,
    mime_type=None,
):
    suffix = (
        Path(
            str(
                filename
                or ""
            )
        )
        .suffix
        .lower()
    )

    mime = str(
        mime_type
        or ""
    ).lower()

    if (
        suffix in IMAGE_EXTENSIONS
        or mime.startswith(
            "image/"
        )
    ):
        return "image"

    if (
        suffix in AUDIO_EXTENSIONS
        or mime.startswith(
            "audio/"
        )
    ):
        return "audio"

    if (
        suffix in VIDEO_EXTENSIONS
        or mime.startswith(
            "video/"
        )
    ):
        return "video"

    if suffix in CODE_EXTENSIONS:
        return "code"

    if suffix in DATA_EXTENSIONS:
        return "data"

    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"

    if (
        suffix in DOCUMENT_EXTENSIONS
        or mime.startswith(
            "text/"
        )
        or mime in {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    ):
        return "document"

    return "other"


def _permission_seed(
    cursor,
):
    timestamp = _now()

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
            LIBRARY_PERMISSION,
            (
                "Use the personal resource Library, including direct uploads "
                "and self-scoped generated resources."
            ),
            timestamp,
            timestamp,
        ),
    )

    cursor.execute(
        """
        SELECT id
        FROM permissions
        WHERE name = ?
        """,
        (
            LIBRARY_PERMISSION,
        ),
    )

    row = cursor.fetchone()

    if not row:
        return

    permission_id = row[0]

    cursor.execute(
        """
        SELECT id
        FROM roles
        WHERE name IN (
            'owner',
            'admin',
            'user'
        )
        """
    )

    for (
        role_id,
    ) in cursor.fetchall():
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


def initialize_library_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    LIBRARY_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = get_connection()
    cursor = conn.cursor()

    _permission_seed(
        cursor
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS library_items (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            origin TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            relative_path TEXT,
            external_url TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            favorite INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
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
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_library_source_unique
        ON library_items(
            user_id,
            source_type,
            source_id
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_library_user_recent
        ON library_items(
            user_id,
            status,
            created_at
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_library_user_kind
        ON library_items(
            user_id,
            kind,
            origin
        )
        """
    )

    # Resource bridge for later Agent Memory / multi-agent work.
    # v2.0.2 establishes provenance and explicit per-run links without giving
    # agents unrestricted access to the whole Library.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_resource_links (
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            library_item_id TEXT NOT NULL,
            access_mode TEXT NOT NULL DEFAULT 'read_only',
            added_at TEXT NOT NULL,
            PRIMARY KEY (
                run_id,
                library_item_id
            ),
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (library_item_id)
                REFERENCES library_items(id)
                ON DELETE CASCADE
        )
        """
    )

    conn.commit()
    conn.close()

    _STORAGE_READY = True


def _library_user_dir(
    user_id,
):
    path = (
        LIBRARY_ROOT
        / f"user_{int(user_id)}"
    )

    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path


def _library_absolute_path(
    relative_path,
):
    root = LIBRARY_ROOT.resolve()

    candidate = (
        LIBRARY_ROOT
        / str(
            relative_path
            or ""
        )
    ).resolve()

    if (
        candidate == root
        or root not in candidate.parents
    ):
        raise LibraryError(
            "Invalid Library path."
        )

    return candidate


def _upsert_source_item(
    user_id,
    *,
    name,
    kind,
    origin,
    source_type,
    source_id,
    mime_type=None,
    size_bytes=0,
    sha256=None,
    relative_path=None,
    external_url=None,
    metadata=None,
    created_at=None,
):
    initialize_library_storage()

    timestamp = _now()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM library_items
        WHERE
            user_id = ?
            AND source_type = ?
            AND source_id = ?
        """,
        (
            int(
                user_id
            ),
            str(
                source_type
            ),
            str(
                source_id
            ),
        ),
    )

    row = cursor.fetchone()

    if row:
        cursor.execute(
            """
            UPDATE library_items
            SET
                name = ?,
                kind = ?,
                origin = ?,
                mime_type = ?,
                size_bytes = ?,
                sha256 = COALESCE(?, sha256),
                relative_path = COALESCE(?, relative_path),
                external_url = COALESCE(?, external_url),
                metadata_json = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                str(
                    name
                )[:500],
                str(
                    kind
                )[:40],
                str(
                    origin
                )[:40],
                (
                    str(
                        mime_type
                    )[:255]
                    if mime_type
                    else None
                ),
                int(
                    size_bytes
                    or 0
                ),
                sha256,
                relative_path,
                external_url,
                json.dumps(
                    metadata
                    or {},
                    ensure_ascii=False,
                ),
                timestamp,
                row[0],
                int(
                    user_id
                ),
            ),
        )

        item_id = row[0]

    else:
        item_id = uuid.uuid4().hex

        cursor.execute(
            """
            INSERT INTO library_items (
                id,
                user_id,
                name,
                kind,
                origin,
                source_type,
                source_id,
                mime_type,
                size_bytes,
                sha256,
                relative_path,
                external_url,
                status,
                favorite,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'active',
                0,
                ?, ?, ?
            )
            """,
            (
                item_id,
                int(
                    user_id
                ),
                str(
                    name
                )[:500],
                str(
                    kind
                )[:40],
                str(
                    origin
                )[:40],
                str(
                    source_type
                )[:80],
                str(
                    source_id
                )[:255],
                (
                    str(
                        mime_type
                    )[:255]
                    if mime_type
                    else None
                ),
                int(
                    size_bytes
                    or 0
                ),
                sha256,
                relative_path,
                external_url,
                json.dumps(
                    metadata
                    or {},
                    ensure_ascii=False,
                ),
                created_at
                or timestamp,
                timestamp,
            ),
        )

    conn.commit()
    conn.close()

    return get_library_item(
        user_id,
        item_id,
    )


def _sync_chat_uploads(
    user_id,
):
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT
                a.id,
                a.original_name,
                a.mime_type,
                a.kind,
                a.size_bytes,
                a.sha256,
                a.created_at,
                a.conversation_id,
                a.message_id,
                c.title
            FROM attachments a
            LEFT JOIN conversations c
                ON c.id = a.conversation_id
            WHERE
                a.user_id = ?
                AND a.message_id IS NOT NULL
            ORDER BY a.created_at ASC
            """,
            (
                int(
                    user_id
                ),
            ),
        )

        rows = cursor.fetchall()

    except Exception:
        rows = []

    conn.close()

    for row in rows:
        (
            attachment_id,
            name,
            mime_type,
            attachment_kind,
            size_bytes,
            sha256,
            created_at,
            conversation_id,
            message_id,
            conversation_title,
        ) = row

        _upsert_source_item(
            user_id,
            name=name,
            kind=_classify_name(
                name,
                mime_type,
            ),
            origin="chat",
            source_type="attachment",
            source_id=attachment_id,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            metadata={
                "attachment_kind":
                    attachment_kind,
                "conversation_id":
                    conversation_id,
                "message_id":
                    message_id,
                "conversation_title":
                    conversation_title,
            },
            created_at=created_at,
        )


def _sync_agent_artifacts(
    user_id,
):
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT
                a.id,
                a.run_id,
                a.filename,
                a.mime_type,
                a.kind,
                a.size_bytes,
                a.created_at,
                r.title,
                r.goal
            FROM agent_artifacts a
            LEFT JOIN agent_runs r
                ON r.id = a.run_id
            WHERE a.user_id = ?
            ORDER BY a.created_at ASC
            """,
            (
                int(
                    user_id
                ),
            ),
        )

        rows = cursor.fetchall()

    except Exception:
        rows = []

    conn.close()

    for row in rows:
        (
            artifact_id,
            run_id,
            filename,
            mime_type,
            artifact_kind,
            size_bytes,
            created_at,
            run_title,
            run_goal,
        ) = row

        _upsert_source_item(
            user_id,
            name=filename,
            kind=_classify_name(
                filename,
                mime_type,
            ),
            origin="agent",
            source_type="agent_artifact",
            source_id=artifact_id,
            mime_type=mime_type,
            size_bytes=size_bytes,
            metadata={
                "run_id":
                    run_id,
                "run_title":
                    run_title,
                "run_goal":
                    run_goal,
                "artifact_kind":
                    artifact_kind,
            },
            created_at=created_at,
        )


def _sync_generated_images(
    user_id,
):
    user_dir = (
        config.GENERATED_DIR
        / f"user_{int(user_id)}"
    )

    if not user_dir.is_dir():
        return

    for metadata_path in user_dir.glob(
        "*.json"
    ):
        try:
            metadata = json.loads(
                metadata_path.read_text(
                    encoding="utf-8"
                )
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            continue

        image_id = str(
            metadata.get(
                "image_id"
            )
            or metadata_path.stem
        ).strip()

        image_path = (
            user_dir
            / f"{image_id}.png"
        )

        if not image_path.is_file():
            continue

        prompt = str(
            metadata.get(
                "prompt"
            )
            or ""
        ).strip()

        display_name = (
            (
                prompt[:70]
                + (
                    "…"
                    if len(
                        prompt
                    )
                    > 70
                    else ""
                )
            )
            if prompt
            else (
                f"Generated image "
                f"{image_id[:8]}"
            )
        )

        _upsert_source_item(
            user_id,
            name=(
                display_name
                + ".png"
            ),
            kind="image",
            origin="generated",
            source_type="generated_image",
            source_id=image_id,
            mime_type="image/png",
            size_bytes=(
                image_path
                .stat()
                .st_size
            ),
            metadata=metadata,
            created_at=(
                metadata.get(
                    "created_at"
                )
            ),
        )


def sync_library_sources(
    user_id,
):
    initialize_library_storage()

    _sync_chat_uploads(
        user_id
    )

    _sync_agent_artifacts(
        user_id
    )

    _sync_generated_images(
        user_id
    )


def _row_to_item(
    row,
):
    if not row:
        return None

    return {
        "id": row[0],
        "user_id": row[1],
        "name": row[2],
        "kind": row[3],
        "origin": row[4],
        "source_type": row[5],
        "source_id": row[6],
        "mime_type": row[7],
        "size_bytes": row[8],
        "sha256": row[9],
        "relative_path": row[10],
        "external_url": row[11],
        "status": row[12],
        "favorite": bool(
            row[13]
        ),
        "metadata": _safe_json(
            row[14]
        ),
        "created_at": row[15],
        "updated_at": row[16],
    }


def get_library_item(
    user_id,
    item_id,
):
    initialize_library_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            name,
            kind,
            origin,
            source_type,
            source_id,
            mime_type,
            size_bytes,
            sha256,
            relative_path,
            external_url,
            status,
            favorite,
            metadata_json,
            created_at,
            updated_at
        FROM library_items
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            str(
                item_id
            ),
            int(
                user_id
            ),
        ),
    )

    item = _row_to_item(
        cursor.fetchone()
    )

    conn.close()

    return item


def list_library_items(
    user_id,
    *,
    query=None,
    kind=None,
    origin=None,
    favorites_only=False,
    limit=250,
):
    sync_library_sources(
        user_id
    )

    clauses = [
        "user_id = ?",
        "status = 'active'",
    ]

    params = [
        int(
            user_id
        )
    ]

    if kind:
        clauses.append(
            "kind = ?"
        )

        params.append(
            str(
                kind
            )
        )

    if origin:
        clauses.append(
            "origin = ?"
        )

        params.append(
            str(
                origin
            )
        )

    if favorites_only:
        clauses.append(
            "favorite = 1"
        )

    q = str(
        query
        or ""
    ).strip()

    if q:
        clauses.append(
            """
            (
                name LIKE ?
                OR metadata_json LIKE ?
                OR external_url LIKE ?
            )
            """
        )

        pattern = (
            "%"
            + q
            + "%"
        )

        params.extend(
            [
                pattern,
                pattern,
                pattern,
            ]
        )

    params.append(
        max(
            1,
            min(
                1000,
                int(
                    limit
                ),
            ),
        )
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            name,
            kind,
            origin,
            source_type,
            source_id,
            mime_type,
            size_bytes,
            sha256,
            relative_path,
            external_url,
            status,
            favorite,
            metadata_json,
            created_at,
            updated_at
        FROM library_items
        WHERE
        """
        + " AND ".join(
            clauses
        )
        + """
        ORDER BY
            favorite DESC,
            created_at DESC
        LIMIT ?
        """,
        tuple(
            params
        ),
    )

    items = [
        _row_to_item(
            row
        )
        for row
        in cursor.fetchall()
    ]

    conn.close()

    return items


def create_library_upload(
    user_id,
    file_storage,
):
    initialize_library_storage()

    if not file_storage:
        raise LibraryError(
            "A file is required."
        )

    original_name = (
        secure_filename(
            str(
                file_storage.filename
                or ""
            )
        )
        or "library_file"
    )[:220]

    suffix = (
        Path(
            original_name
        )
        .suffix
        .lower()
    )

    item_id = uuid.uuid4().hex

    stored_name = (
        item_id
        + suffix
    )

    user_dir = _library_user_dir(
        user_id
    )

    path = (
        user_dir
        / stored_name
    )

    sha256 = hashlib.sha256()
    size_bytes = 0

    try:
        file_storage.stream.seek(
            0
        )
    except (
        AttributeError,
        OSError,
    ):
        pass

    try:
        with path.open(
            "wb"
        ) as output:
            while True:
                chunk = file_storage.stream.read(
                    1024
                    * 1024
                )

                if not chunk:
                    break

                size_bytes += len(
                    chunk
                )

                if (
                    size_bytes
                    > LIBRARY_MAX_UPLOAD_BYTES
                ):
                    raise LibraryError(
                        "Library file is larger than the configured upload limit."
                    )

                sha256.update(
                    chunk
                )

                output.write(
                    chunk
                )

        if size_bytes <= 0:
            raise LibraryError(
                "The uploaded file is empty."
            )

        mime_type = (
            file_storage.mimetype
            or mimetypes.guess_type(
                original_name
            )[0]
            or "application/octet-stream"
        )

        relative = str(
            path.relative_to(
                LIBRARY_ROOT
            )
        )

        timestamp = _now()

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO library_items (
                id,
                user_id,
                name,
                kind,
                origin,
                source_type,
                source_id,
                mime_type,
                size_bytes,
                sha256,
                relative_path,
                external_url,
                status,
                favorite,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, 'upload',
                'library_upload', ?, ?, ?, ?, ?, NULL,
                'active', 0, '{}', ?, ?
            )
            """,
            (
                item_id,
                int(
                    user_id
                ),
                original_name,
                _classify_name(
                    original_name,
                    mime_type,
                ),
                item_id,
                mime_type,
                size_bytes,
                sha256.hexdigest(),
                relative,
                timestamp,
                timestamp,
            ),
        )

        conn.commit()
        conn.close()

    except Exception:
        try:
            path.unlink(
                missing_ok=True
            )
        except OSError:
            pass

        raise

    return get_library_item(
        user_id,
        item_id,
    )


def create_library_link(
    user_id,
    url,
    title=None,
):
    initialize_library_storage()

    value = str(
        url
        or ""
    ).strip()

    parsed = urlparse(
        value
    )

    if (
        parsed.scheme
        not in {
            "http",
            "https",
        }
        or not parsed.netloc
    ):
        raise LibraryError(
            "Add a valid http:// or https:// link."
        )

    item_id = uuid.uuid4().hex

    display_name = (
        str(
            title
            or ""
        ).strip()
        or parsed.netloc
    )[:500]

    timestamp = _now()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO library_items (
            id,
            user_id,
            name,
            kind,
            origin,
            source_type,
            source_id,
            mime_type,
            size_bytes,
            sha256,
            relative_path,
            external_url,
            status,
            favorite,
            metadata_json,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, 'link', 'link',
            'saved_link', ?, NULL, 0, NULL, NULL, ?,
            'active', 0, '{}', ?, ?
        )
        """,
        (
            item_id,
            int(
                user_id
            ),
            display_name,
            item_id,
            value,
            timestamp,
            timestamp,
        ),
    )

    conn.commit()
    conn.close()

    return get_library_item(
        user_id,
        item_id,
    )


def update_library_item(
    user_id,
    item_id,
    *,
    favorite=None,
    name=None,
):
    item = get_library_item(
        user_id,
        item_id,
    )

    if not item:
        raise LibraryError(
            "Library item was not found."
        )

    assignments = []
    params = []

    if favorite is not None:
        assignments.append(
            "favorite = ?"
        )

        params.append(
            int(
                bool(
                    favorite
                )
            )
        )

    if name is not None:
        cleaned = str(
            name
            or ""
        ).strip()[:500]

        if not cleaned:
            raise LibraryError(
                "Library item name cannot be empty."
            )

        assignments.append(
            "name = ?"
        )

        params.append(
            cleaned
        )

    if not assignments:
        return item

    assignments.append(
        "updated_at = ?"
    )

    params.append(
        _now()
    )

    params.extend(
        [
            str(
                item_id
            ),
            int(
                user_id
            ),
        ]
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE library_items
        SET
        """
        + ", ".join(
            assignments
        )
        + """
        WHERE
            id = ?
            AND user_id = ?
        """,
        tuple(
            params
        ),
    )

    conn.commit()
    conn.close()

    return get_library_item(
        user_id,
        item_id,
    )


def remove_library_item(
    user_id,
    item_id,
):
    item = get_library_item(
        user_id,
        item_id,
    )

    if not item:
        return False

    # Direct Library uploads own their blob. Derived resources are only hidden
    # from Library; the chat/agent/generated source remains untouched.
    if (
        item["source_type"]
        == "library_upload"
        and item.get(
            "relative_path"
        )
    ):
        try:
            _library_absolute_path(
                item[
                    "relative_path"
                ]
            ).unlink(
                missing_ok=True
            )
        except OSError:
            pass

    conn = get_connection()
    cursor = conn.cursor()

    if item[
        "source_type"
    ] in {
        "library_upload",
        "saved_link",
    }:
        cursor.execute(
            """
            DELETE FROM library_items
            WHERE id = ? AND user_id = ?
            """,
            (
                str(
                    item_id
                ),
                int(
                    user_id
                ),
            ),
        )

    else:
        cursor.execute(
            """
            UPDATE library_items
            SET
                status = 'hidden',
                updated_at = ?
            WHERE
                id = ?
                AND user_id = ?
            """,
            (
                _now(),
                str(
                    item_id
                ),
                int(
                    user_id
                ),
            ),
        )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return (
        changed
        > 0
    )


def resolve_library_content(
    user_id,
    item_id,
):
    item = get_library_item(
        user_id,
        item_id,
    )

    if (
        not item
        or item[
            "status"
        ]
        != "active"
    ):
        return (
            item,
            None,
        )

    source_type = (
        item[
            "source_type"
        ]
    )

    if source_type == "library_upload":
        try:
            path = _library_absolute_path(
                item[
                    "relative_path"
                ]
            )
        except LibraryError:
            return (
                item,
                None,
            )

        return (
            item,
            (
                path
                if path.is_file()
                else None
            ),
        )

    if source_type == "attachment":
        from app.services.attachments import (
            get_attachment_path,
        )

        _attachment, path = (
            get_attachment_path(
                item[
                    "source_id"
                ],
                user_id,
            )
        )

        return (
            item,
            path,
        )

    if source_type == "agent_artifact":
        from app.services.agents import (
            get_agent_artifact_path,
        )

        _artifact, path = (
            get_agent_artifact_path(
                user_id,
                item[
                    "source_id"
                ],
            )
        )

        return (
            item,
            path,
        )

    if source_type == "generated_image":
        from app.services.image_generation import (
            get_generated_image_path,
        )

        path = get_generated_image_path(
            user_id,
            item[
                "source_id"
            ],
        )

        return (
            item,
            path,
        )

    return (
        item,
        None,
    )


def public_library_item(
    item,
):
    metadata = dict(
        item.get(
            "metadata"
        )
        or {}
    )

    content_url = None
    download_url = None

    if item.get(
        "source_type"
    ) != "saved_link":
        content_url = (
            f"/api/library/items/"
            f"{item['id']}/content"
        )

        download_url = (
            content_url
            + "?download=1"
        )

    return {
        "id": item[
            "id"
        ],
        "name": item[
            "name"
        ],
        "kind": item[
            "kind"
        ],
        "origin": item[
            "origin"
        ],
        "source_type": item[
            "source_type"
        ],
        "mime_type": item.get(
            "mime_type"
        ),
        "size_bytes": int(
            item.get(
                "size_bytes"
            )
            or 0
        ),
        "favorite": bool(
            item.get(
                "favorite"
            )
        ),
        "external_url": item.get(
            "external_url"
        ),
        "content_url": content_url,
        "download_url": download_url,
        "metadata": metadata,
        "created_at": item.get(
            "created_at"
        ),
        "updated_at": item.get(
            "updated_at"
        ),
    }


def link_library_items_to_agent(
    user_id,
    run_id,
    item_ids,
):
    """
    Foundation for v2.0.x resource-to-agent selection.

    It records explicit, read-only provenance links. Binary understanding and
    tool-specific materialization are intentionally added later rather than
    silently exposing the whole Library to an agent.
    """

    initialize_library_storage()

    unique_ids = []

    for item_id in (
        item_ids
        or []
    ):
        value = str(
            item_id
            or ""
        ).strip()

        if (
            value
            and value
            not in unique_ids
        ):
            unique_ids.append(
                value
            )

    timestamp = _now()

    conn = get_connection()
    cursor = conn.cursor()

    for item_id in unique_ids[:50]:
        item = get_library_item(
            user_id,
            item_id,
        )

        if (
            not item
            or item[
                "status"
            ]
            != "active"
        ):
            continue

        cursor.execute(
            """
            INSERT OR IGNORE INTO agent_resource_links (
                run_id,
                user_id,
                library_item_id,
                access_mode,
                added_at
            )
            VALUES (?, ?, ?, 'read_only', ?)
            """,
            (
                str(
                    run_id
                ),
                int(
                    user_id
                ),
                item_id,
                timestamp,
            ),
        )

    conn.commit()
    conn.close()


def list_agent_resource_links(
    user_id,
    run_id,
):
    initialize_library_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            l.id,
            l.name,
            l.kind,
            l.origin,
            l.mime_type,
            l.size_bytes,
            ar.access_mode,
            ar.added_at
        FROM agent_resource_links ar
        JOIN library_items l
            ON l.id = ar.library_item_id
        WHERE
            ar.user_id = ?
            AND ar.run_id = ?
            AND l.status = 'active'
        ORDER BY ar.added_at ASC
        """,
        (
            int(
                user_id
            ),
            str(
                run_id
            ),
        ),
    )

    rows = cursor.fetchall()

    conn.close()

    return [
        {
            "id": row[0],
            "name": row[1],
            "kind": row[2],
            "origin": row[3],
            "mime_type": row[4],
            "size_bytes": int(
                row[5]
                or 0
            ),
            "access_mode": row[6],
            "added_at": row[7],
        }
        for row in rows
    ]
