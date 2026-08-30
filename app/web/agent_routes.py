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


agent_web_bp = Blueprint(
    "agent_web",
    __name__,
)


@agent_web_bp.get("/agents")
@permission_required("agent.use")
def agents_page():
    user = get_current_user()
    settings = get_user_settings(user[0]) or {}

    return render_template(
        "agents.html",
        user=user,
        settings=settings,
        can_web=user_has_permission(user[0], "web_search.use"),
        can_memory=user_has_permission(user[0], "memory.manage_self"),
    )
