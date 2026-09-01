import io
import json
import zipfile

from pathlib import Path

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
from app.services.agents import (
    get_agent_artifact,
    get_agent_artifact_path,
    get_agent_run,
    list_agent_artifacts,
    list_agent_evidence,
    list_agent_steps,
)
from app.services.agent_sandbox import (
    list_agent_sandbox_executions,
)
from app.services.library import (
    INLINE_SAFE_KINDS,
    LIBRARY_PERMISSION,
    LibraryError,
    create_library_link,
    create_library_upload,
    get_library_item,
    list_library_items,
    public_library_item,
    remove_library_item,
    resolve_library_content,
    update_library_item,
)


resource_api_bp = Blueprint(
    "resource_api",
    __name__,
)


def _payload():
    return (
        request.get_json(
            silent=True
        )
        or {}
    )


def _error(
    message,
    status=400,
):
    return (
        jsonify({
            "error": str(
                message
            )
        }),
        status,
    )


@resource_api_bp.get(
    "/api/library/items"
)
@permission_required(
    LIBRARY_PERMISSION
)
def library_items():
    user_id = (
        get_current_user_id()
    )

    favorites = (
        request.args.get(
            "favorites",
            "0",
        )
        == "1"
    )

    items = list_library_items(
        user_id,
        query=request.args.get(
            "q"
        ),
        kind=request.args.get(
            "kind"
        ),
        origin=request.args.get(
            "origin"
        ),
        favorites_only=favorites,
        limit=500,
    )

    counts = {}

    for item in list_library_items(
        user_id,
        limit=1000,
    ):
        counts[
            item["kind"]
        ] = (
            counts.get(
                item["kind"],
                0,
            )
            + 1
        )

    return jsonify({
        "items": [
            public_library_item(
                item
            )
            for item
            in items
        ],
        "counts": counts,
        "total": len(
            items
        ),
    })


@resource_api_bp.post(
    "/api/library/upload"
)
@permission_required(
    LIBRARY_PERMISSION
)
def library_upload():
    file_storage = (
        request.files.get(
            "file"
        )
    )

    if not file_storage:
        return _error(
            "file_required"
        )

    try:
        item = create_library_upload(
            get_current_user_id(),
            file_storage,
        )
    except LibraryError as error:
        return _error(
            error
        )

    return (
        jsonify({
            "item":
                public_library_item(
                    item
                )
        }),
        201,
    )


@resource_api_bp.post(
    "/api/library/links"
)
@permission_required(
    LIBRARY_PERMISSION
)
def library_link():
    payload = _payload()

    try:
        item = create_library_link(
            get_current_user_id(),
            payload.get(
                "url"
            ),
            title=payload.get(
                "title"
            ),
        )
    except LibraryError as error:
        return _error(
            error
        )

    return (
        jsonify({
            "item":
                public_library_item(
                    item
                )
        }),
        201,
    )


@resource_api_bp.patch(
    "/api/library/items/<item_id>"
)
@permission_required(
    LIBRARY_PERMISSION
)
def update_item(
    item_id,
):
    payload = _payload()

    try:
        item = update_library_item(
            get_current_user_id(),
            item_id,
            favorite=(
                payload.get(
                    "favorite"
                )
                if "favorite" in payload
                else None
            ),
            name=(
                payload.get(
                    "name"
                )
                if "name" in payload
                else None
            ),
        )
    except LibraryError as error:
        return _error(
            error,
            404
            if "not found" in str(
                error
            ).lower()
            else 400,
        )

    return jsonify({
        "item":
            public_library_item(
                item
            )
    })


@resource_api_bp.delete(
    "/api/library/items/<item_id>"
)
@permission_required(
    LIBRARY_PERMISSION
)
def delete_item(
    item_id,
):
    deleted = remove_library_item(
        get_current_user_id(),
        item_id,
    )

    if not deleted:
        return _error(
            "library_item_not_found",
            404,
        )

    return jsonify({
        "deleted": True,
        "item_id": item_id,
    })


@resource_api_bp.get(
    "/api/library/items/<item_id>/content"
)
@permission_required(
    LIBRARY_PERMISSION
)
def library_content(
    item_id,
):
    item, path = (
        resolve_library_content(
            get_current_user_id(),
            item_id,
        )
    )

    if not item:
        return _error(
            "library_item_not_found",
            404,
        )

    if item.get(
        "external_url"
    ):
        return _error(
            "library_item_is_link",
            400,
        )

    if not path:
        return _error(
            "library_content_not_found",
            404,
        )

    force_download = (
        request.args.get(
            "download",
            "0",
        )
        == "1"
    )

    inline_allowed = (
        item.get(
            "kind"
        )
        in INLINE_SAFE_KINDS
    )

    response = send_file(
        path,
        mimetype=(
            item.get(
                "mime_type"
            )
            or "application/octet-stream"
        ),
        download_name=(
            item.get(
                "name"
            )
            or Path(
                path
            ).name
        ),
        as_attachment=(
            force_download
            or not inline_allowed
        ),
        conditional=True,
    )

    response.headers[
        "Cache-Control"
    ] = (
        "private, max-age=3600"
    )

    return response


