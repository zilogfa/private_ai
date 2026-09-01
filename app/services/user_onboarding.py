import re
import secrets

from flask import (
    jsonify,
    redirect,
    request,
    url_for,
)
from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)

from app.database import (
    create_user,
    get_auth_credentials,
    get_connection,
    get_user,
    log_admin_action,
    now,
)


USERNAME_RE = re.compile(
    r"^[A-Za-z0-9._-]{3,64}$"
)

TEMP_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
    "23456789"
)

ALLOWED_ONBOARDING_ROLES = {
    "user",
    "guest",
}

_STORAGE_READY = False


class UserOnboardingError(Exception):
    pass


def initialize_user_onboarding_storage():
    """
    Add first-login password rotation without changing the existing credential
    tuple contract. Columns are appended only, so older auth code keeps the
    same indexes.
    """
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "PRAGMA table_info(auth_credentials)"
    )

    columns = {
        row[1]
        for row
        in cursor.fetchall()
    }

    if (
        "must_change_password"
        not in columns
    ):
        cursor.execute(
            """
            ALTER TABLE auth_credentials
            ADD COLUMN must_change_password
            INTEGER NOT NULL DEFAULT 0
            """
        )

    if (
        "temporary_password_issued_at"
        not in columns
    ):
        cursor.execute(
            """
            ALTER TABLE auth_credentials
            ADD COLUMN temporary_password_issued_at
            TEXT
            """
        )

    conn.commit()
    conn.close()

    _STORAGE_READY = True


def _generate_temporary_password():
    """
    Human-copyable but high-entropy temporary password.

    Example shape:
        Ab7k-P2xm-q9RJ-W4tz

    The plaintext value is returned to the request handler exactly once and is
    never written to SQLite, logs, audit details, or Flask session state.
    """
    groups = []

    for _ in range(
        4
    ):
        groups.append(
            "".join(
                secrets.choice(
                    TEMP_ALPHABET
                )
                for _ in range(
                    4
                )
            )
        )

    return "-".join(
        groups
    )


def user_requires_password_change(
    user_id,
):
    initialize_user_onboarding_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT must_change_password
        FROM auth_credentials
        WHERE user_id = ?
        """,
        (
            int(
                user_id
            ),
        ),
    )

    row = cursor.fetchone()

    conn.close()

    return bool(
        row
        and row[0]
    )


def create_internal_user(
    actor_user_id,
    username,
    display_name,
    role="user",
):
    initialize_user_onboarding_storage()

    username = str(
        username
        or ""
    ).strip()

    display_name = str(
        display_name
        or ""
    ).strip()

    role = str(
        role
        or "user"
    ).strip().lower()

    if not USERNAME_RE.fullmatch(
        username
    ):
        raise UserOnboardingError(
            "Username must be 3-64 characters using letters, numbers, dot, underscore, or hyphen."
        )

    if not display_name:
        display_name = username

    if len(
        display_name
    ) > 120:
        raise UserOnboardingError(
            "Display name is too long."
        )

    if role not in ALLOWED_ONBOARDING_ROLES:
        raise UserOnboardingError(
            "Internal onboarding can create User or Guest accounts only."
        )

    temporary_password = (
        _generate_temporary_password()
    )

    user_id = create_user(
        username=username,
        display_name=display_name,
        role=role,
        password_hash=
            generate_password_hash(
                temporary_password
            ),
    )

    if not user_id:
        raise UserOnboardingError(
            "That username already exists or the account could not be created."
        )

    timestamp = now()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE auth_credentials
        SET
            must_change_password = 1,
            temporary_password_issued_at = ?,
            failed_login_attempts = 0,
            locked_until = NULL,
            updated_at = ?
        WHERE user_id = ?
        """,
        (
            timestamp,
            timestamp,
            int(
                user_id
            ),
        ),
    )

    conn.commit()
    conn.close()

    log_admin_action(
        actor_user_id=
            actor_user_id,
        action=
            "user.create_internal",
        target_user_id=
            user_id,
        resource_type=
            "user",
        resource_id=
            user_id,
        details={
            "username":
                username,
            "display_name":
                display_name,
            "role":
                role,
            "temporary_password":
                "issued_not_stored",
            "must_change_password":
                True,
        },
    )

    return {
        "id": user_id,
        "username": username,
        "display_name": display_name,
        "role": role,
        "temporary_password":
            temporary_password,
    }


