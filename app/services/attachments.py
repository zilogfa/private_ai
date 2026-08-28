import hashlib
import os
import uuid

from datetime import (
    datetime,
    timedelta,
)

from pathlib import Path

from werkzeug.utils import (
    secure_filename,
)

from app.config import (
    MAX_UPLOAD_BYTES,
    UPLOAD_DIR,
)

from app.database import (
    get_connection,
)


# =========================================================
# ATTACHMENT FOUNDATION
# =========================================================

MAX_ATTACHMENTS_PER_MESSAGE = 4

ALLOWED_EXTENSIONS = {
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"),
    ".webp": ("image", "image/webp"),
    ".pdf": ("document", "application/pdf"),
    ".txt": ("document", "text/plain"),
    ".md": ("document", "text/plain"),
    ".csv": ("document", "text/csv"),
    ".json": ("document", "application/json"),
    ".docx": (
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}


def now():
    return datetime.now().isoformat()


def initialize_attachment_storage():
    """
    Creates only the additive attachment table/indexes.

    Existing users, conversations, messages, memories,
    and settings are never recreated or deleted here.
    """

    UPLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,

            user_id INTEGER NOT NULL,

            conversation_id INTEGER,

            message_id INTEGER,

            original_name TEXT NOT NULL,

            stored_name TEXT NOT NULL,

            relative_path TEXT NOT NULL,

            mime_type TEXT NOT NULL,

            kind TEXT NOT NULL,

            size_bytes INTEGER NOT NULL,

            sha256 TEXT NOT NULL,

            status TEXT NOT NULL
                DEFAULT 'pending',

            created_at TEXT NOT NULL,

            attached_at TEXT,

            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,

            FOREIGN KEY (conversation_id)
                REFERENCES conversations(id)
                ON DELETE CASCADE,

            FOREIGN KEY (message_id)
                REFERENCES messages(id)
                ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_attachments_user_status
        ON attachments(user_id, status, created_at)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_attachments_conversation_message
        ON attachments(conversation_id, message_id, created_at)
    """)

    conn.commit()
    conn.close()

    cleanup_stale_pending()


def _normalize_name(filename):
    cleaned = secure_filename(
        str(filename or "")
    )

    if not cleaned:
        cleaned = "attachment"

    return cleaned[:180]


def _classify_file(filename):
    suffix = (
        Path(filename)
        .suffix
        .lower()
    )

    file_info = ALLOWED_EXTENSIONS.get(
        suffix
    )

    if not file_info:
        raise ValueError(
            "unsupported_file_type"
        )

    kind, mime_type = file_info

    return suffix, kind, mime_type


def _user_directory(user_id):
    path = (
        UPLOAD_DIR
        / f"user_{int(user_id)}"
    )

    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path


def _absolute_path(relative_path):
    upload_root = (
        UPLOAD_DIR.resolve()
    )

    candidate = (
        UPLOAD_DIR
        / relative_path
    ).resolve()

    if (
        candidate != upload_root
        and upload_root not in candidate.parents
    ):
        raise ValueError(
            "invalid_attachment_path"
        )

    return candidate


def _row_to_attachment(row):
    if not row:
        return None

    return {
        "id": row[0],
        "user_id": row[1],
        "conversation_id": row[2],
        "message_id": row[3],
        "original_name": row[4],
        "stored_name": row[5],
        "relative_path": row[6],
        "mime_type": row[7],
        "kind": row[8],
        "size_bytes": row[9],
        "sha256": row[10],
        "status": row[11],
        "created_at": row[12],
        "attached_at": row[13],
    }


def _select_attachment(
    cursor,
    attachment_id,
    user_id,
):
    cursor.execute(
        """
        SELECT
            id,
            user_id,
            conversation_id,
            message_id,
            original_name,
            stored_name,
            relative_path,
            mime_type,
            kind,
            size_bytes,
            sha256,
            status,
            created_at,
            attached_at

        FROM attachments

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            attachment_id,
            user_id,
        )
    )

    return _row_to_attachment(
        cursor.fetchone()
    )