def _zip_arcname(
    folder,
    filename,
    used,
):
    safe = (
        Path(
            str(
                filename
                or "artifact"
            )
        ).name
        or "artifact"
    )

    candidate = (
        f"{folder}/{safe}"
    )

    if candidate not in used:
        used.add(
            candidate
        )
        return candidate

    stem = Path(
        safe
    ).stem

    suffix = Path(
        safe
    ).suffix

    index = 2

    while True:
        candidate = (
            f"{folder}/"
            f"{stem}_{index}{suffix}"
        )

        if candidate not in used:
            used.add(
                candidate
            )
            return candidate

        index += 1


@resource_api_bp.get(
    "/api/agents/artifacts/"
    "<artifact_id>/workspace.zip"
)
@permission_required(
    "agent.use"
)
def agent_workspace_zip(
    artifact_id,
):
    user_id = (
        get_current_user_id()
    )

    artifact = get_agent_artifact(
        user_id,
        artifact_id,
    )

    if not artifact:
        return _error(
            "agent_artifact_not_found",
            404,
        )

    run_id = artifact[
        "run_id"
    ]

    run = get_agent_run(
        user_id,
        run_id,
    )

    if not run:
        return _error(
            "agent_run_not_found",
            404,
        )

    artifacts = list_agent_artifacts(
        user_id,
        run_id,
    )

    latest_workspace = {}
    extra_artifacts = []

    for item in artifacts:
        if (
            str(
                item.get(
                    "kind"
                )
                or ""
            )
            == "workspace_file"
        ):
            latest_workspace[
                item.get(
                    "filename"
                )
                or item[
                    "id"
                ]
            ] = item
        else:
            extra_artifacts.append(
                item
            )

    buffer = io.BytesIO()
    used = set()

    manifest = {
        "product": "ATLAS by BEGLOO",
        "export_type": "agent_workspace",
        "run": {
            "id": run.get(
                "id"
            ),
            "title": run.get(
                "title"
            ),
            "goal": run.get(
                "goal"
            ),
            "model_mode": run.get(
                "model_mode"
            ),
            "state": run.get(
                "state"
            ),
            "current_step": run.get(
                "current_step"
            ),
            "max_steps": run.get(
                "max_steps"
            ),
            "created_at": run.get(
                "created_at"
            ),
            "updated_at": run.get(
                "updated_at"
            ),
        },
        "workspace_files": [],
        "artifacts": [],
        "evidence": list_agent_evidence(
            user_id,
            run_id,
        ),
        "executions": list_agent_sandbox_executions(
            user_id,
            run_id,
            limit=100,
        ),
        "steps": list_agent_steps(
            user_id,
            run_id,
        ),
    }

    with zipfile.ZipFile(
        buffer,
        "w",
        compression=
            zipfile.ZIP_DEFLATED,
    ) as archive:
        for (
            filename,
            item,
        ) in latest_workspace.items():
            _artifact, path = (
                get_agent_artifact_path(
                    user_id,
                    item[
                        "id"
                    ],
                )
            )

            if not path:
                continue

            arcname = _zip_arcname(
                "workspace",
                filename,
                used,
            )

            archive.write(
                path,
                arcname,
            )

            manifest[
                "workspace_files"
            ].append({
                "filename":
                    filename,
                "size_bytes":
                    item.get(
                        "size_bytes"
                    ),
                "kind":
                    item.get(
                        "kind"
                    ),
            })

        for item in extra_artifacts:
            _artifact, path = (
                get_agent_artifact_path(
                    user_id,
                    item[
                        "id"
                    ],
                )
            )

            if not path:
                continue

            arcname = _zip_arcname(
                "artifacts",
                item.get(
                    "filename"
                )
                or item[
                    "id"
                ],
                used,
            )

            archive.write(
                path,
                arcname,
            )

            manifest[
                "artifacts"
            ].append({
                "filename":
                    item.get(
                        "filename"
                    ),
                "size_bytes":
                    item.get(
                        "size_bytes"
                    ),
                "kind":
                    item.get(
                        "kind"
                    ),
            })

        archive.writestr(
            "atlas_manifest.json",
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
        )

    buffer.seek(
        0
    )

    safe_title = (
        "".join(
            char
            if (
                char.isalnum()
                or char in {
                    "-",
                    "_",
                }
            )
            else "_"
            for char
            in str(
                run.get(
                    "title"
                )
                or "agent_workspace"
            )
        )
        .strip(
            "_"
        )
        or "agent_workspace"
    )[:80]

    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=(
            f"{safe_title}.zip"
        ),
        max_age=0,
    )
