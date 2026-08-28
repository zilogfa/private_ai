import base64

from io import BytesIO

from PIL import (
    Image,
    ImageOps,
    UnidentifiedImageError,
)

import app.config as config

from app.ollama_client import (
    chat_stream,
)


VISION_MODEL = getattr(
    config,
    "VISION_MODEL",
    "qwen3-vl:4b-instruct-q4_K_M",
)

SHOW_VISION_ACTIVITY = getattr(
    config,
    "SHOW_VISION_ACTIVITY",
    True,
)

MAX_VISION_IMAGES = getattr(
    config,
    "MAX_VISION_IMAGES",
    4,
)

# Vision requests need more context than ordinary text requests
# because image patches are converted into visual tokens.
VISION_CONTEXT_SIZE = getattr(
    config,
    "VISION_CONTEXT_SIZE",
    8192,
)

# High-resolution camera/phone images can produce thousands of visual
# tokens. The original upload remains untouched; only the temporary
# in-memory copy sent to Ollama is resized.
MAX_VISION_EDGE = getattr(
    config,
    "MAX_VISION_EDGE",
    1536,
)

VISION_JPEG_QUALITY = getattr(
    config,
    "VISION_JPEG_QUALITY",
    90,
)


# =========================================================
# IMAGE ATTACHMENT HELPERS
# =========================================================

class VisionPreparationError(
    Exception
):
    pass


def list_image_attachments(
    attachments,
):
    return [
        attachment
        for attachment in (
            attachments or []
        )
        if attachment.get("kind") == "image"
    ]


def _absolute_attachment_path(
    attachment,
):
    relative_path = str(
        attachment.get(
            "relative_path",
            "",
        )
    ).strip()

    if not relative_path:
        raise VisionPreparationError(
            "Attachment path is missing."
        )

    upload_root = (
        config.UPLOAD_DIR.resolve()
    )

    candidate = (
        config.UPLOAD_DIR
        / relative_path
    ).resolve()

    if (
        candidate != upload_root
        and upload_root not in candidate.parents
    ):
        raise VisionPreparationError(
            "Invalid attachment path."
        )

    if not candidate.is_file():
        raise VisionPreparationError(
            "Attached image file was not found on disk."
        )

    return candidate


def _prepare_image_bytes(
    attachment,
    max_edge=None,
):
    """
    Return image bytes suitable for the local VLM.

    Small images are kept at their original resolution.
    High-resolution images are EXIF-corrected and resized in memory.
    The original uploaded file on disk is never modified.
    """

    path = _absolute_attachment_path(
        attachment
    )

    if max_edge is None:
        max_edge = MAX_VISION_EDGE

    max_edge = max(
        512,
        int(max_edge),
    )

    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(
                image
            )

            width, height = image.size

            if width <= 0 or height <= 0:
                raise VisionPreparationError(
                    "Attached image has invalid dimensions."
                )

            needs_resize = (
                max(width, height)
                > max_edge
            )

            # If the image is already a reasonable size, keep the
            # original encoded bytes. This preserves screenshot/OCR
            # detail and avoids unnecessary recompression.
            if not needs_resize:
                data = path.read_bytes()

                if not data:
                    raise VisionPreparationError(
                        "Attached image file is empty."
                    )

                return data

            image.thumbnail(
                (
                    max_edge,
                    max_edge,
                ),
                Image.Resampling.LANCZOS,
            )

            # JPEG is compact and well-supported by Ollama vision.
            # Flatten alpha onto white only for the temporary VLM copy.
            if image.mode in {
                "RGBA",
                "LA",
            }:
                background = Image.new(
                    "RGB",
                    image.size,
                    "white",
                )

                alpha = image.getchannel(
                    "A"
                )

                background.paste(
                    image.convert("RGB"),
                    mask=alpha,
                )

                image = background

            elif image.mode != "RGB":
                image = image.convert(
                    "RGB"
                )

            buffer = BytesIO()

            image.save(
                buffer,
                format="JPEG",
                quality=VISION_JPEG_QUALITY,
                optimize=True,
            )

            data = buffer.getvalue()

            if not data:
                raise VisionPreparationError(
                    "Could not prepare attached image."
                )

            return data

    except VisionPreparationError:
        raise

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as error:
        raise VisionPreparationError(
            "Could not decode the attached image."
        ) from error


