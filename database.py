import sqlite3
import json

from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "private_ai.db"


# =========================================================
# DATABASE CONNECTION
# =========================================================

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.now().isoformat()


def serialize_embedding(embedding):
    if embedding is None:
        return None

    return json.dumps(embedding)


# =========================================================
# DATABASE INITIALIZATION / MIGRATION
# =========================================================

def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()

    # =====================================================
    # USERS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            username TEXT NOT NULL UNIQUE COLLATE NOCASE,

            display_name TEXT NOT NULL,

            role TEXT NOT NULL DEFAULT 'user',

            status TEXT NOT NULL DEFAULT 'active',

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL
        )
    """)

    # -----------------------------------------------------
    # Create initial local owner
    # -----------------------------------------------------

    cursor.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        ("local_owner",)
    )

    row = cursor.fetchone()

    if row:
        default_user_id = row[0]

    else:
        cursor.execute(
            """
            INSERT INTO users (
                username,
                display_name,
                role,
                status,
                created_at,
                updated_at
            )

            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "local_owner",
                "Local Owner",
                "owner",
                "active",
                now(),
                now()
            )
        )

        default_user_id = (
            cursor.lastrowid
        )

    # =====================================================
    # CONVERSATIONS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            title TEXT,

            created_at TEXT NOT NULL,

            summary TEXT,

            summary_updated_at TEXT,

            summarized_through_message_id
                INTEGER DEFAULT 0,

            FOREIGN KEY (user_id)
                REFERENCES users(id)
        )
    """)

    # =====================================================
    # MESSAGES
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            conversation_id INTEGER NOT NULL,

            role TEXT NOT NULL,

            content TEXT NOT NULL,

            created_at TEXT NOT NULL,

            FOREIGN KEY (conversation_id)
                REFERENCES conversations(id)
        )
    """)

    # =====================================================
    # MEMORIES
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            content TEXT NOT NULL,

            category TEXT DEFAULT 'general',

            importance INTEGER DEFAULT 5,

            confidence REAL DEFAULT 0.7,

            source TEXT DEFAULT 'auto',

            status TEXT DEFAULT 'active',

            created_at TEXT NOT NULL,

            updated_at TEXT,

            last_accessed_at TEXT,

            access_count INTEGER DEFAULT 0,

            embedding TEXT,

            merged_into_id INTEGER,

            FOREIGN KEY (user_id)
                REFERENCES users(id)
        )
    """)

    # =====================================================
    # MIGRATE OLD CONVERSATIONS TABLE
    # =====================================================

    cursor.execute(
        "PRAGMA table_info(conversations)"
    )

    conversation_columns = [
        row[1]
        for row in cursor.fetchall()
    ]

    if "user_id" not in conversation_columns:
        cursor.execute("""
            ALTER TABLE conversations
            ADD COLUMN user_id INTEGER
            REFERENCES users(id)
        """)

    if "summary" not in conversation_columns:
        cursor.execute("""
            ALTER TABLE conversations
            ADD COLUMN summary TEXT
        """)

    if "summary_updated_at" not in conversation_columns:
        cursor.execute("""
            ALTER TABLE conversations
            ADD COLUMN summary_updated_at TEXT
        """)

    if (
        "summarized_through_message_id"
        not in conversation_columns
    ):
        cursor.execute("""
            ALTER TABLE conversations
            ADD COLUMN summarized_through_message_id
            INTEGER DEFAULT 0
        """)

    # Existing conversations belong to local owner
    cursor.execute(
        """
        UPDATE conversations
        SET user_id = ?
        WHERE user_id IS NULL
        """,
        (default_user_id,)
    )

    # =====================================================
    # MIGRATE OLD MEMORIES TABLE
    # =====================================================

    cursor.execute(
        "PRAGMA table_info(memories)"
    )

    memory_columns = [
        row[1]
        for row in cursor.fetchall()
    ]

    if "user_id" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN user_id INTEGER
            REFERENCES users(id)
        """)

    if "category" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN category TEXT
            DEFAULT 'general'
        """)

    if "importance" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN importance INTEGER
            DEFAULT 5
        """)

    if "confidence" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN confidence REAL
            DEFAULT 0.7
        """)

    if "source" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN source TEXT
            DEFAULT 'auto'
        """)

    if "status" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN status TEXT
            DEFAULT 'active'
        """)

    if "updated_at" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN updated_at TEXT
        """)

    if "last_accessed_at" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN last_accessed_at TEXT
        """)

    if "access_count" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN access_count INTEGER
            DEFAULT 0
        """)

    if "embedding" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN embedding TEXT
        """)

    if "merged_into_id" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN merged_into_id INTEGER
        """)

    # Existing memories belong to local owner
    cursor.execute(
        """
        UPDATE memories
        SET user_id = ?
        WHERE user_id IS NULL
        """,
        (default_user_id,)
    )

    # =====================================================
    # INDEXES
    # =====================================================

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_conversations_user
        ON conversations(user_id, id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_messages_conversation
        ON messages(conversation_id, id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_memories_user_status
        ON memories(user_id, status)
    """)

    conn.commit()
    conn.close()

    return default_user_id


# =========================================================
# USERS
# =========================================================

def create_user(
    username,
    display_name=None,
    role="user"
):
    username = username.strip()

    if not username:
        return None

    if display_name is None:
        display_name = username

    display_name = display_name.strip()

    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO users (
                username,
                display_name,
                role,
                status,
                created_at,
                updated_at
            )

            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (
                username,
                display_name,
                role,
                now(),
                now()
            )
        )

        user_id = cursor.lastrowid

        conn.commit()
        conn.close()

        return user_id

    except sqlite3.IntegrityError:
        conn.close()
        return None


def get_user(user_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            username,
            display_name,
            role,
            status,
            created_at,
            updated_at

        FROM users

        WHERE id = ?
        """,
        (user_id,)
    )

    row = cursor.fetchone()

    conn.close()

    return row


def list_users():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            username,
            display_name,
            role,
            status,
            created_at

        FROM users

        ORDER BY id ASC
    """)

    rows = cursor.fetchall()

    conn.close()

    return rows


# =========================================================
# CONVERSATIONS
# =========================================================

def conversation_belongs_to_user(
    conversation_id,
    user_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT 1

        FROM conversations

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            conversation_id,
            user_id
        )
    )

    exists = (
        cursor.fetchone()
        is not None
    )

    conn.close()

    return exists


def create_conversation(
    user_id,
    title="New Chat"
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO conversations (
            user_id,
            title,
            created_at
        )

        VALUES (?, ?, ?)
        """,
        (
            user_id,
            title,
            now()
        )
    )

    conversation_id = (
        cursor.lastrowid
    )

    conn.commit()
    conn.close()

    return conversation_id


