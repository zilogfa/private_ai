import sqlite3
import json

from datetime import datetime

from app.config import DB_FILE


# =========================================================
# DATABASE CONNECTION
# =========================================================

def get_connection():
    DB_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    return conn


# =========================================================
# HELPERS / FOUNDATION SEEDS
# =========================================================

SCHEMA_VERSION = 2

CORE_ROLES = (
    (
        "owner",
        "Full control of the Private AI instance.",
        1,
    ),
    (
        "admin",
        "Administrative access without ownership.",
        1,
    ),
    (
        "user",
        "Standard personal AI access.",
        1,
    ),
    (
        "guest",
        "Limited access intended for temporary users.",
        1,
    ),
)

CORE_PERMISSIONS = (
    (
        "chat.use",
        "Use text chat.",
    ),
    (
        "memory.manage_self",
        "Use and manage personal memory.",
    ),
    (
        "profile.manage_self",
        "Edit own profile.",
    ),
    (
        "settings.manage_self",
        "Edit own settings.",
    ),
    (
        "vision.use",
        "Use vision and image understanding.",
    ),
    (
        "web_search.use",
        "Use web search tools.",
    ),
    (
        "speech.use",
        "Use speech-to-text and text-to-speech.",
    ),
    (
        "image_generation.use",
        "Generate images.",
    ),
    (
        "admin.access",
        "Open the admin control panel.",
    ),
    (
        "users.manage",
        "Create, disable, and manage users.",
    ),
    (
        "roles.manage",
        "Assign and remove roles and permissions.",
    ),
    (
        "audit.view",
        "View administrative audit history.",
    ),
    (
        "system.manage",
        "Manage instance-level system settings.",
    ),
)

ROLE_PERMISSION_NAMES = {
    "owner": tuple(
        permission_name
        for permission_name, _
        in CORE_PERMISSIONS
    ),

    "admin": (
        "chat.use",
        "memory.manage_self",
        "profile.manage_self",
        "settings.manage_self",
        "vision.use",
        "web_search.use",
        "speech.use",
        "image_generation.use",
        "admin.access",
        "users.manage",
        "roles.manage",
        "audit.view",
    ),

    "user": (
        "chat.use",
        "memory.manage_self",
        "profile.manage_self",
        "settings.manage_self",
        "vision.use",
        "web_search.use",
        "speech.use",
        "image_generation.use",
    ),

    "guest": (
        "chat.use",
        "profile.manage_self",
        "settings.manage_self",
    ),
}


def now():
    return datetime.now().isoformat()


def serialize_embedding(embedding):
    if embedding is None:
        return None

    return json.dumps(embedding)


def serialize_json(value):
    if value is None:
        return None

    if isinstance(value, str):
        return value

    return json.dumps(
        value,
        ensure_ascii=False,
    )


