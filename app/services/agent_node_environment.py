"""
ATLAS v2.3 - dependency-aware Node.js/npm project environments.

Security model mirrors the Python Project environment:
- normal application/test execution remains network-disabled
- dependency setup is a separate opt-in Project phase
- dependency-build context contains ONLY a sanitized package manifest
- user source, Agent memory, prompts and documents are never copied into setup
- generated user npm scripts are NOT copied into the network-enabled build context
- the resulting image is content-addressed and reusable

Dependency lifecycle scripts belonging to third-party npm packages may run
inside the isolated Docker build because many legitimate Node tools require
install-time setup. That build receives no user source or private ATLAS data.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.database import get_connection
from app.services.agents import (
    AgentStoreError,
    get_agent_run,
    utc_iso,
)
from app.services.agent_environment import (
    AgentEnvironmentError,
    ENV_PROFILE_PROJECT,
    ENV_PROFILE_STRICT,
    get_agent_run_environment,
    project_environment_allowed,
    _docker_image_exists,
    _run_cancellable,
    _set_environment_activity,
)
from app.services.agent_runtime import (
    NODE_BASE_IMAGE,
    RUNTIME_NODE,
    docker_engine_status,
)
from app.services.agent_sandbox import (
    list_workspace_files,
    read_workspace_file,
    write_workspace_file,
)


NODE_ENV_IMAGE_PREFIX = os.environ.get(
    "PRIVATE_AI_AGENT_NODE_ENV_IMAGE_PREFIX",
    "atlas-node-env",
).strip().lower() or "atlas-node-env"

NODE_ENV_SETUP_TIMEOUT_SECONDS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_NODE_ENV_SETUP_TIMEOUT_SECONDS",
        "600",
    )
)

NODE_ENV_MAX_DEPENDENCIES = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_NODE_ENV_MAX_DEPENDENCIES",
        "32",
    )
)

NODE_ENV_MAX_PACKAGE_JSON_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_NODE_ENV_MAX_PACKAGE_JSON_CHARS",
        "12000",
    )
)

NODE_ENV_MAX_BUILD_LOG_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_NODE_ENV_MAX_BUILD_LOG_CHARS",
        "18000",
    )
)

_STORAGE_READY = False

_PACKAGE_NAME_RE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*$"
)

_SAFE_SPEC_RE = re.compile(
    r"^[A-Za-z0-9*.+<>=~^| !_-]+$"
)

_IMPORT_PATTERNS = (
    re.compile(
        r"\bfrom\s+['\"]([^'\"]+)['\"]",
        re.MULTILINE,
    ),
    re.compile(
        r"\bimport\s+['\"]([^'\"]+)['\"]",
        re.MULTILINE,
    ),
    re.compile(
        r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        re.MULTILINE,
    ),
    re.compile(
        r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        re.MULTILINE,
    ),
)

# Built-ins should never be added to package.json.
_NODE_BUILTINS = {
    "assert", "assert/strict", "async_hooks", "buffer", "child_process",
    "cluster", "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "http", "http2", "https", "module",
    "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib", "node:test",
}


def initialize_agent_node_environment_storage():
    global _STORAGE_READY

    if _STORAGE_READY:
        return

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_node_environments (
            run_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            manifest_hash TEXT,
            image_tag TEXT,
            status TEXT NOT NULL DEFAULT 'base',
            requested_dependencies_json TEXT NOT NULL DEFAULT '[]',
            resolved_manifest_json TEXT NOT NULL DEFAULT '[]',
            last_error TEXT,
            build_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_node_environment_builds (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            manifest_hash TEXT NOT NULL,
            base_image TEXT NOT NULL,
            image_tag TEXT NOT NULL,
            status TEXT NOT NULL,
            cached INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            requested_dependencies_json TEXT NOT NULL DEFAULT '[]',
            resolved_manifest_json TEXT NOT NULL DEFAULT '[]',
            stdout_text TEXT,
            stderr_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id)
                REFERENCES agent_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_node_environment_builds_run
        ON agent_node_environment_builds(
            run_id,
            created_at
        )
        """
    )

    conn.commit()
    conn.close()

    _STORAGE_READY = True


def _json_list(value):
    try:
        parsed = json.loads(
            value
            or "[]"
        )
    except Exception:
        return []

    return (
        parsed
        if isinstance(
            parsed,
            list,
        )
        else []
    )


