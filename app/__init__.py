import os
import secrets

from datetime import timedelta

from flask import Flask

from app.auth import init_auth
from app.config import (
    DATA_DIR,
    GENERATED_DIR,
    UPLOAD_DIR,
    FLASK_SECRET_KEY_FILE,
    MAX_UPLOAD_BYTES,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME_DAYS,
)
from app.database import initialize_database
from app.product import PRODUCT
from app.services.attachments import initialize_attachment_storage
from app.services.automation_store import initialize_automation_storage
from app.services.notifications import initialize_notification_storage
from app.services.agents import initialize_agent_storage, recover_stale_agent_runs
from app.services.agent_sandbox import initialize_agent_sandbox_storage
from app.services.agent_environment import initialize_agent_environment_storage
from app.services.agent_node_environment import initialize_agent_node_environment_storage
from app.services.agent_runtime import initialize_agent_runtime_storage
from app.services.agent_file_versions import initialize_agent_file_version_storage
from app.services.agent_budget_upgrade import apply_agent_budget_upgrade
from app.services.library import (
    LIBRARY_MAX_UPLOAD_BYTES,
    LIBRARY_PERMISSION,
    initialize_library_storage,
)
from app.services.user_onboarding import (
    init_user_onboarding,
    initialize_user_onboarding_storage,
)
from app.services.agent_identity import (
    initialize_agent_identity_storage,
)
from app.services.agent_revision import (
    initialize_agent_revision_storage,
)
from app.services.agent_project_planner import (
    initialize_agent_project_planner_storage,
)


def _load_or_create_secret_key():
    environment_key = os.environ.get("PRIVATE_AI_SECRET_KEY")
    if environment_key:
        return environment_key

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if FLASK_SECRET_KEY_FILE.exists():
        existing_key = FLASK_SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing_key:
            return existing_key

    key = secrets.token_hex(32)
    FLASK_SECRET_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        FLASK_SECRET_KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


def create_app():
    """Flask application factory. The browser layer remains separate from AI services."""

    apply_agent_budget_upgrade()

    initialize_database()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    initialize_attachment_storage()
    initialize_automation_storage()
    initialize_notification_storage()
    initialize_agent_storage()
    initialize_agent_sandbox_storage()
    initialize_agent_environment_storage()
    initialize_agent_node_environment_storage()
    initialize_agent_runtime_storage()
    initialize_agent_file_version_storage()
    initialize_library_storage()
    initialize_user_onboarding_storage()
    initialize_agent_identity_storage()
    initialize_agent_revision_storage()
    initialize_agent_project_planner_storage()
    recover_stale_agent_runs()

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
        static_url_path="/static",
    )

    app.config.update(
        SECRET_KEY=_load_or_create_secret_key(),
        MAX_CONTENT_LENGTH=max(
            MAX_UPLOAD_BYTES,
            LIBRARY_MAX_UPLOAD_BYTES,
        ),
        SESSION_COOKIE_NAME=SESSION_COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_LIFETIME_DAYS),
    )

    init_auth(app)
    init_user_onboarding(app)

    from app.services.agent_identity_runtime import (
        apply_agent_identity_runtime,
    )

    apply_agent_identity_runtime()

    from app.web.routes import web_bp
    from app.web.automation_routes import automation_web_bp
    from app.web.agent_routes import agent_web_bp
    from app.web.library_routes import library_web_bp
    from app.web.onboarding_routes import onboarding_web_bp
    from app.web.agent_identity_routes import agent_identity_web_bp
    from app.api.routes import api_bp
    from app.api.speech_routes import speech_api_bp
    from app.api.image_routes import image_api_bp, register_image_chat_interceptor
    from app.api.automation_routes import automation_api_bp
    from app.api.agent_routes import agent_api_bp
    from app.api.resource_routes import resource_api_bp
    from app.api.agent_identity_routes import agent_identity_api_bp
    from app.api.agent_revision_routes import agent_revision_api_bp

    from app.services.agent_research_upgrade import apply_agent_research_upgrade

    apply_agent_research_upgrade()

    # Apply AFTER identity + research wrappers so revision finalization becomes
    # the outermost completion hook and sees the actual persisted final result.
    from app.services.agent_revision_runtime import (
        apply_agent_revision_runtime,
    )

    apply_agent_revision_runtime()

    register_image_chat_interceptor(app)

    app.register_blueprint(web_bp)
    app.register_blueprint(automation_web_bp)
    app.register_blueprint(agent_web_bp)
    app.register_blueprint(library_web_bp)
    app.register_blueprint(onboarding_web_bp)
    app.register_blueprint(agent_identity_web_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(speech_api_bp)
    app.register_blueprint(image_api_bp)
    app.register_blueprint(automation_api_bp)
    app.register_blueprint(agent_api_bp)
    app.register_blueprint(resource_api_bp)
    app.register_blueprint(agent_identity_api_bp)
    app.register_blueprint(agent_revision_api_bp)

    @app.context_processor
    def inject_product_context():
        from app.auth import get_current_user_id
        from app.database import user_has_permission

        user_id = get_current_user_id()

        return {
            "product": PRODUCT,
            "can_library": bool(
                user_id
                and user_has_permission(
                    user_id,
                    LIBRARY_PERMISSION,
                )
            ),
        }

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none';"
        )
        return response

    from app.services.automation_engine import start_automation_engine

    start_automation_engine()
    return app