def _seed_roles(cursor):
    timestamp = now()

    for name, description, is_system in CORE_ROLES:
        cursor.execute(
            """
            INSERT OR IGNORE INTO roles (
                name,
                description,
                is_system,
                created_at,
                updated_at
            )

            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                description,
                is_system,
                timestamp,
                timestamp,
            )
        )


def _seed_permissions(cursor):
    timestamp = now()

    for name, description in CORE_PERMISSIONS:
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
                name,
                description,
                timestamp,
                timestamp,
            )
        )


def _seed_role_permissions(cursor):
    timestamp = now()

    cursor.execute(
        """
        SELECT id, name
        FROM roles
        """
    )

    role_ids = {
        name: role_id
        for role_id, name
        in cursor.fetchall()
    }

    cursor.execute(
        """
        SELECT id, name
        FROM permissions
        """
    )

    permission_ids = {
        name: permission_id
        for permission_id, name
        in cursor.fetchall()
    }

    for role_name, permission_names in (
        ROLE_PERMISSION_NAMES.items()
    ):
        role_id = role_ids.get(
            role_name
        )

        if not role_id:
            continue

        for permission_name in permission_names:
            permission_id = permission_ids.get(
                permission_name
            )

            if not permission_id:
                continue

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
                )
            )


def _ensure_user_foundation(
    cursor,
    user_id,
    legacy_role="user",
):
    timestamp = now()

    cursor.execute(
        """
        INSERT OR IGNORE INTO user_settings (
            user_id,
            default_model_mode,
            show_thinking,
            theme,
            tts_enabled,
            voice_id,
            preferred_language,
            settings_json,
            created_at,
            updated_at
        )

        VALUES (
            ?,
            'auto',
            1,
            'system',
            0,
            NULL,
            NULL,
            '{}',
            ?,
            ?
        )
        """,
        (
            user_id,
            timestamp,
            timestamp,
        )
    )

    cursor.execute(
        """
        INSERT OR IGNORE INTO auth_credentials (
            user_id,
            password_hash,
            is_enabled,
            failed_login_attempts,
            locked_until,
            last_login_at,
            password_updated_at,
            created_at,
            updated_at
        )

        VALUES (
            ?,
            NULL,
            1,
            0,
            NULL,
            NULL,
            NULL,
            ?,
            ?
        )
        """,
        (
            user_id,
            timestamp,
            timestamp,
        )
    )

    role_name = (
        str(legacy_role or "user")
        .strip()
        .lower()
    )

    cursor.execute(
        """
        SELECT id
        FROM roles
        WHERE name = ?
        """,
        (role_name,)
    )

    role_row = cursor.fetchone()

    if not role_row:
        cursor.execute(
            """
            SELECT id
            FROM roles
            WHERE name = 'user'
            """
        )

        role_row = cursor.fetchone()

    if role_row:
        cursor.execute(
            """
            INSERT OR IGNORE INTO user_roles (
                user_id,
                role_id,
                granted_by_user_id,
                granted_at
            )

            VALUES (?, ?, NULL, ?)
            """,
            (
                user_id,
                role_row[0],
                timestamp,
            )
        )


# =========================================================
# DATABASE INITIALIZATION / MIGRATION
# =========================================================

def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()

    # =====================================================
    # USERS
    #
    # "role" remains for terminal/backward compatibility.
    # user_roles is the long-term authorization source.
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            username TEXT NOT NULL
                UNIQUE COLLATE NOCASE,

            display_name TEXT NOT NULL,

            role TEXT NOT NULL
                DEFAULT 'user',

            status TEXT NOT NULL
                DEFAULT 'active',

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL
        )
    """)

    # =====================================================
    # ROLES
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL
                UNIQUE COLLATE NOCASE,

            description TEXT,

            is_system INTEGER NOT NULL
                DEFAULT 0,

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL
        )
    """)

    # =====================================================
    # PERMISSIONS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL
                UNIQUE COLLATE NOCASE,

            description TEXT,

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL
        )
    """)

    # =====================================================
    # USER SETTINGS / PROFILE PREFERENCES
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,

            default_model_mode TEXT NOT NULL
                DEFAULT 'auto',

            show_thinking INTEGER NOT NULL
                DEFAULT 1,

            theme TEXT NOT NULL
                DEFAULT 'system',

            tts_enabled INTEGER NOT NULL
                DEFAULT 0,

            voice_id TEXT,

            preferred_language TEXT,

            settings_json TEXT NOT NULL
                DEFAULT '{}',

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL,

            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
    """)

    # =====================================================
    # AUTH CREDENTIALS
    #
    # Only password hashes belong here.
    # Never store plaintext passwords.
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_credentials (
            user_id INTEGER PRIMARY KEY,

            password_hash TEXT,

            is_enabled INTEGER NOT NULL
                DEFAULT 1,

            failed_login_attempts INTEGER NOT NULL
                DEFAULT 0,

            locked_until TEXT,

            last_login_at TEXT,

            password_updated_at TEXT,

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL,

            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
    """)

    # =====================================================
    # USER ROLES
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER NOT NULL,

            role_id INTEGER NOT NULL,

            granted_by_user_id INTEGER,

            granted_at TEXT NOT NULL,

            PRIMARY KEY (
                user_id,
                role_id
            ),

            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,

            FOREIGN KEY (role_id)
                REFERENCES roles(id)
                ON DELETE CASCADE,

            FOREIGN KEY (granted_by_user_id)
                REFERENCES users(id)
                ON DELETE SET NULL
        )
    """)

    # =====================================================
    # ROLE PERMISSIONS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_permissions (
            role_id INTEGER NOT NULL,

            permission_id INTEGER NOT NULL,

            granted_at TEXT NOT NULL,

            PRIMARY KEY (
                role_id,
                permission_id
            ),

            FOREIGN KEY (role_id)
                REFERENCES roles(id)
                ON DELETE CASCADE,

            FOREIGN KEY (permission_id)
                REFERENCES permissions(id)
                ON DELETE CASCADE
        )
    """)

    # =====================================================
    # PER-USER PERMISSION OVERRIDES
    #
    # This lets admin allow/deny one feature for one user
    # without creating a new role.
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_permission_overrides (
            user_id INTEGER NOT NULL,

            permission_id INTEGER NOT NULL,

            allowed INTEGER NOT NULL,

            granted_by_user_id INTEGER,

            updated_at TEXT NOT NULL,

            PRIMARY KEY (
                user_id,
                permission_id
            ),

            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,

            FOREIGN KEY (permission_id)
                REFERENCES permissions(id)
                ON DELETE CASCADE,

            FOREIGN KEY (granted_by_user_id)
                REFERENCES users(id)
                ON DELETE SET NULL
        )
    """)

    # =====================================================
    # ADMIN AUDIT LOG
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            actor_user_id INTEGER,

            target_user_id INTEGER,

            action TEXT NOT NULL,

            resource_type TEXT,

            resource_id TEXT,

            details_json TEXT,

            created_at TEXT NOT NULL,

            FOREIGN KEY (actor_user_id)
                REFERENCES users(id)
                ON DELETE SET NULL,

            FOREIGN KEY (target_user_id)
                REFERENCES users(id)
                ON DELETE SET NULL
        )
    """)

    # =====================================================
    # SCHEMA METADATA
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,

            value TEXT NOT NULL,

            updated_at TEXT NOT NULL
        )
    """)

    _seed_roles(cursor)
    _seed_permissions(cursor)
    _seed_role_permissions(cursor)

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
        timestamp = now()

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
                timestamp,
                timestamp,
            )
        )

        default_user_id = (
            cursor.lastrowid
        )

    # -----------------------------------------------------
    # Ensure auth/settings/roles exist for every old user
    # -----------------------------------------------------

    cursor.execute(
        """
        SELECT
            id,
            role

        FROM users
        """
    )

    existing_users = cursor.fetchall()

    for user_id, legacy_role in existing_users:
        _ensure_user_foundation(
            cursor,
            user_id,
            legacy_role,
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

    if (
        "summary_updated_at"
        not in conversation_columns
    ):
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

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_user_roles_role
        ON user_roles(role_id, user_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_role_permissions_permission
        ON role_permissions(permission_id, role_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_user_permission_overrides_permission
        ON user_permission_overrides(
            permission_id,
            user_id
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_admin_audit_created
        ON admin_audit_log(created_at, id)
    """)

    # =====================================================
    # SCHEMA VERSION
    # =====================================================

    cursor.execute(
        """
        INSERT INTO schema_meta (
            key,
            value,
            updated_at
        )

        VALUES (
            'schema_version',
            ?,
            ?
        )

        ON CONFLICT(key)
        DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (
            str(SCHEMA_VERSION),
            now(),
        )
    )

    conn.commit()
    conn.close()

    return default_user_id


# =========================================================
# USERS / PROFILE
# =========================================================

def create_user(
    username,
    display_name=None,
    role="user",
    password_hash=None,
):
    username = username.strip()

    if not username:
        return None

    if display_name is None:
        display_name = username

    display_name = display_name.strip()

    role = (
        str(role or "user")
        .strip()
        .lower()
    )

    conn = get_connection()
    cursor = conn.cursor()

    try:
        timestamp = now()

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
                timestamp,
                timestamp,
            )
        )

        user_id = cursor.lastrowid

        _ensure_user_foundation(
            cursor,
            user_id,
            role,
        )

        if password_hash:
            cursor.execute(
                """
                UPDATE auth_credentials

                SET
                    password_hash = ?,
                    password_updated_at = ?,
                    updated_at = ?

                WHERE user_id = ?
                """,
                (
                    password_hash,
                    timestamp,
                    timestamp,
                    user_id,
                )
            )

        conn.commit()
        conn.close()

        return user_id

    except sqlite3.IntegrityError:
        conn.rollback()
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


def get_user_by_username(username):
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

        WHERE username = ?
        """,
        (username.strip(),)
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


def update_user_profile(
    user_id,
    display_name,
):
    display_name = (
        str(display_name)
        .strip()
    )

    if not display_name:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users

        SET
            display_name = ?,
            updated_at = ?

        WHERE id = ?
        """,
        (
            display_name,
            now(),
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def set_user_status(
    user_id,
    status,
):
    status = (
        str(status)
        .strip()
        .lower()
    )

    if status not in {
        "active",
        "disabled",
        "suspended",
    }:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users

        SET
            status = ?,
            updated_at = ?

        WHERE id = ?
        """,
        (
            status,
            now(),
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# USER SETTINGS
# =========================================================

def get_user_settings(user_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            default_model_mode,
            show_thinking,
            theme,
            tts_enabled,
            voice_id,
            preferred_language,
            settings_json,
            created_at,
            updated_at

        FROM user_settings

        WHERE user_id = ?
        """,
        (user_id,)
    )

    row = cursor.fetchone()

    if not row:
        user = get_user(user_id)

        if not user:
            conn.close()
            return None

        _ensure_user_foundation(
            cursor,
            user_id,
            user[3],
        )

        conn.commit()

        cursor.execute(
            """
            SELECT
                default_model_mode,
                show_thinking,
                theme,
                tts_enabled,
                voice_id,
                preferred_language,
                settings_json,
                created_at,
                updated_at

            FROM user_settings

            WHERE user_id = ?
            """,
            (user_id,)
        )

        row = cursor.fetchone()

    conn.close()

    if not row:
        return None

    try:
        extra_settings = json.loads(
            row[6] or "{}"
        )
    except json.JSONDecodeError:
        extra_settings = {}

    return {
        "user_id": user_id,
        "default_model_mode": row[0],
        "show_thinking": bool(row[1]),
        "theme": row[2],
        "tts_enabled": bool(row[3]),
        "voice_id": row[4],
        "preferred_language": row[5],
        "extra": extra_settings,
        "created_at": row[7],
        "updated_at": row[8],
    }


def update_user_settings(
    user_id,
    default_model_mode=None,
    show_thinking=None,
    theme=None,
    tts_enabled=None,
    voice_id=None,
    preferred_language=None,
    extra=None,
):
    existing = get_user_settings(
        user_id
    )

    if not existing:
        return False

    values = {
        "default_model_mode":
            (
                existing["default_model_mode"]
                if default_model_mode is None
                else str(default_model_mode)
            ),

        "show_thinking":
            (
                int(existing["show_thinking"])
                if show_thinking is None
                else int(bool(show_thinking))
            ),

        "theme":
            (
                existing["theme"]
                if theme is None
                else str(theme)
            ),

        "tts_enabled":
            (
                int(existing["tts_enabled"])
                if tts_enabled is None
                else int(bool(tts_enabled))
            ),

        "voice_id":
            (
                existing["voice_id"]
                if voice_id is None
                else voice_id
            ),

        "preferred_language":
            (
                existing["preferred_language"]
                if preferred_language is None
                else preferred_language
            ),

        "settings_json":
            (
                serialize_json(
                    existing["extra"]
                )
                if extra is None
                else serialize_json(extra)
            ),
    }

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE user_settings

        SET
            default_model_mode = ?,
            show_thinking = ?,
            theme = ?,
            tts_enabled = ?,
            voice_id = ?,
            preferred_language = ?,
            settings_json = ?,
            updated_at = ?

        WHERE user_id = ?
        """,
        (
            values[
                "default_model_mode"
            ],
            values[
                "show_thinking"
            ],
            values[
                "theme"
            ],
            values[
                "tts_enabled"
            ],
            values[
                "voice_id"
            ],
            values[
                "preferred_language"
            ],
            values[
                "settings_json"
            ],
            now(),
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# AUTHENTICATION FOUNDATION
# =========================================================

def get_auth_credentials(user_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            user_id,
            password_hash,
            is_enabled,
            failed_login_attempts,
            locked_until,
            last_login_at,
            password_updated_at,
            created_at,
            updated_at

        FROM auth_credentials

        WHERE user_id = ?
        """,
        (user_id,)
    )

    row = cursor.fetchone()

    conn.close()

    return row


def set_password_hash(
    user_id,
    password_hash,
):
    if not password_hash:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    timestamp = now()

    cursor.execute(
        """
        UPDATE auth_credentials

        SET
            password_hash = ?,
            password_updated_at = ?,
            failed_login_attempts = 0,
            locked_until = NULL,
            updated_at = ?

        WHERE user_id = ?
        """,
        (
            password_hash,
            timestamp,
            timestamp,
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def set_auth_enabled(
    user_id,
    enabled,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE auth_credentials

        SET
            is_enabled = ?,
            updated_at = ?

        WHERE user_id = ?
        """,
        (
            int(bool(enabled)),
            now(),
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def record_login_success(user_id):
    conn = get_connection()
    cursor = conn.cursor()

    timestamp = now()

    cursor.execute(
        """
        UPDATE auth_credentials

        SET
            failed_login_attempts = 0,
            locked_until = NULL,
            last_login_at = ?,
            updated_at = ?

        WHERE user_id = ?
        """,
        (
            timestamp,
            timestamp,
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def record_login_failure(
    user_id,
    locked_until=None,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE auth_credentials

        SET
            failed_login_attempts =
                failed_login_attempts + 1,

            locked_until = ?,

            updated_at = ?

        WHERE user_id = ?
        """,
        (
            locked_until,
            now(),
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# ROLES / PERMISSIONS
# =========================================================

def list_roles():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            name,
            description,
            is_system,
            created_at,
            updated_at

        FROM roles

        ORDER BY id ASC
    """)

    rows = cursor.fetchall()

    conn.close()

    return rows


def get_user_roles(user_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            r.id,
            r.name,
            r.description,
            ur.granted_by_user_id,
            ur.granted_at

        FROM user_roles ur

        JOIN roles r
            ON r.id = ur.role_id

        WHERE ur.user_id = ?

        ORDER BY r.id ASC
        """,
        (user_id,)
    )

    rows = cursor.fetchall()

    conn.close()

    return rows


def assign_role(
    user_id,
    role_name,
    granted_by_user_id=None,
):
    role_name = (
        str(role_name)
        .strip()
        .lower()
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM roles
        WHERE name = ?
        """,
        (role_name,)
    )

    row = cursor.fetchone()

    if not row:
        conn.close()
        return False

    cursor.execute(
        """
        INSERT OR IGNORE INTO user_roles (
            user_id,
            role_id,
            granted_by_user_id,
            granted_at
        )

        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            row[0],
            granted_by_user_id,
            now(),
        )
    )

    conn.commit()
    conn.close()

    return True


def remove_role(
    user_id,
    role_name,
):
    role_name = (
        str(role_name)
        .strip()
        .lower()
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM user_roles

        WHERE
            user_id = ?
            AND role_id = (
                SELECT id
                FROM roles
                WHERE name = ?
            )
        """,
        (
            user_id,
            role_name,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def user_has_role(
    user_id,
    role_name,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT 1

        FROM user_roles ur

        JOIN roles r
            ON r.id = ur.role_id

        WHERE
            ur.user_id = ?
            AND r.name = ?

        LIMIT 1
        """,
        (
            user_id,
            role_name.strip().lower(),
        )
    )

    exists = (
        cursor.fetchone()
        is not None
    )

    conn.close()

    return exists


def set_primary_role(
    user_id,
    role_name,
    granted_by_user_id=None,
):
    if not assign_role(
        user_id,
        role_name,
        granted_by_user_id,
    ):
        return False

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users

        SET
            role = ?,
            updated_at = ?

        WHERE id = ?
        """,
        (
            role_name.strip().lower(),
            now(),
            user_id,
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def list_permissions():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            name,
            description,
            created_at,
            updated_at

        FROM permissions

        ORDER BY id ASC
    """)

    rows = cursor.fetchall()

    conn.close()

    return rows


def set_user_permission_override(
    user_id,
    permission_name,
    allowed,
    granted_by_user_id=None,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM permissions
        WHERE name = ?
        """,
        (
            permission_name
            .strip()
            .lower(),
        )
    )

    row = cursor.fetchone()

    if not row:
        conn.close()
        return False

    cursor.execute(
        """
        INSERT INTO user_permission_overrides (
            user_id,
            permission_id,
            allowed,
            granted_by_user_id,
            updated_at
        )

        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(
            user_id,
            permission_id
        )

        DO UPDATE SET
            allowed = excluded.allowed,
            granted_by_user_id =
                excluded.granted_by_user_id,
            updated_at =
                excluded.updated_at
        """,
        (
            user_id,
            row[0],
            int(bool(allowed)),
            granted_by_user_id,
            now(),
        )
    )

    conn.commit()
    conn.close()

    return True


def clear_user_permission_override(
    user_id,
    permission_name,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM user_permission_overrides

        WHERE
            user_id = ?
            AND permission_id = (
                SELECT id
                FROM permissions
                WHERE name = ?
            )
        """,
        (
            user_id,
            permission_name
            .strip()
            .lower(),
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def user_has_permission(
    user_id,
    permission_name,
):
    permission_name = (
        permission_name
        .strip()
        .lower()
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT status
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    )

    user_row = cursor.fetchone()

    if (
        not user_row
        or user_row[0] != "active"
    ):
        conn.close()
        return False

    cursor.execute(
        """
        SELECT
            upo.allowed

        FROM user_permission_overrides upo

        JOIN permissions p
            ON p.id = upo.permission_id

        WHERE
            upo.user_id = ?
            AND p.name = ?

        LIMIT 1
        """,
        (
            user_id,
            permission_name,
        )
    )

    override = cursor.fetchone()

    if override is not None:
        conn.close()
        return bool(override[0])

    cursor.execute(
        """
        SELECT 1

        FROM user_roles ur

        JOIN role_permissions rp
            ON rp.role_id = ur.role_id

        JOIN permissions p
            ON p.id = rp.permission_id

        WHERE
            ur.user_id = ?
            AND p.name = ?

        LIMIT 1
        """,
        (
            user_id,
            permission_name,
        )
    )

    allowed = (
        cursor.fetchone()
        is not None
    )

    conn.close()

    return allowed


# =========================================================
# ADMIN AUDIT LOG
# =========================================================

def log_admin_action(
    actor_user_id,
    action,
    target_user_id=None,
    resource_type=None,
    resource_id=None,
    details=None,
):
    action = (
        str(action)
        .strip()
    )

    if not action:
        return None

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO admin_audit_log (
            actor_user_id,
            target_user_id,
            action,
            resource_type,
            resource_id,
            details_json,
            created_at
        )

        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_user_id,
            target_user_id,
            action,
            resource_type,
            (
                None
                if resource_id is None
                else str(resource_id)
            ),
            serialize_json(details),
            now(),
        )
    )

    audit_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return audit_id


def list_admin_audit_log(
    limit=100,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            actor_user_id,
            target_user_id,
            action,
            resource_type,
            resource_id,
            details_json,
            created_at

        FROM admin_audit_log

        ORDER BY id DESC

        LIMIT ?
        """,
        (limit,)
    )

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