def create_attachment(
    user_id,
    file_storage,
    conversation_id=None,
):
    if not file_storage:
        raise ValueError(
            "file_required"
        )

    original_name = _normalize_name(
        file_storage.filename
    )

    suffix, kind, mime_type = (
        _classify_file(
            original_name
        )
    )

    attachment_id = (
        uuid.uuid4().hex
    )

    stored_name = (
        f"{attachment_id}{suffix}"
    )

    user_dir = _user_directory(
        user_id
    )

    final_path = (
        user_dir
        / stored_name
    )

    relative_path = str(
        final_path.relative_to(
            UPLOAD_DIR
        )
    )

    sha256 = hashlib.sha256()
    size_bytes = 0

    try:
        file_storage.stream.seek(0)

    except (AttributeError, OSError):
        pass

    try:
        with final_path.open("wb") as output:
            while True:
                chunk = (
                    file_storage.stream.read(
                        1024 * 1024
                    )
                )

                if not chunk:
                    break

                size_bytes += len(chunk)

                if size_bytes > MAX_UPLOAD_BYTES:
                    raise ValueError(
                        "file_too_large"
                    )

                sha256.update(chunk)
                output.write(chunk)

        if size_bytes <= 0:
            raise ValueError(
                "empty_file"
            )

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO attachments (
                id,
                user_id,
                conversation_id,
                message_id,
                original_name,
                stored_name,
                relative_path,
                mime_type,
                kind,
                size_bytes,
                sha256,
                status,
                created_at,
                attached_at
            )

            VALUES (
                ?, ?, ?, NULL,
                ?, ?, ?, ?, ?, ?, ?,
                'pending', ?, NULL
            )
            """,
            (
                attachment_id,
                user_id,
                conversation_id,
                original_name,
                stored_name,
                relative_path,
                mime_type,
                kind,
                size_bytes,
                sha256.hexdigest(),
                now(),
            )
        )

        conn.commit()

        attachment = _select_attachment(
            cursor,
            attachment_id,
            user_id,
        )

        conn.close()

        return attachment

    except Exception:
        try:
            final_path.unlink(
                missing_ok=True
            )

        except OSError:
            pass

        raise


def get_attachment(
    attachment_id,
    user_id,
):
    conn = get_connection()
    cursor = conn.cursor()

    attachment = _select_attachment(
        cursor,
        attachment_id,
        user_id,
    )

    conn.close()

    return attachment


def get_attachment_path(
    attachment_id,
    user_id,
):
    attachment = get_attachment(
        attachment_id,
        user_id,
    )

    if not attachment:
        return None, None

    path = _absolute_path(
        attachment["relative_path"]
    )

    if not path.is_file():
        return attachment, None

    return attachment, path


def get_attachments_by_ids(
    user_id,
    attachment_ids,
    conversation_id=None,
):
    ids = []

    for attachment_id in (
        attachment_ids or []
    ):
        value = (
            str(attachment_id or "")
            .strip()
        )

        if (
            value
            and value not in ids
        ):
            ids.append(value)

    if not ids:
        return []

    if len(ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError(
            "too_many_attachments"
        )

    conn = get_connection()
    cursor = conn.cursor()

    attachments = []

    for attachment_id in ids:
        attachment = _select_attachment(
            cursor,
            attachment_id,
            user_id,
        )

        if not attachment:
            conn.close()
            raise ValueError(
                "attachment_not_found"
            )

        if attachment["message_id"] is not None:
            conn.close()
            raise ValueError(
                "attachment_already_used"
            )

        existing_conversation = (
            attachment[
                "conversation_id"
            ]
        )

        if (
            existing_conversation
            is not None
            and conversation_id
            is not None
            and int(existing_conversation)
            != int(conversation_id)
        ):
            conn.close()
            raise ValueError(
                "attachment_conversation_mismatch"
            )

        attachments.append(
            attachment
        )

    conn.close()

    return attachments


def bind_attachments_to_message(
    user_id,
    conversation_id,
    message_id,
    attachment_ids,
):
    attachments = get_attachments_by_ids(
        user_id,
        attachment_ids,
        conversation_id=
            conversation_id,
    )

    if not attachments:
        return []

    conn = get_connection()
    cursor = conn.cursor()

    timestamp = now()

    for attachment in attachments:
        cursor.execute(
            """
            UPDATE attachments

            SET
                conversation_id = ?,
                message_id = ?,
                status = 'attached',
                attached_at = ?

            WHERE
                id = ?
                AND user_id = ?
                AND message_id IS NULL
            """,
            (
                conversation_id,
                message_id,
                timestamp,
                attachment["id"],
                user_id,
            )
        )

        if cursor.rowcount != 1:
            conn.rollback()
            conn.close()

            raise ValueError(
                "attachment_bind_failed"
            )

    conn.commit()
    conn.close()

    return get_attachments_by_message(
        user_id,
        conversation_id,
        message_id,
    )


def get_attachments_by_message(
    user_id,
    conversation_id,
    message_id,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            conversation_id,
            message_id,
            original_name,
            stored_name,
            relative_path,
            mime_type,
            kind,
            size_bytes,
            sha256,
            status,
            created_at,
            attached_at

        FROM attachments

        WHERE
            user_id = ?
            AND conversation_id = ?
            AND message_id = ?

        ORDER BY created_at ASC
        """,
        (
            user_id,
            conversation_id,
            message_id,
        )
    )

    rows = cursor.fetchall()
    conn.close()

    return [
        _row_to_attachment(row)
        for row in rows
    ]