def list_conversations(
    user_id,
    limit=10
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            title,
            created_at

        FROM conversations

        WHERE user_id = ?

        ORDER BY id DESC

        LIMIT ?
        """,
        (
            user_id,
            limit
        )
    )

    rows = cursor.fetchall()

    conn.close()

    return rows


# =========================================================
# SESSION SUMMARIES
# =========================================================

def get_conversation_summary(
    conversation_id,
    user_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            summary,
            summary_updated_at,
            summarized_through_message_id

        FROM conversations

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            conversation_id,
            user_id
        )
    )

    row = cursor.fetchone()

    conn.close()

    if not row:
        return {
            "summary": None,
            "summary_updated_at": None,
            "summarized_through_message_id": 0
        }

    return {
        "summary":
            row[0],

        "summary_updated_at":
            row[1],

        "summarized_through_message_id":
            row[2] or 0
    }


def update_conversation_summary(
    conversation_id,
    user_id,
    summary,
    summarized_through_message_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE conversations

        SET
            summary = ?,
            summary_updated_at = ?,
            summarized_through_message_id = ?

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            summary,
            now(),
            summarized_through_message_id,
            conversation_id,
            user_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# MESSAGES
# =========================================================

def save_message(
    conversation_id,
    user_id,
    role,
    content
):
    if not conversation_belongs_to_user(
        conversation_id,
        user_id
    ):
        return None

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO messages (
            conversation_id,
            role,
            content,
            created_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            conversation_id,
            role,
            content,
            now()
        )
    )

    message_id = (
        cursor.lastrowid
    )

    conn.commit()
    conn.close()

    return message_id


def load_messages(
    conversation_id,
    user_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            m.role,
            m.content

        FROM messages m

        JOIN conversations c
            ON c.id = m.conversation_id

        WHERE
            m.conversation_id = ?
            AND c.user_id = ?

        ORDER BY m.id ASC
        """,
        (
            conversation_id,
            user_id
        )
    )

    rows = cursor.fetchall()

    conn.close()

    return [
        {
            "role": role,
            "content": content
        }

        for role, content
        in rows
    ]