def _encode_image_attachment(
    attachment,
    max_edge=None,
):
    data = _prepare_image_bytes(
        attachment,
        max_edge=max_edge,
    )

    return base64.b64encode(
        data
    ).decode("utf-8")


# =========================================================
# BUILD VISION MESSAGES
# =========================================================


def build_vision_messages(
    base_messages,
    image_attachments=None,
    other_attachments=None,
    extra_images=None,
    additional_context=None,
):
    images = list(
        image_attachments or []
    )

    extra_images = list(
        extra_images or []
    )

    visual_sources = []

    for attachment in images:
        visual_sources.append({
            "kind": "attachment",
            "name": attachment.get(
                "original_name",
                "image",
            ),
            "attachment": attachment,
        })

    for item in extra_images:
        data = item.get(
            "data"
        )

        if not data:
            continue

        visual_sources.append({
            "kind": "bytes",
            "name": item.get(
                "name",
                "document page",
            ),
            "data": data,
        })

    visual_sources = visual_sources[
        :MAX_VISION_IMAGES
    ]

    if not visual_sources:
        raise VisionPreparationError(
            "No visual attachments were provided."
        )

    visual_count = len(
        visual_sources
    )

    # Reduce per-image dimensions when several images are sent in one
    # request so the combined visual-token load remains reasonable.
    if visual_count == 1:
        image_edge = MAX_VISION_EDGE

    elif visual_count == 2:
        image_edge = min(
            MAX_VISION_EDGE,
            1280,
        )

    else:
        image_edge = min(
            MAX_VISION_EDGE,
            1024,
        )

    encoded_images = []
    image_names = []

    for source in visual_sources:
        image_names.append(
            source["name"]
        )

        if source["kind"] == "attachment":
            encoded = _encode_image_attachment(
                source["attachment"],
                max_edge=image_edge,
            )

        else:
            encoded = base64.b64encode(
                source["data"]
            ).decode("utf-8")

        encoded_images.append(
            encoded
        )

    other_attachments = list(
        other_attachments or []
    )

    messages = []

    for message in (
        base_messages or []
    ):
        messages.append(
            dict(message)
        )

    last_user_index = None

    for index in range(
        len(messages) - 1,
        -1,
        -1,
    ):
        if (
            messages[index].get(
                "role"
            )
            == "user"
        ):
            last_user_index = index
            break

    if last_user_index is None:
        raise VisionPreparationError(
            "Could not locate the latest user message."
        )

    if additional_context:
        messages.insert(
            last_user_index,
            {
                "role": "system",
                "content": str(
                    additional_context
                ),
            },
        )

        last_user_index += 1

    user_message = dict(
        messages[last_user_index]
    )

    original_text = str(
        user_message.get(
            "content",
            "",
        )
    ).strip()

    instruction_lines = [
        original_text,
        "",
        "Current visual source(s):",
        ", ".join(image_names),
        "",
        "Use the visual source(s) as context when answering.",
        "If text or visual detail is unclear, say so.",
        "If multiple visuals matter, mention their file/page names when useful.",
    ]

    if other_attachments:
        other_names = ", ".join(
            attachment[
                "original_name"
            ]
            for attachment in other_attachments
        )

        instruction_lines.extend([
            "",
            "The following attachment(s) could not be processed in the current request:",
            other_names,
            "Do not claim to have read their contents.",
        ])

    user_message["content"] = "\n".join(
        line
        for line in instruction_lines
    ).strip()

    user_message["images"] = (
        encoded_images
    )

    messages[last_user_index] = (
        user_message
    )

    return messages


# =========================================================
# VISION STREAM
# =========================================================


def stream_vision_chat(
    messages,
    timeout=900,
):
    return chat_stream(
        model=VISION_MODEL,
        messages=messages,
        options={
            "num_ctx":
                VISION_CONTEXT_SIZE,
        },
        timeout=timeout,
    )
