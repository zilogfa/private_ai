import json

from app.database import (
    get_connection,
)

from app.services.automation_store import (
    utc_iso,
)


class NotificationError(Exception):
    pass


def initialize_notification_storage():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel TEXT NOT NULL DEFAULT 'in_app',
            source_type TEXT,
            source_id TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            is_read INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notifications_user_read
        ON notifications(user_id, is_read, id)
        """
    )

    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_source_unique
        ON notifications(
            user_id,
            channel,
            source_type,
            source_id
        )
        WHERE source_id IS NOT NULL
        """
    )

    conn.commit()
    conn.close()


def notify_in_app(
    user_id,
    title,
    body,
    source_type=None,
    source_id=None,
    level="info",
    metadata=None,
):
    initialize_notification_storage()

    title = " ".join(
        str(title or "Notification")
        .split()
    )[:160]
    body = str(
        body or ""
    ).strip()[:12000]
    level = str(
        level or "info"
    ).strip().lower()

    if level not in {
        "info",
        "success",
        "warning",
        "error",
    }:
        level = "info"

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO notifications (
            user_id,
            channel,
            source_type,
            source_id,
            title,
            body,
            level,
            is_read,
            metadata_json,
            created_at
        )
        VALUES (?, 'in_app', ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            int(user_id),
            (
                None
                if source_type is None
                else str(source_type)[:80]
            ),
            (
                None
                if source_id is None
                else str(source_id)[:160]
            ),
            title,
            body,
            level,
            json.dumps(
                metadata or {},
                ensure_ascii=False,
            ),
            utc_iso(),
        ),
    )
    notification_id = cursor.lastrowid

    if not notification_id and source_id is not None:
        cursor.execute(
            """
            SELECT id
            FROM notifications
            WHERE
                user_id = ?
                AND channel = 'in_app'
                AND source_type = ?
                AND source_id = ?
            LIMIT 1
            """,
            (
                int(user_id),
                (
                    None
                    if source_type is None
                    else str(source_type)[:80]
                ),
                str(source_id)[:160],
            ),
        )
        row = cursor.fetchone()
        notification_id = (
            row[0] if row else None
        )

    conn.commit()
    conn.close()
    return notification_id



def send_notification(
    user_id,
    title,
    body,
    channel="in_app",
    source_type=None,
    source_id=None,
    level="info",
    metadata=None,
):
    """
    Notification-channel abstraction. v1.8 ships only the in-app provider,
    while callers already use a channel-neutral interface for future macOS,
    email, or push providers.
    """

    channel = str(
        channel or "in_app"
    ).strip().lower()

    if channel != "in_app":
        raise NotificationError(
            f"Unsupported notification channel: {channel}"
        )

    return notify_in_app(
        user_id=user_id,
        title=title,
        body=body,
        source_type=source_type,
        source_id=source_id,
        level=level,
        metadata=metadata,
    )

def _notification_from_row(row):
    if not row:
        return None

    try:
        metadata = json.loads(
            row[9] or "{}"
        )
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        metadata = {}

    return {
        "id": row[0],
        "user_id": row[1],
        "channel": row[2],
        "source_type": row[3],
        "source_id": row[4],
        "title": row[5],
        "body": row[6],
        "level": row[7],
        "is_read": bool(row[8]),
        "metadata": metadata,
        "created_at": row[10],
    }


def list_notifications(
    user_id,
    unread_only=False,
    limit=50,
):
    initialize_notification_storage()
    limit = max(
        1,
        min(200, int(limit)),
    )

    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT
            id,
            user_id,
            channel,
            source_type,
            source_id,
            title,
            body,
            level,
            is_read,
            metadata_json,
            created_at
        FROM notifications
        WHERE user_id = ?
    """
    params = [int(user_id)]

    if unread_only:
        query += " AND is_read = 0"

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cursor.execute(
        query,
        tuple(params),
    )
    items = [
        _notification_from_row(row)
        for row in cursor.fetchall()
    ]
    conn.close()
    return items


def count_unread_notifications(user_id):
    initialize_notification_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM notifications
        WHERE user_id = ? AND is_read = 0
        """,
        (int(user_id),),
    )
    count = int(
        cursor.fetchone()[0] or 0
    )
    conn.close()
    return count


def mark_notification_read(
    user_id,
    notification_id,
    is_read=True,
):
    initialize_notification_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE notifications
        SET is_read = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            int(bool(is_read)),
            int(notification_id),
            int(user_id),
        ),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def mark_all_notifications_read(user_id):
    initialize_notification_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE notifications
        SET is_read = 1
        WHERE user_id = ? AND is_read = 0
        """,
        (int(user_id),),
    )
    changed = int(
        cursor.rowcount or 0
    )
    conn.commit()
    conn.close()
    return changed
