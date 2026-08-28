from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.auth import (
    authenticate_user,
    get_current_user,
    get_current_user_id,
    login_required,
    login_user,
    logout_user,
    owner_needs_setup,
    permission_required,
    set_initial_owner_password,
)

from app.config import (
    VALID_MODEL_MODES,
)

from app.database import (
    get_user_roles,
    get_user_settings,
    list_admin_audit_log,
    list_users,
    update_user_profile,
    update_user_settings,
    user_has_permission,
)

from app.ui_preferences import (
    merge_accent_setting,
    resolve_accent_choice,
)


web_bp = Blueprint(
    "web",
    __name__,
)


def _safe_next_url(value):
    value = (
        str(value or "")
        .strip()
    )

    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return url_for(
            "web.chat"
        )

    return value


# =========================================================
# ROOT / FIRST-RUN SETUP
# =========================================================

@web_bp.get("/")
def index():
    if owner_needs_setup():
        return redirect(
            url_for(
                "web.setup"
            )
        )

    if get_current_user():
        return redirect(
            url_for(
                "web.chat"
            )
        )

    return redirect(
        url_for(
            "web.login"
        )
    )


@web_bp.route(
    "/setup",
    methods=[
        "GET",
        "POST",
    ],
)
def setup():
    if not owner_needs_setup():
        return redirect(
            url_for(
                "web.login"
            )
        )

    if request.method == "POST":
        password = (
            request.form.get(
                "password",
                "",
            )
        )

        confirm = (
            request.form.get(
                "confirm_password",
                "",
            )
        )

        if len(password) < 8:
            flash(
                "Use at least 8 characters.",
                "error",
            )

        elif password != confirm:
            flash(
                "Passwords do not match.",
                "error",
            )

        elif set_initial_owner_password(
            password
        ):
            flash(
                "Owner password created. You can log in now.",
                "success",
            )

            return redirect(
                url_for(
                    "web.login"
                )
            )

        else:
            flash(
                "Could not create the owner password.",
                "error",
            )

    return render_template(
        "setup.html"
    )


# =========================================================
# LOGIN / LOGOUT
# =========================================================

@web_bp.route(
    "/login",
    methods=[
        "GET",
        "POST",
    ],
)
def login():
    if owner_needs_setup():
        return redirect(
            url_for(
                "web.setup"
            )
        )

    if get_current_user():
        return redirect(
            url_for(
                "web.chat"
            )
        )

    if request.method == "POST":
        user, error = (
            authenticate_user(
                request.form.get(
                    "username"
                ),
                request.form.get(
                    "password"
                ),
            )
        )

        if user:
            login_user(
                user[0]
            )

            return redirect(
                _safe_next_url(
                    request.form.get(
                        "next"
                    )
                )
            )

        flash(
            error
            or "Login failed.",
            "error",
        )

    return render_template(
        "login.html",
        next_url=
            request.args.get(
                "next",
                "",
            ),
    )


@web_bp.post("/logout")
@login_required
def logout():
    logout_user()

    return redirect(
        url_for(
            "web.login"
        )
    )


# =========================================================
# CHAT
# =========================================================

@web_bp.get("/chat")
@permission_required("chat.use")
def chat():
    user = get_current_user()

    settings = (
        get_user_settings(
            user[0]
        )
        or {}
    )

    can_admin = (
        user_has_permission(
            user[0],
            "admin.access",
        )
    )

    return render_template(
        "chat.html",
        user=user,
        settings=settings,
        can_admin=can_admin,
    )


# =========================================================
# PROFILE / SETTINGS
# =========================================================

@web_bp.route(
    "/profile",
    methods=[
        "GET",
        "POST",
    ],
)
@login_required
def profile():
    user_id = (
        get_current_user_id()
    )

    user = get_current_user()

    current_settings = (
        get_user_settings(
            user_id
        )
        or {}
    )

    if request.method == "POST":
        display_name = (
            request.form.get(
                "display_name",
                "",
            )
            .strip()
        )

        model_mode = (
            request.form.get(
                "default_model_mode",
                "auto",
            )
            .strip()
            .lower()
        )

        theme = (
            request.form.get(
                "theme",
                "system",
            )
            .strip()
            .lower()
        )

        show_thinking = (
            request.form.get(
                "show_thinking"
            )
            == "on"
        )

        tts_enabled = (
            request.form.get(
                "tts_enabled"
            )
            == "on"
        )

        accent_color = (
            resolve_accent_choice(
                request.form.get(
                    "accent_choice",
                    "default",
                ),
                request.form.get(
                    "accent_custom",
                ),
            )
        )

        if model_mode not in VALID_MODEL_MODES:
            flash(
                "Invalid model mode.",
                "error",
            )

        elif theme not in {
            "system",
            "light",
            "dark",
        }:
            flash(
                "Invalid theme.",
                "error",
            )

        elif not accent_color:
            flash(
                "Invalid accent color.",
                "error",
            )

        else:
            if display_name:
                update_user_profile(
                    user_id,
                    display_name,
                )

            extra_settings = (
                merge_accent_setting(
                    current_settings,
                    accent_color,
                )
            )

            update_user_settings(
                user_id,
                default_model_mode=
                    model_mode,
                show_thinking=
                    show_thinking,
                theme=
                    theme,
                tts_enabled=
                    tts_enabled,
                extra=
                    extra_settings,
            )

            flash(
                "Settings saved.",
                "success",
            )

            return redirect(
                url_for(
                    "web.profile"
                )
            )

    user = get_current_user()

    settings = (
        get_user_settings(
            user_id
        )
        or {}
    )

    roles = get_user_roles(
        user_id
    )

    can_admin = (
        user_has_permission(
            user_id,
            "admin.access",
        )
    )

    return render_template(
        "profile.html",
        user=user,
        settings=settings,
        roles=roles,
        can_admin=can_admin,
    )


# =========================================================
# ADMIN CONTROL PANEL
# =========================================================

@web_bp.get("/admin")
@permission_required("admin.access")
def admin():
    rows = list_users()

    users = []

    for row in rows:
        roles = get_user_roles(
            row[0]
        )

        users.append({
            "id": row[0],
            "username": row[1],
            "display_name": row[2],
            "primary_role": row[3],
            "status": row[4],
            "roles": [
                role[1]
                for role in roles
            ],
        })

    audit_rows = (
        list_admin_audit_log(
            limit=20
        )
    )

    return render_template(
        "admin.html",
        users=users,
        audit_rows=audit_rows,
    )
