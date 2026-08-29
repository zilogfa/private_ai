import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import uuid

from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

import app.config as config


class ImageGenerationError(Exception):
    pass


class ImageGenerationUnavailableError(
    ImageGenerationError
):
    pass


IMAGE_PREFIXES = (
    "/image",
    "/imagine",
    "image:",
)

IMAGE_ID_RE = re.compile(
    r"^[a-f0-9]{32}$"
)


# =========================================================
# COMMAND PARSING
# =========================================================


def parse_image_command(message):
    """
    Return the requested image prompt for explicit image commands.

    v1.6 intentionally starts with an explicit command boundary so a normal
    chat prompt cannot accidentally launch a large local diffusion workload.
    """

    text = str(
        message or ""
    ).strip()

    lowered = text.lower()

    for prefix in IMAGE_PREFIXES:
        if (
            lowered == prefix
            or lowered.startswith(
                prefix + " "
            )
        ):
            prompt = text[
                len(prefix):
            ].strip()

            if not prompt:
                raise ImageGenerationError(
                    "Add an image description after /image."
                )

            if (
                len(prompt)
                > config.IMAGE_GENERATION_MAX_PROMPT_CHARS
            ):
                raise ImageGenerationError(
                    (
                        "The image prompt is too long. Keep it under "
                        f"{config.IMAGE_GENERATION_MAX_PROMPT_CHARS} characters."
                    )
                )

            if "\x00" in prompt:
                raise ImageGenerationError(
                    "The image prompt contains an unsupported character."
                )

            return prompt

    return None


# =========================================================
# STORAGE
# =========================================================


def _user_generated_dir(user_id):
    path = (
        config.GENERATED_DIR
        / f"user_{int(user_id)}"
    )

    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path


def get_generated_image_path(
    user_id,
    image_id,
):
    image_id = str(
        image_id or ""
    ).strip().lower()

    if not IMAGE_ID_RE.fullmatch(
        image_id
    ):
        return None

    path = (
        _user_generated_dir(
            user_id
        )
        / f"{image_id}.png"
    )

    if not path.is_file():
        return None

    return path


def _write_metadata(
    user_id,
    image_id,
    metadata,
):
    path = (
        _user_generated_dir(
            user_id
        )
        / f"{image_id}.json"
    )

    path.write_text(
        json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# =========================================================
# MEMORY PRESSURE HELPERS
# =========================================================


def _release_loaded_ollama_models():
    """
    Best-effort release of currently resident Ollama models.

    This only affects loaded model residency, not downloaded model files or
    chat data. The next chat request simply loads the needed Ollama model again.
    """

    if not (
        config
        .IMAGE_RELEASE_OLLAMA_BEFORE_GENERATION
    ):
        return

    try:
        response = requests.get(
            f"{config.OLLAMA_BASE_URL}/api/ps",
            timeout=3,
        )
        response.raise_for_status()

        loaded = (
            response.json().get(
                "models",
                [],
            )
            or []
        )

    except Exception:
        return

    for item in loaded:
        model_name = (
            item.get("name")
            or item.get("model")
            or ""
        )

        if not model_name:
            continue

        try:
            requests.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": model_name,
                    "keep_alive": 0,
                },
                timeout=15,
            )

        except Exception:
            # Image generation should still be attempted if Ollama cannot be
            # reached or a model refuses an unload request.
            pass


# =========================================================
# MFLUX EXECUTION
# =========================================================


def _resolve_mflux_cli():
    venv_candidate = (
        Path(sys.executable)
        .resolve()
        .parent
        / "mflux-generate-z-image-turbo"
    )

    if venv_candidate.is_file():
        return str(
            venv_candidate
        )

    command = shutil.which(
        "mflux-generate-z-image-turbo"
    )

    if command:
        return command

    raise ImageGenerationUnavailableError(
        (
            "MFLUX is not installed in the project environment. Run "
            "'python -m pip install -r requirements.txt' and try again."
        )
    )


def _verify_generated_png(path):
    if (
        not path.is_file()
        or path.stat().st_size <= 0
    ):
        raise ImageGenerationError(
            "The image model finished without creating an output image."
        )

    try:
        with Image.open(path) as image:
            image.verify()

    except Exception as error:
        try:
            path.unlink(
                missing_ok=True
            )
        except OSError:
            pass

        raise ImageGenerationError(
            "The generated image file could not be validated."
        ) from error


