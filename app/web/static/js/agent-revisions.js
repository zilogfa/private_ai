(() => {
    "use strict";

    const detailCard = document.getElementById(
        "agentDetailCard"
    );

    if (!detailCard) {
        return;
    }

    const csrfToken = (
        document.querySelector(
            'meta[name="csrf-token"]'
        )?.content
        || ""
    );

    let runMap = [];
    let annotatedSignature = "";
    let activeRunId = null;
    let revisionPanel = null;

    async function api(path, options = {}) {
        const config = {
            method: options.method || "GET",
            headers: {
                Accept: "application/json",
                ...(options.headers || {}),
            },
        };

        if (options.body !== undefined) {
            config.headers["Content-Type"] = "application/json";
            config.body = JSON.stringify(options.body);
        }

        if (
            config.method !== "GET"
            && config.method !== "HEAD"
        ) {
            config.headers["X-CSRF-Token"] = csrfToken;
        }

        const response = await fetch(
            path,
            config,
        );

        let data = {};

        try {
            data = await response.json();
        } catch (_) {
            data = {};
        }

        if (!response.ok) {
            throw new Error(
                data.error
                || `Request failed (${response.status}).`
            );
        }

        return data;
    }

    async function refreshRunMap() {
        try {
            const data = await api(
                "/api/agents/runs?limit=50"
            );

            runMap = data.runs || [];

            const cards = Array.from(
                document.querySelectorAll(
                    ".agent-run-row"
                )
            );

            const signature = cards
                .map(
                    (card) => (
                        (card.querySelector(
                            ".agent-run-title-wrap strong"
                        )?.textContent || "")
                        + "|"
                        + (
                            card.querySelector(
                                ".agent-run-goal"
                            )?.textContent || ""
                        )
                    )
                )
                .join("||");

            if (
                signature !== annotatedSignature
                || cards.some(
                    (card) => !card.dataset.runId
                )
            ) {
                cards.forEach(
                    (card, index) => {
                        if (runMap[index]) {
                            card.dataset.runId = (
                                runMap[index].id
                            );
                        }
                    }
                );

                annotatedSignature = signature;
            }

            const selected = document.querySelector(
                ".agent-run-row.selected"
            );

            activeRunId = (
                selected?.dataset.runId
                || null
            );

            return activeRunId;

        } catch (_) {
            return activeRunId;
        }
    }

    function stateText() {
        return (
            document.getElementById(
                "agentDetailState"
            )?.textContent
            || ""
        )
        .trim()
        .toLowerCase();
    }

    function canReviseState() {
        return [
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "paused",
        ].includes(
            stateText()
        );
    }

    function createPanel() {
        if (revisionPanel) {
            return revisionPanel;
        }

        const resultSection = (
            document.getElementById(
                "agentResult"
            )?.closest(
                ".agent-detail-section"
            )
        );

        if (!resultSection) {
            return null;
        }

        const section = document.createElement(
            "section"
        );

        section.className = (
            "agent-detail-section "
            + "atlas-revision-section"
        );

        section.innerHTML = `
            <div class="atlas-revision-heading">
                <div>
                    <h3>Continue / Request changes</h3>
                    <small>
                        Keep this same Agent, workspace, sources and execution history.
                    </small>
                </div>
                <button
                    type="button"
                    class="secondary-button compact-button"
                    data-revision-toggle
                >
                    Continue / Revise
                </button>
            </div>

            <form
                class="atlas-revision-form"
                hidden
            >
                <textarea
                    rows="4"
                    maxlength="8000"
                    placeholder="Tell the Agent what is wrong, what should change, or what to continue..."
                    required
                ></textarea>

                <div class="atlas-revision-options">
                    <label>
                        Additional step budget
                        <select>
                            <option value="6">6 · quick revision</option>
                            <option value="12" selected>12 · standard revision</option>
                            <option value="25">25 · deep revision</option>
                        </select>
                    </label>

                    <label class="atlas-revision-learn">
                        <input
                            type="checkbox"
                            checked
                        >
                        <span>
                            Let this Agent learn durable lessons from my feedback
                        </span>
                    </label>
                </div>

                <div class="atlas-revision-actions">
                    <button
                        type="submit"
                        class="primary-button"
                    >
                        Continue same run
                    </button>

                    <button
                        type="button"
                        class="secondary-button"
                        data-revision-cancel
                    >
                        Cancel
                    </button>
                </div>

                <small class="privacy-note">
                    Feedback is kept as revision provenance. Learning is optional;
                    task-specific feedback should not become permanent Agent Memory.
                </small>
            </form>

            <div
                class="atlas-revision-history"
                hidden
            ></div>
        `;

        resultSection.insertAdjacentElement(
            "afterend",
            section,
        );

        const toggle = section.querySelector(
            "[data-revision-toggle]"
        );

        const form = section.querySelector(
            ".atlas-revision-form"
        );

        const cancel = section.querySelector(
            "[data-revision-cancel]"
        );

        toggle.addEventListener(
            "click",
            () => {
                form.hidden = false;
                toggle.hidden = true;

                form.querySelector(
                    "textarea"
                ).focus();
            },
        );

        cancel.addEventListener(
            "click",
            () => {
                form.hidden = true;
                toggle.hidden = false;
                form.reset();

                const learn = form.querySelector(
                    '.atlas-revision-learn input'
                );

                if (learn) {
                    learn.checked = true;
                }
            },
        );

        form.addEventListener(
            "submit",
            async (event) => {
                event.preventDefault();

                await refreshRunMap();

                if (!activeRunId) {
                    window.alert(
                        "Could not resolve the selected Agent run. Refresh the page and try again."
                    );
                    return;
                }

                const textarea = form.querySelector(
                    "textarea"
                );

                const select = form.querySelector(
                    "select"
                );

                const learn = form.querySelector(
                    '.atlas-revision-learn input'
                );

                const feedback = (
                    textarea.value
                    || ""
                ).trim();

                if (!feedback) {
                    return;
                }

                const submit = form.querySelector(
                    'button[type="submit"]'
                );

                submit.disabled = true;
                submit.textContent = "Continuing...";

                try {
                    await api(
                        `/api/agents/runs/${encodeURIComponent(activeRunId)}/revise`,
                        {
                            method: "POST",
                            body: {
                                feedback,
                                extra_steps: Number(
                                    select.value
                                    || 12
                                ),
                                learn_from_feedback:
                                    Boolean(
                                        learn.checked
                                    ),
                            },
                        },
                    );

                    textarea.value = "";
                    form.hidden = true;
                    toggle.hidden = false;

                    window.setTimeout(
                        () => {
                            document.getElementById(
                                "refreshAgentRunsButton"
                            )?.click();
                        },
                        120,
                    );

                } catch (error) {
                    window.alert(
                        error.message
                    );
                } finally {
                    submit.disabled = false;
                    submit.textContent = (
                        "Continue same run"
                    );
                }
            },
        );

        revisionPanel = section;

        return section;
    }

    async function renderHistory(
        runId,
    ) {
        const section = createPanel();

        if (!section || !runId) {
            return;
        }

        const history = section.querySelector(
            ".atlas-revision-history"
        );

        try {
            const data = await api(
                `/api/agents/runs/${encodeURIComponent(runId)}/revisions`
            );

            const revisions = (
                data.revisions
                || []
            );

            history.replaceChildren();
            history.hidden = !revisions.length;

            if (!revisions.length) {
                return;
            }

            const heading = document.createElement(
                "div"
            );

            heading.className = (
                "atlas-revision-history-title"
            );

            heading.textContent = (
                `Revision history · ${revisions.length}`
            );

            history.appendChild(
                heading
            );

            for (
                const revision
                of revisions.slice().reverse()
            ) {
                const item = document.createElement(
                    "article"
                );

                item.className = (
                    "atlas-revision-item"
                );

                const top = document.createElement(
                    "div"
                );

                top.className = (
                    "atlas-revision-item-top"
                );

                const title = document.createElement(
                    "strong"
                );

                title.textContent = (
                    `Revision ${revision.revision_number}`
                );

                const status = document.createElement(
                    "span"
                );

                status.textContent = (
                    revision.status
                );

                top.append(
                    title,
                    status,
                );

                const feedback = document.createElement(
                    "div"
                );

                feedback.className = (
                    "atlas-revision-feedback"
                );

                feedback.textContent = (
                    revision.feedback?.content
                    || ""
                );

                const meta = document.createElement(
                    "small"
                );

                const learning = (
                    revision.feedback
                    ?.learning_status
                    || "not_requested"
                );

                meta.textContent = (
                    `steps ${revision.start_step}`
                    + (
                        revision.end_step
                            ? `–${revision.end_step}`
                            : "+"
                    )
                    + ` · learning: ${learning}`
                );

                item.append(
                    top,
                    feedback,
                    meta,
                );

                if (revision.previous_result) {
                    const previous = document.createElement(
                        "details"
                    );

                    const summary = document.createElement(
                        "summary"
                    );

                    summary.textContent = (
                        "Previous final result"
                    );

                    const pre = document.createElement(
                        "pre"
                    );

                    pre.textContent = (
                        revision.previous_result
                    );

                    previous.append(
                        summary,
                        pre,
                    );

                    item.appendChild(
                        previous
                    );
                }

                history.appendChild(
                    item
                );
            }

        } catch (_) {
            history.hidden = true;
        }
    }

    async function update() {
        await refreshRunMap();

        const section = createPanel();

        if (!section) {
            return;
        }

        section.hidden = (
            !activeRunId
            || !canReviseState()
        );

        if (
            activeRunId
            && !section.hidden
        ) {
            await renderHistory(
                activeRunId
            );
        }
    }

    const observer = new MutationObserver(
        () => {
            window.clearTimeout(
                observer._timer
            );

            observer._timer = window.setTimeout(
                update,
                100,
            );
        }
    );

    observer.observe(
        document.body,
        {
            childList: true,
            subtree: true,
            characterData: true,
        },
    );

    window.setInterval(
        update,
        3500,
    );

    update();
})();
