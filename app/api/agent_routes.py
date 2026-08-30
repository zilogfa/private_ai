from flask import (
    Blueprint,
    jsonify,
    request,
    send_file,
)

from app.auth import (
    get_current_user_id,
    permission_required,
)
from app.database import user_has_permission
from app.services.agent_engine import (
    agent_engine_status,
    start_agent_run,
)
from app.services.agents import (
    AgentStoreError,
    add_agent_input,
    create_agent_run,
    delete_agent_run,
    get_agent_artifact_path,
    get_agent_run,
    list_agent_artifacts,
    list_agent_document_sources,
    list_agent_evidence,
    list_agent_inputs,
    list_agent_runs,
    list_agent_sources,
    list_agent_steps,
    queue_agent_resume,
    request_agent_cancel,
    request_agent_pause,
)
from app.services.rag import has_indexed_documents


agent_api_bp = Blueprint(
    "agent_api",
    __name__,
    url_prefix="/api/agents",
)


def _payload():
    return request.get_json(silent=True) or {}


def _error(error, status=400):
    return jsonify({"error": str(error)}), status


def _validate_capabilities(user_id, payload):
    if bool(payload.get("allow_web")) and not user_has_permission(
        user_id,
        "web_search.use",
    ):
        raise AgentStoreError(
            "This account does not have web-search permission."
        )

    if bool(payload.get("allow_memory")) and not user_has_permission(
        user_id,
        "memory.manage_self",
    ):
        raise AgentStoreError(
            "This account does not have memory permission."
        )


def _run_detail(user_id, run_id):
    run = get_agent_run(user_id, run_id)
    if not run:
        return None

    return {
        "run": run,
        "steps": list_agent_steps(user_id, run_id),
        "sources": list_agent_sources(user_id, run_id),
        "document_sources": list_agent_document_sources(user_id, run_id),
        "evidence": list_agent_evidence(user_id, run_id),
        "artifacts": list_agent_artifacts(user_id, run_id),
        "inputs": list_agent_inputs(user_id, run_id),
    }


@agent_api_bp.get("/status")
@permission_required("agent.use")
def agent_status():
    user_id = get_current_user_id()
    return jsonify({
        "engine": agent_engine_status(),
        "capabilities": {
            "web": user_has_permission(user_id, "web_search.use"),
            "rag": has_indexed_documents(user_id),
            "memory": user_has_permission(user_id, "memory.manage_self"),
        },
    })


@agent_api_bp.get("/runs")
@permission_required("agent.use")
def agent_runs():
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50

    return jsonify({
        "runs": list_agent_runs(
            get_current_user_id(),
            limit=limit,
        )
    })


@agent_api_bp.post("/runs")
@permission_required("agent.use")
def create_run():
    user_id = get_current_user_id()
    payload = _payload()

    try:
        _validate_capabilities(user_id, payload)
        run = create_agent_run(user_id, payload)
    except AgentStoreError as error:
        return _error(error)

    start_agent_run(user_id, run["id"])
    return jsonify({"run": run}), 202


@agent_api_bp.get("/runs/<run_id>")
@permission_required("agent.use")
def agent_run_detail(run_id):
    detail = _run_detail(
        get_current_user_id(),
        run_id,
    )
    if not detail:
        return _error("Agent run was not found.", 404)
    return jsonify(detail)


@agent_api_bp.post("/runs/<run_id>/pause")
@permission_required("agent.use")
def pause_run(run_id):
    user_id = get_current_user_id()
    try:
        run = request_agent_pause(user_id, run_id)
    except AgentStoreError as error:
        return _error(error)
    return jsonify({"run": run}), 202


@agent_api_bp.post("/runs/<run_id>/cancel")
@permission_required("agent.use")
def cancel_run(run_id):
    user_id = get_current_user_id()
    try:
        run = request_agent_cancel(user_id, run_id)
    except AgentStoreError as error:
        return _error(error)
    return jsonify({"run": run}), 202


@agent_api_bp.post("/runs/<run_id>/resume")
@permission_required("agent.use")
def resume_run(run_id):
    user_id = get_current_user_id()
    try:
        run = queue_agent_resume(user_id, run_id)
    except AgentStoreError as error:
        return _error(error)

    start_agent_run(user_id, run_id)
    return jsonify({"run": run}), 202


@agent_api_bp.post("/runs/<run_id>/input")
@permission_required("agent.use")
def provide_agent_input(run_id):
    user_id = get_current_user_id()
    try:
        run = add_agent_input(
            user_id,
            run_id,
            _payload().get("content"),
        )
    except AgentStoreError as error:
        return _error(error)

    start_agent_run(user_id, run_id)
    return jsonify({"run": run}), 202


@agent_api_bp.delete("/runs/<run_id>")
@permission_required("agent.use")
def delete_run(run_id):
    user_id = get_current_user_id()
    try:
        deleted = delete_agent_run(user_id, run_id)
    except AgentStoreError as error:
        return _error(error)

    if not deleted:
        return _error("Agent run was not found.", 404)

    return jsonify({"deleted": True, "run_id": run_id})


@agent_api_bp.get("/artifacts/<artifact_id>/content")
@permission_required("agent.use")
def agent_artifact_content(artifact_id):
    artifact, path = get_agent_artifact_path(
        get_current_user_id(),
        artifact_id,
    )

    if not artifact or not path:
        return _error("Agent artifact was not found.", 404)

    # Always download artifacts in v1.9. HTML/JS/Python files are stored as
    # inert text/code and never executed from the Private AI origin.
    response = send_file(
        path,
        mimetype=artifact.get("mime_type") or "application/octet-stream",
        download_name=artifact.get("filename") or "artifact.txt",
        as_attachment=True,
        conditional=True,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response
