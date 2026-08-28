import re


DEFAULT_ACCENT_COLOR = "default"

ACCENT_PRESETS = (
    ("#2563eb", "Blue"),
    ("#7c3aed", "Purple"),
    ("#0f766e", "Teal"),
    ("#c2410c", "Orange"),
    ("#be123c", "Rose"),
)

_ACCENT_PATTERN = re.compile(
    r"^#[0-9a-fA-F]{6}$"
)


def normalize_accent_color(value):
    value = (
        str(value or "")
        .strip()
        .lower()
    )

    if not value:
        return None

    if value == DEFAULT_ACCENT_COLOR:
        return DEFAULT_ACCENT_COLOR

    if not _ACCENT_PATTERN.fullmatch(
        value
    ):
        return None

    return value


def resolve_accent_choice(
    choice,
    custom_color=None,
):
    choice = (
        str(choice or "")
        .strip()
        .lower()
    )

    if choice == "custom":
        return normalize_accent_color(
            custom_color
        )

    return normalize_accent_color(
        choice
    )


def accent_from_settings(settings):
    extra = (
        (settings or {})
        .get(
            "extra",
            {},
        )
        or {}
    )

    accent = normalize_accent_color(
        extra.get(
            "accent_color",
            DEFAULT_ACCENT_COLOR,
        )
    )

    return (
        accent
        or DEFAULT_ACCENT_COLOR
    )


def merge_accent_setting(
    settings,
    accent_color,
):
    accent_color = (
        normalize_accent_color(
            accent_color
        )
    )

    if not accent_color:
        return None

    extra = dict(
        (
            (settings or {})
            .get(
                "extra",
                {},
            )
            or {}
        )
    )

    extra[
        "accent_color"
    ] = accent_color

    return extra
