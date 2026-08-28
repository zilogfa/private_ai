(() => {
    "use strict";

    const root = document.documentElement;

    const CACHE_KEY = (
        "private_ai_accent_color"
    );

    const HEX_PATTERN = (
        /^#[0-9a-fA-F]{6}$/
    );


    function normalizeAccent(value) {
        const normalized = String(
            value || ""
        )
        .trim()
        .toLowerCase();

        if (!normalized) {
            return null;
        }

        if (normalized === "default") {
            return "default";
        }

        if (!HEX_PATTERN.test(normalized)) {
            return null;
        }

        return normalized;
    }


    function contrastText(hexColor) {
        const hex = hexColor.slice(1);

        const red = parseInt(
            hex.slice(0, 2),
            16
        );

        const green = parseInt(
            hex.slice(2, 4),
            16
        );

        const blue = parseInt(
            hex.slice(4, 6),
            16
        );

        const luminance = (
            (
                red * 299
                + green * 587
                + blue * 114
            )
            / 1000
        );

        return (
            luminance >= 150
            ? "#171717"
            : "#ffffff"
        );
    }


    function applyAccent(
        value,
        persist = false
    ) {
        const accent = (
            normalizeAccent(value)
            || "default"
        );

        if (accent === "default") {
            root.style.removeProperty(
                "--ui-accent"
            );

            root.style.removeProperty(
                "--ui-accent-text"
            );

        } else {
            root.style.setProperty(
                "--ui-accent",
                accent
            );

            root.style.setProperty(
                "--ui-accent-text",
                contrastText(
                    accent
                )
            );
        }

        root.dataset.appliedAccent = (
            accent
        );

        if (persist) {
            try {
                localStorage.setItem(
                    CACHE_KEY,
                    accent
                );

            } catch (_) {
                // Storage may be unavailable in restricted mode.
            }
        }

        return accent;
    }


    function cachedAccent() {
        try {
            return normalizeAccent(
                localStorage.getItem(
                    CACHE_KEY
                )
            );

        } catch (_) {
            return null;
        }
    }


    const serverAccent = normalizeAccent(
        root.dataset.accentColor
    );

    const initialAccent = (
        serverAccent
        || cachedAccent()
        || "default"
    );

    applyAccent(
        initialAccent,
        Boolean(serverAccent)
    );


    function selectedPreview(
        select,
        customInput
    ) {
        if (!select) {
            return "default";
        }

        if (select.value === "custom") {
            return (
                normalizeAccent(
                    customInput?.value
                )
                || "default"
            );
        }

        return (
            normalizeAccent(
                select.value
            )
            || "default"
        );
    }


    function wireAccentControls() {
        const form = document.querySelector(
            "[data-accent-form]"
        );

        const select = document.querySelector(
            "[data-accent-select]"
        );

        const customInput = document.querySelector(
            "[data-accent-custom]"
        );

        if (!form || !select) {
            return;
        }

        const preview = () => {
            applyAccent(
                selectedPreview(
                    select,
                    customInput
                ),
                false
            );
        };

        select.addEventListener(
            "change",
            preview
        );

        if (customInput) {
            customInput.addEventListener(
                "input",
                () => {
                    select.value = "custom";
                    preview();
                }
            );
        }

        form.addEventListener(
            "submit",
            () => {
                applyAccent(
                    selectedPreview(
                        select,
                        customInput
                    ),
                    true
                );
            }
        );
    }


    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            wireAccentControls,
            {
                once: true,
            }
        );

    } else {
        wireAccentControls();
    }
})();