def load_message_records(
    conversation_id,
    user_id,
    after_id=0
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            m.id,
            m.role,
            m.content,
            m.created_at

        FROM messages m

        JOIN conversations c
            ON c.id = m.conversation_id

        WHERE
            m.conversation_id = ?
            AND c.user_id = ?
            AND m.id > ?

        ORDER BY m.id ASC
        """,
        (
            conversation_id,
            user_id,
            after_id
        )
    )

    rows = cursor.fetchall()

    conn.close()

    return [
        {
            "id": row[0],
            "role": row[1],
            "content": row[2],
            "created_at": row[3]
        }

        for row in rows
    ]


# =========================================================
# MEMORIES
#
# Returned tuple:
#
# 0  id
# 1  content
# 2  category
# 3  importance
# 4  confidence
# 5  source
# 6  status
# 7  created_at
# 8  updated_at
# 9  last_accessed_at
# 10 access_count
# 11 embedding
# 12 merged_into_id
# =========================================================

def save_memory(
    user_id,
    content,
    category="general",
    importance=5,
    confidence=0.7,
    source="auto",
    embedding=None
):
    conn = get_connection()
    cursor = conn.cursor()

    timestamp = now()

    cursor.execute(
        """
        INSERT INTO memories (
            user_id,
            content,
            category,
            importance,
            confidence,
            source,
            status,
            created_at,
            updated_at,
            last_accessed_at,
            access_count,
            embedding,
            merged_into_id
        )

        VALUES (
            ?, ?, ?, ?, ?, ?,
            'active',
            ?, ?,
            NULL,
            0,
            ?,
            NULL
        )
        """,
        (
            user_id,
            content,
            category,
            importance,
            confidence,
            source,
            timestamp,
            timestamp,
            serialize_embedding(
                embedding
            )
        )
    )

    memory_id = (
        cursor.lastrowid
    )

    conn.commit()
    conn.close()

    return memory_id


def load_memories(
    user_id,
    include_archived=False
):
    conn = get_connection()
    cursor = conn.cursor()

    if include_archived:

        cursor.execute(
            """
            SELECT
                id,
                content,
                category,
                importance,
                confidence,
                source,
                status,
                created_at,
                updated_at,
                last_accessed_at,
                access_count,
                embedding,
                merged_into_id

            FROM memories

            WHERE user_id = ?

            ORDER BY
                status ASC,
                importance DESC,
                id ASC
            """,
            (user_id,)
        )

    else:

        cursor.execute(
            """
            SELECT
                id,
                content,
                category,
                importance,
                confidence,
                source,
                status,
                created_at,
                updated_at,
                last_accessed_at,
                access_count,
                embedding,
                merged_into_id

            FROM memories

            WHERE
                user_id = ?
                AND status = 'active'

            ORDER BY
                importance DESC,
                id ASC
            """,
            (user_id,)
        )

    rows = cursor.fetchall()

    conn.close()

    return rows


def get_memory(
    user_id,
    memory_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            content,
            category,
            importance,
            confidence,
            source,
            status,
            created_at,
            updated_at,
            last_accessed_at,
            access_count,
            embedding,
            merged_into_id

        FROM memories

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            memory_id,
            user_id
        )
    )

    row = cursor.fetchone()

    conn.close()

    return row


def update_memory(
    user_id,
    memory_id,
    new_content,
    category=None,
    importance=None,
    confidence=None,
    source=None,
    embedding=None
):
    existing = get_memory(
        user_id,
        memory_id
    )

    if not existing:
        return False

    if category is None:
        category = existing[2]

    if importance is None:
        importance = existing[3]

    if confidence is None:
        confidence = existing[4]

    if source is None:
        source = existing[5]

    if embedding is None:
        embedding_value = existing[11]

    else:
        embedding_value = (
            serialize_embedding(
                embedding
            )
        )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE memories

        SET
            content = ?,
            category = ?,
            importance = ?,
            confidence = ?,
            source = ?,
            updated_at = ?,
            embedding = ?

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            new_content,
            category,
            importance,
            confidence,
            source,
            now(),
            embedding_value,
            memory_id,
            user_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def archive_memory(
    user_id,
    memory_id,
    merged_into_id=None
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE memories

        SET
            status = 'archived',
            updated_at = ?,
            merged_into_id = ?

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            now(),
            merged_into_id,
            memory_id,
            user_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def restore_memory(
    user_id,
    memory_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE memories

        SET
            status = 'active',
            merged_into_id = NULL,
            updated_at = ?

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            now(),
            memory_id,
            user_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def delete_memory(
    user_id,
    memory_id
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM memories

        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            memory_id,
            user_id
        )
    )

    deleted = cursor.rowcount

    conn.commit()
    conn.close()

    return deleted > 0


def mark_memories_accessed(
    user_id,
    memory_ids
):
    if not memory_ids:
        return

    memory_ids = list(
        dict.fromkeys(
            memory_ids
        )
    )

    placeholders = ",".join(
        "?"
        for _ in memory_ids
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        f"""
        UPDATE memories

        SET
            access_count =
                access_count + 1,

            last_accessed_at = ?

        WHERE
            user_id = ?
            AND id IN ({placeholders})
        """,
        [
            now(),
            user_id,
            *memory_ids
        ]
    )

    conn.commit()
    conn.close()


# =========================================================
# EMBEDDING MANAGEMENT
# =========================================================

def load_memories_without_embeddings():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            content

        FROM memories

        WHERE
            status = 'active'
            AND (
                embedding IS NULL
                OR TRIM(embedding) = ''
            )
        """
    )

    rows = cursor.fetchall()

    conn.close()

    return rows


def set_memory_embedding(
    memory_id,
    embedding
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE memories
        SET embedding = ?
        WHERE id = ?
        """,
        (
            serialize_embedding(
                embedding
            ),
            memory_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def clear_memory_embeddings():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE memories
        SET embedding = NULL
        WHERE status = 'active'
    """)

    conn.commit()
    conn.close()