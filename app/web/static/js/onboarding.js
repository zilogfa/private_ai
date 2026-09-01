(() => {
    "use strict";

    const app = document.getElementById("onboardingApp");

    if (!app) {
        return;
    }

    // The temporary password exists only in this rendered POST response.
    // Replace the visible URL so a later refresh becomes a normal GET and the
    // plaintext password disappears instead of being re-submitted.
    if (app.dataset.tempPasswordPresent === "1") {
        window.history.replaceState(
            {},
            "",
            "/admin/onboarding",
        );
    }

    const copyButton = document.getElementById(
        "copyTemporaryPasswordButton"
    );

    const password = document.getElementById(
        "temporaryPassword"
    );

    if (
        copyButton
        && password
    ) {
        copyButton.addEventListener(
            "click",
            async () => {
                const text = (
                    password.textContent
                    || ""
                ).trim();

                try {
                    await navigator.clipboard.writeText(
                        text
                    );

                    copyButton.textContent = "Copied";
                } catch (_) {
                    window.prompt(
                        "Copy temporary password:",
                        text,
                    );
                }
            },
        );
    }
})();
