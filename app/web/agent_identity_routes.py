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
from app.services.agent_identity import (
    AGENT_MEMORY_PERMISSION,
)


agent_identity_web_bp = Blueprint(
    "agent_identity_web",
    __name__,
)


@agent_identity_web_bp.get(
    "/agents/identities"
)
@permission_required(
    AGENT_MEMORY_PERMISSION
)
def identities_page():
    user = get_current_user()

    return render_template(
        "agent_identities.html",
        user=user,
        settings=(
            get_user_settings(
                user[0]
            )
            or {}
        ),
    )
