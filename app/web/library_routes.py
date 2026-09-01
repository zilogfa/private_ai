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
)
from app.services.library import (
    LIBRARY_PERMISSION,
)


library_web_bp = Blueprint(
    "library_web",
    __name__,
)


@library_web_bp.get(
    "/library"
)
@permission_required(
    LIBRARY_PERMISSION
)
def library_page():
    user = get_current_user()

    settings = (
        get_user_settings(
            user[0]
        )
        or {}
    )

    return render_template(
        "library.html",
        user=user,
        settings=settings,
    )