def _row_to_environment(row):
    if not row:
        return None

    return {
        "run_id": row[0],
        "user_id": row[1],
        "manifest_hash": row[2],
        "image_tag": row[3],
        "status": row[4],
        "requested_requirements": _json_list(
            row[5]
        ),
        "resolved_manifest": _json_list(
            row[6]
        ),
        "last_error": row[7],
        "build_count": int(
            row[8]
            or 0
        ),
        "created_at": row[9],
        "updated_at": row[10],
    }


def _get_environment(
    user_id,
    run_id,
):
    initialize_agent_node_environment_storage()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            run_id,
            user_id,
            manifest_hash,
            image_tag,
            status,
            requested_dependencies_json,
            resolved_manifest_json,
            last_error,
            build_count,
            created_at,
            updated_at
        FROM agent_node_environments
        WHERE
            run_id = ?
            AND user_id = ?
        """,
        (
            str(run_id),
            int(user_id),
        ),
    )
    result = _row_to_environment(
        cursor.fetchone()
    )
    conn.close()

    if result:
        return result

    return {
        "run_id": str(run_id),
        "user_id": int(user_id),
        "manifest_hash": None,
        "image_tag": None,
        "status": "base",
        "requested_requirements": [],
        "resolved_manifest": [],
        "last_error": None,
        "build_count": 0,
        "created_at": None,
        "updated_at": None,
    }


def _package_json_text(
    user_id,
    run_id,
):
    names = {
        str(
            item.get(
                "filename"
            )
            or ""
        ).lower(): item.get(
            "filename"
        )
        for item in list_workspace_files(
            user_id,
            run_id,
        )
    }

    actual = names.get(
        "package.json"
    )
    if not actual:
        return ""

    return read_workspace_file(
        user_id,
        run_id,
        actual,
        max_chars=NODE_ENV_MAX_PACKAGE_JSON_CHARS,
    )


def _parse_workspace_package_json(
    user_id,
    run_id,
):
    text = _package_json_text(
        user_id,
        run_id,
    )

    if not text.strip():
        return {}

    try:
        data = json.loads(
            text
        )
    except json.JSONDecodeError as error:
        raise AgentEnvironmentError(
            f"package.json is invalid JSON: {error}"
        ) from error

    if not isinstance(
        data,
        dict,
    ):
        raise AgentEnvironmentError(
            "package.json must contain a JSON object."
        )

    return data


def _sanitize_package_name(name):
    value = str(
        name
        or ""
    ).strip()

    if not _PACKAGE_NAME_RE.fullmatch(
        value
    ):
        raise AgentEnvironmentError(
            f"Unsupported npm package name: {value[:160]}"
        )

    return value


def _sanitize_package_spec(spec):
    value = str(
        spec
        or ""
    ).strip()

    lowered = value.lower()
    forbidden = (
        "://",
        "git+",
        "git://",
        "file:",
        "workspace:",
        "link:",
        "github:",
        "bitbucket:",
        "npm:",
        "../",
        "./",
        "\\",
    )

    if (
        not value
        or len(
            value
        ) > 120
        or any(
            marker in lowered
            for marker in forbidden
        )
        or not _SAFE_SPEC_RE.fullmatch(
            value
        )
    ):
        raise AgentEnvironmentError(
            "Project npm dependencies may use registry package names and ordinary "
            "version/range/tag specifiers only. URLs, git/VCS, local paths, aliases "
            "and workspace links are not allowed in this profile."
        )

    return value


def sanitize_node_manifest(data):
    if not isinstance(
        data,
        dict,
    ):
        raise AgentEnvironmentError(
            "package.json must contain a JSON object."
        )

    output = {
        "name": "atlas-project",
        "version": "0.0.0",
        "private": True,
    }

    package_type = str(
        data.get(
            "type"
        )
        or ""
    ).strip().lower()
    if package_type in {
        "module",
        "commonjs",
    }:
        output[
            "type"
        ] = package_type

    total = 0

    for section in (
        "dependencies",
        "devDependencies",
    ):
        raw = data.get(
            section
        )
        if raw is None:
            continue
        if not isinstance(
            raw,
            dict,
        ):
            raise AgentEnvironmentError(
                f"package.json {section} must be an object."
            )

        cleaned = {}

        for name, spec in raw.items():
            package_name = _sanitize_package_name(
                name
            )
            package_spec = _sanitize_package_spec(
                spec
            )
            cleaned[
                package_name
            ] = package_spec
            total += 1

            if total > NODE_ENV_MAX_DEPENDENCIES:
                raise AgentEnvironmentError(
                    f"Node Project environment supports at most "
                    f"{NODE_ENV_MAX_DEPENDENCIES} direct dependencies."
                )

        if cleaned:
            output[
                section
            ] = dict(
                sorted(
                    cleaned.items(),
                    key=lambda item: item[0].lower(),
                )
            )

    return output


def current_node_manifest(
    user_id,
    run_id,
):
    return sanitize_node_manifest(
        _parse_workspace_package_json(
            user_id,
            run_id,
        )
    )


def _dependency_labels(manifest):
    labels = []

    for section, prefix in (
        (
            "dependencies",
            "",
        ),
        (
            "devDependencies",
            "dev:",
        ),
    ):
        for name, spec in (
            manifest.get(
                section
            )
            or {}
        ).items():
            labels.append(
                f"{prefix}{name}@{spec}"
            )

    return sorted(
        labels,
        key=str.lower,
    )


def _manifest_hash(manifest):
    payload = json.dumps(
        {
            "base_image": NODE_BASE_IMAGE,
            "manifest": manifest,
            "schema": 1,
        },
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
    ).encode(
        "utf-8"
    )

    return hashlib.sha256(
        payload
    ).hexdigest()


def _image_tag(manifest_hash):
    return (
        f"{NODE_ENV_IMAGE_PREFIX}:"
        f"{manifest_hash[:16]}"
    )


def _update_environment_state(
    user_id,
    run_id,
    manifest_hash,
    image_tag,
    status,
    requested,
    *,
    resolved=None,
    last_error=None,
    increment_build=False,
):
    initialize_agent_node_environment_storage()

    timestamp = utc_iso()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO agent_node_environments (
            run_id,
            user_id,
            manifest_hash,
            image_tag,
            status,
            requested_dependencies_json,
            resolved_manifest_json,
            last_error,
            build_count,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            manifest_hash = excluded.manifest_hash,
            image_tag = excluded.image_tag,
            status = excluded.status,
            requested_dependencies_json = excluded.requested_dependencies_json,
            resolved_manifest_json = excluded.resolved_manifest_json,
            last_error = excluded.last_error,
            build_count = (
                agent_node_environments.build_count
                + ?
            ),
            updated_at = excluded.updated_at
        """,
        (
            str(run_id),
            int(user_id),
            manifest_hash,
            image_tag,
            str(status),
            json.dumps(
                list(
                    requested
                    or []
                ),
                ensure_ascii=False,
            ),
            json.dumps(
                list(
                    resolved
                    or []
                ),
                ensure_ascii=False,
            ),
            (
                str(last_error)[-6000:]
                if last_error
                else None
            ),
            1 if increment_build else 0,
            timestamp,
            timestamp,
            1 if increment_build else 0,
        ),
    )

    conn.commit()
    conn.close()


