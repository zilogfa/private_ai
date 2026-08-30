from flask import (
    Blueprint,
    render_template,
)

from app.auth import (
    get_current_user,
    permission_required,
)
from app.database import (
    get_user_settings,
    user_has_permission,
)


automation_web_bp = Blueprint(
    "automation_web",
    __name__,
)


@automation_web_bp.get("/automations")
@permission_required("automation.use")
def automations_page():
    user = get_current_user()
    settings = (
        get_user_settings(
            user[0]
        )
        or {}
    )

    return render_template(
        "automations.html",
        user=user,
        settings=settings,
        can_web=user_has_permission(
            user[0],
            "web_search.use",
        ),
    )