def list_attachments_for_conversation(
    user_id,
    conversation_id,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            conversation_id,
            message_id,
            original_name,
            stored_name,
            relative_path,
            mime_type,
            kind,
            size_bytes,
            sha256,
            status,
            created_at,
            attached_at

        FROM attachments

        WHERE
            user_id = ?
            AND conversation_id = ?
            AND message_id IS NOT NULL

        ORDER BY created_at ASC
        """,
        (
            user_id,
            conversation_id,
        )
    )

    rows = cursor.fetchall()
    conn.close()

    grouped = {}

    for row in rows:
        attachment = _row_to_attachment(
            row
        )

        grouped.setdefault(
            attachment["message_id"],
            [],
        ).append(
            attachment
        )

    return grouped


def delete_pending_attachment(
    attachment_id,
    user_id,
):
    attachment = get_attachment(
        attachment_id,
        user_id,
    )

    if not attachment:
        return False

    if attachment["message_id"] is not None:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM attachments

        WHERE
            id = ?
            AND user_id = ?
            AND message_id IS NULL
        """,
        (
            attachment_id,
            user_id,
        )
    )

    deleted = cursor.rowcount

    conn.commit()
    conn.close()

    if deleted:
        try:
            _absolute_path(
                attachment["relative_path"]
            ).unlink(
                missing_ok=True
            )

        except OSError:
            pass

    return deleted > 0


def cleanup_conversation_files(
    user_id,
    conversation_id,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT relative_path

        FROM attachments

        WHERE
            user_id = ?
            AND conversation_id = ?
        """,
        (
            user_id,
            conversation_id,
        )
    )

    paths = [
        row[0]
        for row in cursor.fetchall()
    ]

    conn.close()

    for relative_path in paths:
        try:
            _absolute_path(
                relative_path
            ).unlink(
                missing_ok=True
            )

        except OSError:
            pass


def cleanup_stale_pending(
    max_age_hours=24,
):
    cutoff = (
        datetime.now()
        - timedelta(
            hours=max_age_hours
        )
    ).isoformat()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            user_id,
            relative_path

        FROM attachments

        WHERE
            message_id IS NULL
            AND created_at < ?
        """,
        (cutoff,)
    )

    stale = cursor.fetchall()

    if stale:
        cursor.executemany(
            """
            DELETE FROM attachments
            WHERE id = ? AND user_id = ?
            """,
            [
                (row[0], row[1])
                for row in stale
            ],
        )

    conn.commit()
    conn.close()

    for _, _, relative_path in stale:
        try:
            _absolute_path(
                relative_path
            ).unlink(
                missing_ok=True
            )

        except OSError:
            pass


def public_attachment_data(
    attachment,
):
    return {
        "id": attachment["id"],
        "name": attachment[
            "original_name"
        ],
        "mime_type": attachment[
            "mime_type"
        ],
        "kind": attachment["kind"],
        "size_bytes": attachment[
            "size_bytes"
        ],
        "status": attachment["status"],
        "content_url": (
            "/api/attachments/"
            f"{attachment['id']}"
            "/content"
        ),
    }


def build_unprocessed_attachment_note(
    attachments,
):
    if not attachments:
        return None

    names = ", ".join(
        attachment["original_name"]
        for attachment in attachments
    )

    return (
        "ATTACHMENT CAPABILITY NOTE:\n"
        "The user attached the following local file(s): "
        f"{names}.\n"
        "This software version has stored the files safely, "
        "but file-content analysis is not connected yet. "
        "Do not claim to have viewed, read, or inferred their contents. "
        "If the user's request depends on the file contents, say that "
        "the attachment was received but analysis will be available "
        "after the vision/document capability is connected."
    )