def _record_build(
    user_id,
    run_id,
    build_id,
    manifest_hash,
    image_tag,
    status,
    cached,
    duration_ms,
    requested,
    resolved,
    stdout_text,
    stderr_text,
):
    initialize_agent_node_environment_storage()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO agent_node_environment_builds (
            id,
            run_id,
            user_id,
            manifest_hash,
            base_image,
            image_tag,
            status,
            cached,
            duration_ms,
            requested_dependencies_json,
            resolved_manifest_json,
            stdout_text,
            stderr_text,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(build_id),
            str(run_id),
            int(user_id),
            str(manifest_hash),
            NODE_BASE_IMAGE,
            str(image_tag),
            str(status),
            int(bool(cached)),
            int(duration_ms or 0),
            json.dumps(
                list(requested or []),
                ensure_ascii=False,
            ),
            json.dumps(
                list(resolved or []),
                ensure_ascii=False,
            ),
            str(stdout_text or "")[-NODE_ENV_MAX_BUILD_LOG_CHARS:],
            str(stderr_text or "")[-NODE_ENV_MAX_BUILD_LOG_CHARS:],
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()


def _freeze_manifest(image_tag):
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                str(image_tag),
                "sh",
                "-lc",
                "cd /opt/atlas && npm ls --depth=0 --json 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ):
        return []

    try:
        data = json.loads(
            result.stdout
            or "{}"
        )
    except Exception:
        return []

    dependencies = data.get(
        "dependencies"
    )
    if not isinstance(
        dependencies,
        dict,
    ):
        return []

    resolved = []
    for name, metadata in dependencies.items():
        if not isinstance(
            metadata,
            dict,
        ):
            continue
        version = str(
            metadata.get(
                "version"
            )
            or "unknown"
        )
        resolved.append(
            f"{name}=={version}"
        )

    return sorted(
        resolved,
        key=str.lower,
    )


def node_environment_status_for_run(
    user_id,
    run_id,
):
    environment = _get_environment(
        user_id,
        run_id,
    )
    shared_profile = get_agent_run_environment(
        user_id,
        run_id,
    ).get(
        "profile"
    ) or ENV_PROFILE_STRICT

    try:
        from app.services.agent_environment import get_environment_activity
        activity = get_environment_activity(
            user_id,
            run_id,
        )
    except Exception:
        activity = {
            "status": "idle",
            "stage": "idle",
            "progress": 0,
        }

    base_ready = _docker_image_exists(
        NODE_BASE_IMAGE
    )

    if shared_profile != ENV_PROFILE_PROJECT:
        return {
            **environment,
            "runtime": RUNTIME_NODE,
            "profile": ENV_PROFILE_STRICT,
            "activity": activity,
            "ready": bool(base_ready),
            "stale": not base_ready,
            "execution_image": (
                NODE_BASE_IMAGE
                if base_ready
                else None
            ),
            "current_requirements": [],
            "message": (
                "Strict Node.js sandbox: base Node image, execution network disabled."
                if base_ready
                else (
                    "Node.js base image is not installed. Run: "
                    f"docker pull {NODE_BASE_IMAGE}"
                )
            ),
        }

    try:
        manifest = current_node_manifest(
            user_id,
            run_id,
        )
    except AgentEnvironmentError as error:
        return {
            **environment,
            "runtime": RUNTIME_NODE,
            "profile": ENV_PROFILE_PROJECT,
            "activity": activity,
            "ready": False,
            "stale": True,
            "failed_current": True,
            "status": "invalid_manifest",
            "execution_image": None,
            "current_requirements": [],
            "last_error": str(error),
            "message": (
                "package.json is not valid for the controlled Node Project profile: "
                + str(error)
            ),
        }

    requested = _dependency_labels(
        manifest
    )

    if not requested:
        return {
            **environment,
            "runtime": RUNTIME_NODE,
            "profile": ENV_PROFILE_PROJECT,
            "activity": activity,
            "ready": bool(base_ready),
            "stale": not base_ready,
            "status": "base",
            "execution_image": (
                NODE_BASE_IMAGE
                if base_ready
                else None
            ),
            "current_requirements": [],
            "message": (
                "Node Project sandbox is enabled. No npm dependencies are declared; "
                "the base Node.js image will be used."
                if base_ready
                else (
                    "Node.js base image is not installed. Project setup can pull it, "
                    f"or run: docker pull {NODE_BASE_IMAGE}"
                )
            ),
        }

    manifest_hash = _manifest_hash(
        manifest
    )
    image_tag = _image_tag(
        manifest_hash
    )

    matches = (
        environment.get(
            "manifest_hash"
        ) == manifest_hash
        and environment.get(
            "image_tag"
        ) == image_tag
        and environment.get(
            "status"
        ) in {
            "ready",
            "cached",
        }
        and _docker_image_exists(
            image_tag
        )
    )

    failed_current = (
        environment.get(
            "manifest_hash"
        ) == manifest_hash
        and environment.get(
            "status"
        ) == "failed"
    )

    return {
        **environment,
        "runtime": RUNTIME_NODE,
        "profile": ENV_PROFILE_PROJECT,
        "activity": activity,
        "ready": bool(matches),
        "stale": not matches,
        "failed_current": bool(failed_current),
        "execution_image": (
            image_tag
            if matches
            else None
        ),
        "current_requirements": requested,
        "current_manifest_hash": manifest_hash,
        "current_image_tag": image_tag,
        "message": (
            "Node Project dependency image is ready; normal execution remains network-disabled."
            if matches
            else (
                "The current package.json dependency build failed. Revise the manifest."
                if failed_current
                else "Node Project dependencies require an isolated npm setup/build step."
            )
        ),
    }


def node_environment_needs_setup(
    user_id,
    run_id,
):
    if not project_environment_allowed(
        user_id,
        run_id,
    ):
        return False

    status = node_environment_status_for_run(
        user_id,
        run_id,
    )

    # v2.3.0a: a non-empty dependency list does not mean setup is still needed.
    # Once the exact manifest's image is ready/cached, execution should proceed.
    return bool(
        not status.get(
            "ready"
        )
        and not status.get(
            "failed_current"
        )
    )


def resolve_node_execution_image(
    user_id,
    run_id,
):
    status = node_environment_status_for_run(
        user_id,
        run_id,
    )

    if status.get(
        "ready"
    ):
        return (
            status.get(
                "execution_image"
            )
            or NODE_BASE_IMAGE
        )

    raise AgentEnvironmentError(
        status.get(
            "message"
        )
        or "Node.js Project dependencies are not ready."
    )


def _normalize_import_package(specifier):
    value = str(
        specifier
        or ""
    ).strip()

    if (
        not value
        or value.startswith(
            (".", "/")
        )
        or value.startswith(
            "node:"
        )
    ):
        return None

    if value in _NODE_BUILTINS:
        return None

    if value.startswith(
        "@"
    ):
        parts = value.split(
            "/"
        )
        if len(parts) >= 2:
            return "/".join(
                parts[:2]
            )
        return None

    return value.split(
        "/"
    )[0]


def static_node_dependencies(
    user_id,
    run_id,
):
    dependencies = {}

    for item in list_workspace_files(
        user_id,
        run_id,
    ):
        filename = str(
            item.get(
                "filename"
            )
            or ""
        )
        lower = filename.lower()

        if not lower.endswith(
            (
                ".js",
                ".mjs",
                ".cjs",
                ".jsx",
                ".ts",
                ".tsx",
            )
        ):
            continue

        try:
            source = read_workspace_file(
                user_id,
                run_id,
                filename,
                max_chars=120000,
            )
        except Exception:
            continue

        for pattern in _IMPORT_PATTERNS:
            for match in pattern.finditer(
                source
            ):
                package = _normalize_import_package(
                    match.group(
                        1
                    )
                )
                if not package:
                    continue

                dependencies.setdefault(
                    package.lower(),
                    {
                        "package": package,
                        "filename": filename,
                        "source": "static_import",
                    },
                )

    return list(
        dependencies.values()
    )


def _declared_node_dependency_names(
    user_id,
    run_id,
):
    try:
        manifest = current_node_manifest(
            user_id,
            run_id,
        )
    except AgentEnvironmentError:
        return set()

    names = set()
    for section in (
        "dependencies",
        "devDependencies",
    ):
        names.update(
            str(name).lower()
            for name in (
                manifest.get(
                    section
                )
                or {}
            )
        )

    return names


def undeclared_node_dependency_for_run(
    user_id,
    run_id,
):
    if not project_environment_allowed(
        user_id,
        run_id,
    ):
        return None

    declared = _declared_node_dependency_names(
        user_id,
        run_id,
    )

    for dependency in static_node_dependencies(
        user_id,
        run_id,
    ):
        if dependency[
            "package"
        ].lower() not in declared:
            return dependency

    return None


def node_manifest_needs_update(
    user_id,
    run_id,
):
    return bool(
        undeclared_node_dependency_for_run(
            user_id,
            run_id,
        )
    )


def add_missing_node_dependency_to_manifest(
    user_id,
    run_id,
):
    run = get_agent_run(
        user_id,
        run_id,
    )
    if not run:
        raise AgentStoreError(
            "Agent run was not found."
        )

    if not project_environment_allowed(
        user_id,
        run_id,
    ):
        raise AgentEnvironmentError(
            "This run is not using the dependency-enabled Project sandbox profile."
        )

    missing = undeclared_node_dependency_for_run(
        user_id,
        run_id,
    )
    if not missing:
        raise AgentEnvironmentError(
            "No undeclared npm dependency was detected in the current Node workspace."
        )

    try:
        raw = _parse_workspace_package_json(
            user_id,
            run_id,
        )
    except AgentEnvironmentError:
        raise

    if not raw:
        raw = {
            "name": "atlas-project",
            "version": "1.0.0",
            "private": True,
        }

        # ESM import syntax needs package type=module for ordinary .js files.
        try:
            source = read_workspace_file(
                user_id,
                run_id,
                missing[
                    "filename"
                ],
                max_chars=120000,
            )
        except Exception:
            source = ""

        if re.search(
            r"^\s*(?:import|export)\b",
            source,
            re.MULTILINE,
        ):
            raw[
                "type"
            ] = "module"

    dependencies = raw.get(
        "dependencies"
    )
    if not isinstance(
        dependencies,
        dict,
    ):
        dependencies = {}

    package = _sanitize_package_name(
        missing[
            "package"
        ]
    )
    dependencies[
        package
    ] = "*"
    raw[
        "dependencies"
    ] = dependencies

    # Validate the dependency subset before writing anything.
    sanitize_node_manifest(
        raw
    )

    result = write_workspace_file(
        user_id,
        run_id,
        "package.json",
        json.dumps(
            raw,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )

    return {
        "package": package,
        "source": missing.get(
            "source"
        ),
        "detected_in": missing.get(
            "filename"
        ),
        "file": result,
    }


def _classify_node_build_progress(
    line,
    current_progress=25,
):
    text = str(
        line
        or ""
    ).strip()
    lowered = text.lower()

    rules = (
        (("load build definition", "load metadata"), "preparing", "Preparing Node dependency build…", 25),
        (("load build context", "transferring context"), "preparing", "Preparing sanitized package manifest…", 32),
        (("npm http fetch", "fetching", "resolved "), "downloading", "Resolving/downloading npm packages…", 55),
        (("added ", "changed ", "up to date"), "installing", "Installing npm dependencies…", 78),
        (("exporting to image", "exporting layers"), "finalizing", "Exporting reusable Node environment…", 92),
        (("naming to ",), "finalizing", "Saving content-addressed Node environment…", 97),
    )

    for markers, stage, detail, progress in rules:
        if any(
            marker in lowered
            for marker in markers
        ):
            return (
                stage,
                detail,
                max(
                    int(current_progress or 0),
                    progress,
                ),
            )

    return (
        "building",
        text[-500:] or "Building isolated Node environment…",
        max(
            25,
            int(current_progress or 25),
        ),
    )


def setup_node_project_environment(
    user_id,
    run_id,
    cancel_check=None,
):
    initialize_agent_node_environment_storage()

    if not project_environment_allowed(
        user_id,
        run_id,
    ):
        raise AgentEnvironmentError(
            "This run is not using the dependency-enabled Project sandbox profile."
        )

    engine = docker_engine_status()
    if not engine.get(
        "ready"
    ):
        raise AgentEnvironmentError(
            engine.get(
                "message"
            )
            or "Docker sandbox is unavailable."
        )

    _set_environment_activity(
        user_id,
        run_id,
        status="running",
        stage="validating",
        detail="Validating sanitized Node package dependencies…",
        progress=5,
        started=True,
    )

    manifest = current_node_manifest(
        user_id,
        run_id,
    )
    requested = _dependency_labels(
        manifest
    )

    if not _docker_image_exists(
        NODE_BASE_IMAGE
    ):
        _set_environment_activity(
            user_id,
            run_id,
            status="running",
            stage="downloading",
            detail=f"Pulling Node.js base runtime image {NODE_BASE_IMAGE}…",
            progress=10,
        )

        pull = _run_cancellable(
            [
                "docker",
                "pull",
                NODE_BASE_IMAGE,
            ],
            NODE_ENV_SETUP_TIMEOUT_SECONDS,
            cancel_check=cancel_check,
        )

        if (
            pull.get(
                "returncode"
            ) != 0
        ):
            _set_environment_activity(
                user_id,
                run_id,
                status="failed",
                stage="failed",
                detail="Could not pull the Node.js base runtime image.",
                progress=10,
                finished=True,
            )
            raise AgentEnvironmentError(
                "Could not pull Node.js base image. "
                + str(
                    pull.get(
                        "stderr"
                    )
                    or ""
                )[-1800:]
            )

    if not requested:
        _update_environment_state(
            user_id,
            run_id,
            None,
            None,
            "base",
            [],
            resolved=[],
            last_error=None,
        )
        _set_environment_activity(
            user_id,
            run_id,
            status="ready",
            stage="base",
            detail="No npm dependencies declared; using the Node.js base image.",
            progress=100,
            finished=True,
        )
        return {
            "runtime": RUNTIME_NODE,
            "status": "base",
            "cached": True,
            "image": NODE_BASE_IMAGE,
            "requested": [],
            "resolved": [],
            "duration_ms": 0,
        }

    manifest_hash = _manifest_hash(
        manifest
    )
    image_tag = _image_tag(
        manifest_hash
    )
    build_id = uuid.uuid4().hex

    _set_environment_activity(
        user_id,
        run_id,
        build_id=build_id,
        status="running",
        stage="cache_check",
        detail="Checking the local Node environment cache…",
        progress=16,
    )

    if _docker_image_exists(
        image_tag
    ):
        resolved = _freeze_manifest(
            image_tag
        )
        _update_environment_state(
            user_id,
            run_id,
            manifest_hash,
            image_tag,
            "cached",
            requested,
            resolved=resolved,
            last_error=None,
        )
        _record_build(
            user_id,
            run_id,
            build_id,
            manifest_hash,
            image_tag,
            "cached",
            True,
            0,
            requested,
            resolved,
            "Node dependency image already existed in the local Docker cache.",
            "",
        )
        _set_environment_activity(
            user_id,
            run_id,
            build_id=build_id,
            status="cached",
            stage="cache_hit",
            detail="Reused the existing content-addressed Node project environment.",
            progress=100,
            finished=True,
        )
        return {
            "runtime": RUNTIME_NODE,
            "status": "cached",
            "cached": True,
            "image": image_tag,
            "requested": requested,
            "resolved": resolved,
            "duration_ms": 0,
        }

    _update_environment_state(
        user_id,
        run_id,
        manifest_hash,
        image_tag,
        "building",
        requested,
        resolved=[],
        last_error=None,
        increment_build=True,
    )

    build_manifest = {
        key: value
        for key, value in manifest.items()
        if key
        in {
            "name",
            "version",
            "private",
            "type",
            "dependencies",
            "devDependencies",
        }
    }

    dockerfile = (
        f"FROM {NODE_BASE_IMAGE}\n"
        "ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false "
        "NPM_CONFIG_AUDIT=false\n"
        "USER root\n"
        "WORKDIR /opt/atlas\n"
        "COPY package.json /opt/atlas/package.json\n"
        "RUN npm install --no-audit --no-fund --include=dev "
        "&& npm cache clean --force\n"
        "USER 65534:65534\n"
        "WORKDIR /runtime\n"
    )

    result = None
    current_progress = {
        "value": 22,
        "stage": "building",
        "detail": "Building isolated Node dependency image…",
    }

    with tempfile.TemporaryDirectory(
        prefix="atlas-node-env-build-"
    ) as temp_dir:
        root = Path(
            temp_dir
        )
        (
            root
            / "Dockerfile"
        ).write_text(
            dockerfile,
            encoding="utf-8",
        )
        (
            root
            / "package.json"
        ).write_text(
            json.dumps(
                build_manifest,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        command = [
            "docker",
            "build",
            "--progress=plain",
            "--network",
            "default",
            "--pull=false",
            "--tag",
            image_tag,
            "--label",
            "com.begloo.atlas.environment=node-project",
            "--label",
            f"com.begloo.atlas.manifest={manifest_hash[:16]}",
            str(root),
        ]

        def progress_callback(
            line,
            heartbeat,
        ):
            if heartbeat:
                _set_environment_activity(
                    user_id,
                    run_id,
                    build_id=build_id,
                    status="running",
                    stage=current_progress[
                        "stage"
                    ],
                    detail=current_progress[
                        "detail"
                    ],
                    progress=current_progress[
                        "value"
                    ],
                )
                return

            stage, detail, progress = _classify_node_build_progress(
                line,
                current_progress[
                    "value"
                ],
            )
            current_progress.update({
                "stage": stage,
                "detail": detail,
                "value": progress,
            })
            _set_environment_activity(
                user_id,
                run_id,
                build_id=build_id,
                status="running",
                stage=stage,
                detail=detail,
                progress=progress,
            )

        try:
            result = _run_cancellable(
                command,
                NODE_ENV_SETUP_TIMEOUT_SECONDS,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            )
        except Exception:
            _set_environment_activity(
                user_id,
                run_id,
                build_id=build_id,
                status="stopped",
                stage="stopped",
                detail="Node dependency setup was stopped before completion.",
                progress=current_progress[
                    "value"
                ],
                finished=True,
            )
            raise

    build_status = (
        "timeout"
        if result.get(
            "timed_out"
        )
        else (
            "ready"
            if result.get(
                "returncode"
            ) == 0
            else "failed"
        )
    )

    resolved = []
    if build_status == "ready":
        _set_environment_activity(
            user_id,
            run_id,
            build_id=build_id,
            status="running",
            stage="resolving",
            detail="Recording resolved npm package versions…",
            progress=97,
        )
        resolved = _freeze_manifest(
            image_tag
        )

    error_text = str(
        result.get(
            "stderr"
        )
        or result.get(
            "stdout"
        )
        or ""
    )[-6000:]

    if build_status == "timeout":
        error_text = (
            "Node dependency setup exceeded the configured timeout. "
            + error_text
        ).strip()

    _update_environment_state(
        user_id,
        run_id,
        manifest_hash,
        image_tag,
        build_status,
        requested,
        resolved=resolved,
        last_error=(
            error_text
            if build_status != "ready"
            else None
        ),
    )

    _record_build(
        user_id,
        run_id,
        build_id,
        manifest_hash,
        image_tag,
        build_status,
        False,
        result.get(
            "duration_ms"
        )
        or 0,
        requested,
        resolved,
        result.get(
            "stdout"
        )
        or "",
        result.get(
            "stderr"
        )
        or "",
    )

    _set_environment_activity(
        user_id,
        run_id,
        build_id=build_id,
        status=(
            "ready"
            if build_status == "ready"
            else build_status
        ),
        stage=(
            "ready"
            if build_status == "ready"
            else build_status
        ),
        detail=(
            "Node Project environment is ready; normal execution remains network-disabled."
            if build_status == "ready"
            else (
                "Node dependency setup timed out."
                if build_status == "timeout"
                else "Node dependency setup failed."
            )
        ),
        progress=(
            100
            if build_status == "ready"
            else max(
                1,
                int(
                    current_progress[
                        "value"
                    ]
                ),
            )
        ),
        finished=True,
    )

    if build_status != "ready":
        raise AgentEnvironmentError(
            "Node Project dependency setup failed. "
            + (
                error_text[-2200:]
                or "Docker build failed."
            )
        )

    return {
        "runtime": RUNTIME_NODE,
        "status": "ready",
        "cached": False,
        "image": image_tag,
        "requested": requested,
        "resolved": resolved,
        "duration_ms": result.get(
            "duration_ms"
        )
        or 0,
    }


def list_node_environment_builds(
    user_id,
    run_id,
    limit=20,
):
    initialize_agent_node_environment_storage()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            id,
            manifest_hash,
            base_image,
            image_tag,
            status,
            cached,
            duration_ms,
            requested_dependencies_json,
            resolved_manifest_json,
            stdout_text,
            stderr_text,
            created_at
        FROM agent_node_environment_builds
        WHERE
            run_id = ?
            AND user_id = ?
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (
            str(run_id),
            int(user_id),
            max(
                1,
                min(
                    100,
                    int(limit),
                ),
            ),
        ),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": row[0],
            "requirements_hash": row[1],
            "base_image": row[2],
            "image_tag": row[3],
            "status": row[4],
            "cached": bool(row[5]),
            "duration_ms": int(row[6] or 0),
            "requested_requirements": _json_list(row[7]),
            "resolved_manifest": _json_list(row[8]),
            "stdout": row[9],
            "stderr": row[10],
            "created_at": row[11],
            "runtime": RUNTIME_NODE,
        }
        for row in rows
    ]


def format_node_environment_observation(result):
    status = str(
        result.get(
            "status"
        )
        or "unknown"
    ).upper()

    lines = [
        "ATLAS Node Project Environment",
        f"Status: {status}",
        f"Image: {result.get('image')}",
        "Network policy: dependency setup may access the npm registry; normal project execution remains network-disabled.",
    ]

    requested = result.get(
        "requested"
    ) or []
    if requested:
        lines.append(
            "Requested dependencies: "
            + ", ".join(
                requested
            )
        )

    resolved = result.get(
        "resolved"
    ) or []
    if resolved:
        lines.append(
            "Resolved direct dependencies: "
            + ", ".join(
                resolved[:24]
            )
        )

    if result.get(
        "cached"
    ):
        lines.append(
            "Setup duration: cache hit (environment reused)."
        )
    else:
        lines.append(
            f"Setup duration: {int(result.get('duration_ms') or 0)} ms"
        )

    return "\n".join(
        lines
    )