def generate_local_image(
    user_id,
    prompt,
):
    prompt = str(
        prompt or ""
    ).strip()

    if not prompt:
        raise ImageGenerationError(
            "An image prompt is required."
        )

    provider = str(
        config.IMAGE_GENERATION_PROVIDER
        or ""
    ).strip().lower()

    if provider != "mflux_z_image_turbo":
        raise ImageGenerationUnavailableError(
            (
                "Unsupported image generation provider: "
                f"{config.IMAGE_GENERATION_PROVIDER}"
            )
        )

    cli = _resolve_mflux_cli()

    image_id = uuid.uuid4().hex
    output_path = (
        _user_generated_dir(
            user_id
        )
        / f"{image_id}.png"
    )

    seed = secrets.randbelow(
        2_147_483_647
    )

    _release_loaded_ollama_models()

    command = [
        cli,
        "--model",
        config.IMAGE_GENERATION_MODEL,
        "--prompt",
        prompt,
        "--width",
        str(
            config.IMAGE_GENERATION_WIDTH
        ),
        "--height",
        str(
            config.IMAGE_GENERATION_HEIGHT
        ),
        "--seed",
        str(seed),
        "--steps",
        str(
            config.IMAGE_GENERATION_STEPS
        ),
        "--output",
        str(output_path),
    ]

    if config.IMAGE_GENERATION_LOW_RAM:
        command.append(
            "--low-ram"
        )

    environment = os.environ.copy()
    environment.setdefault(
        "HF_HUB_DISABLE_TELEMETRY",
        "1",
    )
    environment.setdefault(
        "DO_NOT_TRACK",
        "1",
    )
    environment.setdefault(
        "TOKENIZERS_PARALLELISM",
        "false",
    )

    try:
        result = subprocess.run(
            command,
            cwd=str(
                config.PROJECT_ROOT
            ),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=(
                config
                .IMAGE_GENERATION_TIMEOUT_SECONDS
            ),
            check=False,
        )

    except subprocess.TimeoutExpired as error:
        try:
            output_path.unlink(
                missing_ok=True
            )
        except OSError:
            pass

        raise ImageGenerationError(
            (
                "Local image generation timed out. The first run may need "
                "extra time to download the model."
            )
        ) from error

    except OSError as error:
        raise ImageGenerationUnavailableError(
            (
                "Could not start the local MFLUX image generator. "
                f"Details: {error}"
            )
        ) from error

    if result.returncode != 0:
        try:
            output_path.unlink(
                missing_ok=True
            )
        except OSError:
            pass

        output_lines = [
            line.strip()
            for line in str(
                result.stdout or ""
            ).splitlines()
            if line.strip()
        ]

        detail = "\n".join(
            output_lines[-8:]
        )

        if len(detail) > 1400:
            detail = detail[-1400:]

        message = (
            "Local image generation failed."
        )

        if detail:
            message += (
                " Details: "
                + detail
            )

        raise ImageGenerationError(
            message
        )

    _verify_generated_png(
        output_path
    )

    metadata = {
        "image_id": image_id,
        "user_id": int(user_id),
        "prompt": prompt,
        "provider": provider,
        "model": (
            config
            .IMAGE_GENERATION_MODEL
        ),
        "model_label": (
            config
            .IMAGE_GENERATION_MODEL_LABEL
        ),
        "width": (
            config
            .IMAGE_GENERATION_WIDTH
        ),
        "height": (
            config
            .IMAGE_GENERATION_HEIGHT
        ),
        "steps": (
            config
            .IMAGE_GENERATION_STEPS
        ),
        "seed": seed,
        "low_ram": bool(
            config
            .IMAGE_GENERATION_LOW_RAM
        ),
        "created_at": (
            datetime.now()
            .isoformat()
        ),
    }

    _write_metadata(
        user_id,
        image_id,
        metadata,
    )

    return {
        **metadata,
        "path": output_path,
        "content_url": (
            f"/api/images/{image_id}/content"
        ),
    }


# =========================================================
# CHAT PRESENTATION
# =========================================================


def format_generated_image_markdown(
    generated,
):
    url = generated[
        "content_url"
    ]

    width = generated[
        "width"
    ]
    height = generated[
        "height"
    ]
    seed = generated[
        "seed"
    ]

    return (
        f"[![Generated image]({url} \"Generated locally\")]({url})\n\n"
        "*Generated locally with Z-Image Turbo · "
        f"{width}×{height} · seed `{seed}`*"
    )
