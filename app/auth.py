import hmac
import secrets

from datetime import (
    datetime,
    timedelta,
)

from functools import wraps

from flask import (
    abort,
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)

from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)

from app.database import (
    get_auth_credentials,
    get_user,
    get_user_by_username,
    record_login_failure,
    record_login_success,
    set_password_hash,
    user_has_permission,
)


MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 15


# =========================================================
# CURRENT USER
# =========================================================

def get_current_user():
    return getattr(
        g,
        "current_user",
        None,
    )


def get_current_user_id():
    user = get_current_user()

    if not user:
        return None

    return user[0]


def login_user(user_id):
    """
    Start a fresh authenticated browser session.
    """

    session.clear()

    session["user_id"] = int(
        user_id
    )

    session.permanent = True

    # Create a new token after session rotation.
    get_csrf_token()


def logout_user():
    session.clear()


# =========================================================
# AUTHENTICATION
# =========================================================

def _locked_until_is_active(
    locked_until,
):
    if not locked_until:
        return False

    try:
        lock_time = datetime.fromisoformat(
            locked_until
        )

    except (
        TypeError,
        ValueError,
    ):
        return False

    return lock_time > datetime.now()


def authenticate_user(
    username,
    password,
):
    username = (
        str(username or "")
        .strip()
    )

    password = str(
        password or ""
    )

    if not username or not password:
        return (
            None,
            "Username and password are required.",
        )

    user = get_user_by_username(
        username
    )

    if not user:
        return (
            None,
            "Invalid username or password.",
        )

    if user[4] != "active":
        return (
            None,
            "This account is not active.",
        )

    credentials = (
        get_auth_credentials(
            user[0]
        )
    )

    if not credentials:
        return (
            None,
            "Login is not configured for this account.",
        )

    password_hash = credentials[1]
    is_enabled = bool(
        credentials[2]
    )
    failed_attempts = (
        credentials[3]
        or 0
    )
    locked_until = credentials[4]

    if not is_enabled:
        return (
            None,
            "Login is disabled for this account.",
        )

    if _locked_until_is_active(
        locked_until
    ):
        return (
            None,
            "Too many failed attempts. Try again later.",
        )

    if not password_hash:
        return (
            None,
            "A password has not been configured for this account.",
        )

    if not check_password_hash(
        password_hash,
        password,
    ):
        next_attempt = (
            failed_attempts + 1
        )

        lock_until_value = None

        if (
            next_attempt
            >= MAX_LOGIN_FAILURES
        ):
            lock_until_value = (
                datetime.now()
                + timedelta(
                    minutes=
                        LOCKOUT_MINUTES
                )
            ).isoformat()

        record_login_failure(
            user[0],
            locked_until=
                lock_until_value,
        )

        return (
            None,
            "Invalid username or password.",
        )

    record_login_success(
        user[0]
    )

    return (
        user,
        None,
    )


def owner_needs_setup():
    owner = get_user_by_username(
        "local_owner"
    )

    if not owner:
        return False

    credentials = (
        get_auth_credentials(
            owner[0]
        )
    )

    if not credentials:
        return True

    return not bool(
        credentials[1]
    )


def set_initial_owner_password(
    password,
):
    owner = get_user_by_username(
        "local_owner"
    )

    if not owner:
        return False

    credentials = (
        get_auth_credentials(
            owner[0]
        )
    )

    if (
        credentials
        and credentials[1]
    ):
        return False

    password_hash = (
        generate_password_hash(
            password
        )
    )

    return set_password_hash(
        owner[0],
        password_hash,
    )


def set_user_password(
    user_id,
    password,
):
    password_hash = (
        generate_password_hash(
            password
        )
    )

    return set_password_hash(
        user_id,
        password_hash,
    )


# =========================================================
# CSRF
# =========================================================

def get_csrf_token():
    token = session.get(
        "_csrf_token"
    )

    if not token:
        token = (
            secrets.token_urlsafe(
                32
            )
        )

        session[
            "_csrf_token"
        ] = token

    return token


def _validate_csrf():
    if request.method not in {
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        return

    expected = session.get(
        "_csrf_token"
    )

    received = (
        request.headers.get(
            "X-CSRF-Token"
        )
        or request.form.get(
            "_csrf_token"
        )
    )

    if (
        not expected
        or not received
        or not hmac.compare_digest(
            expected,
            received,
        )
    ):
        abort(
            400,
            description=
                "Invalid CSRF token.",
        )


# =========================================================
# ACCESS DECORATORS
# =========================================================

def _unauthorized_response():
    if request.path.startswith(
        "/api/"
    ):
        return (
            jsonify({
                "error":
                    "authentication_required"
            }),
            401,
        )

    return redirect(
        url_for(
            "web.login",
            next=request.full_path,
        )
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not get_current_user():
            return (
                _unauthorized_response()
            )

        return view(
            *args,
            **kwargs,
        )

    return wrapped


def permission_required(
    permission_name,
):
    def decorator(view):
        @wraps(view)
        def wrapped(
            *args,
            **kwargs,
        ):
            user_id = (
                get_current_user_id()
            )

            if not user_id:
                return (
                    _unauthorized_response()
                )

            if not user_has_permission(
                user_id,
                permission_name,
            ):
                if request.path.startswith(
                    "/api/"
                ):
                    return (
                        jsonify({
                            "error":
                                "permission_denied",
                            "permission":
                                permission_name,
                        }),
                        403,
                    )

                abort(403)

            return view(
                *args,
                **kwargs,
            )

        return wrapped

    return decorator


# =========================================================
# FLASK INTEGRATION
# =========================================================

def init_auth(app):
    @app.before_request
    def load_current_user():
        user_id = session.get(
            "user_id"
        )

        g.current_user = None

        if not user_id:
            return

        user = get_user(
            user_id
        )

        if (
            user
            and user[4] == "active"
        ):
            g.current_user = user

        else:
            session.clear()

    @app.before_request
    def csrf_protection():
        _validate_csrf()

    @app.context_processor
    def inject_auth_context():
        return {
            "current_user":
                get_current_user(),
            "csrf_token":
                get_csrf_token,
        }
