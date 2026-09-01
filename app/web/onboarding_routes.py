from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from app.auth import (
    get_current_user,
    get_current_user_id,
    login_required,
    login_user,
    permission_required,
)
from app.database import (
    get_user_settings,
)
from app.services.user_onboarding import (
    UserOnboardingError,
    create_internal_user,
    list_internal_users,
    set_required_new_password,
    user_requires_password_change,
)


onboarding_web_bp = Blueprint(
    "onboarding_web",
    __name__,
)


def _no_store(
    response,
):
    response.headers[
        "Cache-Control"
    ] = (
        "no-store, no-cache, "
        "must-revalidate, private"
    )

    response.headers[
        "Pragma"
    ] = "no-cache"

    return response


@onboarding_web_bp.get(
    "/admin/onboarding"
)
@permission_required(
    "users.manage"
)
def onboarding_dashboard():
    user = get_current_user()

    response = make_response(
        render_template(
            "onboarding.html",
            user=user,
            settings=(
                get_user_settings(
                    user[0]
                )
                or {}
            ),
            users=
                list_internal_users(),
            created_user=None,
        )
    )

    return _no_store(
        response
    )


@onboarding_web_bp.post(
    "/admin/onboarding/create"
)
@permission_required(
    "users.manage"
)
def create_onboarding_user():
    user = get_current_user()
    actor_user_id = user[0]

    created_user = None
    error = None

    try:
        created_user = create_internal_user(
            actor_user_id=
                actor_user_id,
            username=
                request.form.get(
                    "username"
                ),
            display_name=
                request.form.get(
                    "display_name"
                ),
            role=
                request.form.get(
                    "role",
                    "user",
                ),
        )
    except UserOnboardingError as exc:
        error = str(
            exc
        )

    response = make_response(
        render_template(
            "onboarding.html",
            user=user,
            settings=(
                get_user_settings(
                    actor_user_id
                )
                or {}
            ),
            users=
                list_internal_users(),
            created_user=
                created_user,
            create_error=
                error,
        )
    )

    return _no_store(
        response
    )


@onboarding_web_bp.route(
    "/change-password",
    methods=[
        "GET",
        "POST",
    ],
)
@login_required
def change_password():
    user = get_current_user()
    user_id = user[0]

    if not user_requires_password_change(
        user_id
    ):
        return redirect(
            url_for(
                "web.chat"
            )
        )

    error = None

    if request.method == "POST":
        password = request.form.get(
            "password",
            "",
        )

        confirm = request.form.get(
            "confirm_password",
            "",
        )

        if password != confirm:
            error = (
                "Passwords do not match."
            )
        else:
            try:
                set_required_new_password(
                    user_id,
                    password,
                )
            except UserOnboardingError as exc:
                error = str(
                    exc
                )
            else:
                # Rotate the authenticated session and CSRF token after the
                # credential transition.
                login_user(
                    user_id
                )

                return redirect(
                    url_for(
                        "web.chat"
                    )
                )

    response = make_response(
        render_template(
            "change_password.html",
            user=user,
            settings=(
                get_user_settings(
                    user_id
                )
                or {}
            ),
            error=error,
        )
    )

    return _no_store(
        response
    )