def set_required_new_password(
    user_id,
    password,
):
    initialize_user_onboarding_storage()

    password = str(
        password
        or ""
    )

    if len(
        password
    ) < 10:
        raise UserOnboardingError(
            "Use at least 10 characters for the new password."
        )

    credentials = get_auth_credentials(
        user_id
    )

    if not credentials:
        raise UserOnboardingError(
            "Login credentials were not found."
        )

    current_hash = credentials[1]

    if (
        current_hash
        and check_password_hash(
            current_hash,
            password,
        )
    ):
        raise UserOnboardingError(
            "Choose a new password different from the temporary password."
        )

    timestamp = now()
    new_hash = generate_password_hash(
        password
    )

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE auth_credentials
        SET
            password_hash = ?,
            must_change_password = 0,
            temporary_password_issued_at = NULL,
            password_updated_at = ?,
            failed_login_attempts = 0,
            locked_until = NULL,
            updated_at = ?
        WHERE user_id = ?
        """,
        (
            new_hash,
            timestamp,
            timestamp,
            int(
                user_id
            ),
        ),
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    if changed != 1:
        raise UserOnboardingError(
            "The password could not be updated."
        )

    log_admin_action(
        actor_user_id=
            user_id,
        action=
            "user.complete_first_login_password_change",
        target_user_id=
            user_id,
        resource_type=
            "auth",
        resource_id=
            user_id,
        details={
            "must_change_password":
                False,
        },
    )

    return True


def list_internal_users():
    initialize_user_onboarding_storage()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            u.id,
            u.username,
            u.display_name,
            u.role,
            u.status,
            u.created_at,
            ac.last_login_at,
            ac.must_change_password,
            ac.temporary_password_issued_at,
            ac.is_enabled
        FROM users u
        LEFT JOIN auth_credentials ac
            ON ac.user_id = u.id
        ORDER BY u.id ASC
        """
    )

    rows = cursor.fetchall()

    conn.close()

    return [
        {
            "id": row[0],
            "username": row[1],
            "display_name": row[2],
            "role": row[3],
            "status": row[4],
            "created_at": row[5],
            "last_login_at": row[6],
            "must_change_password":
                bool(
                    row[7]
                ),
            "temporary_password_issued_at":
                row[8],
            "login_enabled":
                bool(
                    row[9]
                )
                if row[9] is not None
                else False,
        }
        for row in rows
    ]


def init_user_onboarding(
    app,
):
    """
    Enforce first-login password rotation after the existing auth middleware
    loads g.current_user. This is intentionally outside app/auth.py so the
    security change remains an isolated, additive product feature.
    """

    initialize_user_onboarding_storage()

    @app.before_request
    def require_first_login_password_change():
        from app.auth import (
            get_current_user_id,
        )

        user_id = get_current_user_id()

        if not user_id:
            return None

        if not user_requires_password_change(
            user_id
        ):
            return None

        endpoint = str(
            request.endpoint
            or ""
        )

        if (
            endpoint
            in {
                "onboarding_web.change_password",
                "web.logout",
                "static",
            }
            or request.path.startswith(
                "/static/"
            )
        ):
            return None

        if request.path.startswith(
            "/api/"
        ):
            return (
                jsonify({
                    "error":
                        "password_change_required"
                }),
                403,
            )

        return redirect(
            url_for(
                "onboarding_web.change_password"
            )
        )
