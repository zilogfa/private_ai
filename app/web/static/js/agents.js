(() => {
    "use strict";

    const app = document.getElementById("agentApp");
    if (!app) return;

    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const canCode = app.dataset.canCode === "1";

    const el = {
        form: document.getElementById("agentForm"),
        title: document.getElementById("agentTitle"),
        goal: document.getElementById("agentGoal"),
        model: document.getElementById("agentModelMode"),
        maxSteps: document.getElementById("agentMaxSteps"),
        allowWeb: document.getElementById("agentAllowWeb"),
        allowRag: document.getElementById("agentAllowRag"),
        allowMemory: document.getElementById("agentAllowMemory"),
        allowCode: document.getElementById("agentAllowCode"),
        sandboxRuntime: document.getElementById("agentSandboxRuntime"),
        sandboxProfile: document.getElementById("agentSandboxProfile"),
        environmentHint: document.getElementById("agentEnvironmentHint"),
        sandboxHint: document.getElementById("agentSandboxHint"),
        startButton: document.getElementById("startAgentButton"),
        refreshButton: document.getElementById("refreshAgentRunsButton"),
        runList: document.getElementById("agentRunList"),
        notice: document.getElementById("agentNotice"),
        engineStatus: document.getElementById("agentEngineStatus"),
        detailCard: document.getElementById("agentDetailCard"),
        detailTitle: document.getElementById("agentDetailTitle"),
        detailState: document.getElementById("agentDetailState"),
        detailMeta: document.getElementById("agentDetailMeta"),
        detailActions: document.getElementById("agentDetailActions"),
        detailGoal: document.getElementById("agentDetailGoal"),
        result: document.getElementById("agentResult"),
        stepList: document.getElementById("agentStepList"),
        stepCount: document.getElementById("agentStepCount"),
        evidenceList: document.getElementById("agentEvidenceList"),
        sourceList: document.getElementById("agentSourceList"),
        artifactList: document.getElementById("agentArtifactList"),
        environmentSection: document.getElementById("agentEnvironmentSection"),
        environmentState: document.getElementById("agentEnvironmentState"),
        environmentLive: document.getElementById("agentEnvironmentLive"),
        environmentDependencies: document.getElementById("agentEnvironmentDependencies"),
        environmentBuilds: document.getElementById("agentEnvironmentBuilds"),
        questionSection: document.getElementById("agentQuestionSection"),
        pendingQuestion: document.getElementById("agentPendingQuestion"),
        inputForm: document.getElementById("agentInputForm"),
        inputText: document.getElementById("agentInputText"),
        fileModal: document.getElementById("agentFileModal"),
        fileModalBackdrop: document.getElementById("agentFileModalBackdrop"),
        fileClose: document.getElementById("agentFileClose"),
        fileTitle: document.getElementById("agentFileTitle"),
        fileMeta: document.getElementById("agentFileMeta"),
        filePreviewButton: document.getElementById("agentFilePreviewButton"),
        fileDiffButton: document.getElementById("agentFileDiffButton"),
        fileDownload: document.getElementById("agentFileDownload"),
        fileRestoreButton: document.getElementById("agentFileRestoreButton"),
        fileSecurityNote: document.getElementById("agentFileSecurityNote"),
        fileRenderedPreview: document.getElementById("agentFileRenderedPreview"),
        fileSourcePreview: document.getElementById("agentFileSourcePreview"),
        fileDiffPreview: document.getElementById("agentFileDiffPreview"),
        fileVersionList: document.getElementById("agentFileVersionList"),
    };

    let selectedRunId = null;
    let pollHandle = null;
    let requestBusy = false;
    let sandboxReady = false;
    let projectEnvironmentAllowed = false;
    let runtimeStatuses = {};
    let activeFileArtifact = null;
    let activeFileVersion = null;
    let activeFileVersions = [];

    function showNotice(message, kind = "info") {
        if (!el.notice) return;
        el.notice.textContent = message;
        el.notice.dataset.kind = kind;
        el.notice.hidden = !message;
    }

    function clearStaleLoadNotice() {
        if (!el.notice || el.notice.hidden) return;
        const kind = el.notice.dataset.kind || "";
        const text = (el.notice.textContent || "").trim().toLowerCase();
        if (
            kind === "error"
            && (
                text === "load failed"
                || text.startsWith("request failed")
                || text.includes("failed to load")
            )
        ) {
            showNotice("");
        }
    }

    async function api(path, options = {}) {
        const config = {
            method: options.method || "GET",
            headers: { Accept: "application/json", ...(options.headers || {}) },
        };
        if (options.body !== undefined) {
            config.headers["Content-Type"] = "application/json";
            config.body = JSON.stringify(options.body);
        }
        if (config.method !== "GET" && config.method !== "HEAD") {
            config.headers["X-CSRF-Token"] = csrfToken;
        }
        const response = await fetch(path, config);
        let data = {};
        try { data = await response.json(); } catch (_) { data = {}; }
        if (!response.ok) {
            throw new Error(data.error || `Request failed (${response.status}).`);
        }
        return data;
    }

    function formatDate(value) {
        if (!value) return "";
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }

    function elapsedLabel(value) {
        if (!value) return "";

        const started = new Date(value);
        if (Number.isNaN(started.getTime())) return "";

        const seconds = Math.max(
            0,
            Math.round(
                (
                    Date.now()
                    - started.getTime()
                )
                / 1000
            ),
        );

        if (seconds < 60) {
            return `${seconds}s`;
        }

        const minutes = Math.floor(seconds / 60);
        const remainder = seconds % 60;

        return remainder
            ? `${minutes}m ${remainder}s`
            : `${minutes}m`;
    }

    function enhanceRenderedResult(container) {
        container
            .querySelectorAll("a")
            .forEach((link) => {
                link.target = "_blank";
                link.rel = "noopener noreferrer";
            });
    }

    function renderRichResult(run, renderedHtml) {
        el.result.classList.remove(
            "muted",
            "error-text",
            "rendered-markdown",
            "agent-result-rich",
        );

        if (run.result) {
            if (
                typeof renderedHtml === "string"
                && renderedHtml.trim()
            ) {
                el.result.innerHTML = renderedHtml;
                el.result.classList.add(
                    "rendered-markdown",
                    "agent-result-rich",
                );
                enhanceRenderedResult(el.result);
            } else {
                el.result.textContent = run.result;
            }

            return;
        }

        if (run.error) {
            el.result.textContent = run.error;
            el.result.classList.add("error-text");
            return;
        }

        el.result.textContent = (
            isActiveState(run.state)
                ? "Agent is working..."
                : "Agent has not produced a final result yet."
        );

        el.result.classList.add("muted");
    }

    function formatEnvironmentStage(value) {
        return ({
            idle: "Idle",
            validating: "Validating",
            cache_check: "Checking cache",
            cache_hit: "Cached",
            preparing: "Preparing",
            building: "Building",
            downloading: "Downloading",
            installing: "Installing",
            finalizing: "Finalizing",
            resolving: "Resolving versions",
            ready: "Ready",
            base: "Base image",
            failed: "Failed",
            timeout: "Timed out",
            stopped: "Stopped",
        })[value] || value || "Unknown";
    }

    function renderEnvironment(run, environment, builds) {
        if (!el.environmentSection) return;

        const visible = Boolean(run.allow_code);
        el.environmentSection.hidden = !visible;

        if (!visible) return;

        environment = environment || {};
        builds = builds || [];

        const profile = environment.profile || run.sandbox_profile || "strict";
        const runtime = (
            environment.runtime
            || run.effective_runtime
            || run.sandbox_runtime
            || "python"
        );
        const runtimeLabel = runtime === "node" ? "Node.js" : "Python";
        const activity = environment.activity || {};
        const activeActivity = ["running"].includes(activity.status);

        el.environmentState.className = (
            "agent-environment-state "
            + `environment-${activity.status || environment.status || "base"}`
        );

        if (profile !== "project") {
            el.environmentState.textContent = "Strict";
        } else if (activeActivity) {
            el.environmentState.textContent = formatEnvironmentStage(activity.stage);
        } else if (environment.status === "cached") {
            el.environmentState.textContent = "Cached";
        } else if (environment.ready) {
            el.environmentState.textContent = "Ready";
        } else {
            el.environmentState.textContent = formatEnvironmentStage(
                activity.stage || environment.status
            );
        }

        el.environmentLive.replaceChildren();

        const summary = document.createElement("div");
        summary.className = "agent-environment-summary";

        const title = document.createElement("strong");
        title.textContent = (
            profile === "project"
                ? `Project · ${runtimeLabel} dependencies`
                : `Strict · ${runtimeLabel}`
        );

        const detail = document.createElement("small");
        detail.textContent = (
            activeActivity
                ? (
                    activity.detail
                    || environment.message
                    || "Environment setup is running…"
                )
                : (
                    environment.message
                    || (
                        profile === "project"
                            ? `${runtimeLabel} project environment status is available.`
                            : `Strict environment uses the base ${runtimeLabel} image.`
                    )
                )
        );

        summary.append(title, detail);
        el.environmentLive.appendChild(summary);

        if (activeActivity) {
            const progressShell = document.createElement("div");
            progressShell.className = "agent-environment-progress-shell";

            const progressBar = document.createElement("div");
            progressBar.className = "agent-environment-progress-bar";
            progressBar.style.width = `${Math.max(
                2,
                Math.min(100, Number(activity.progress || 0)),
            )}%`;

            progressShell.appendChild(progressBar);
            el.environmentLive.appendChild(progressShell);

            const meta = document.createElement("div");
            meta.className = "agent-environment-live-meta";

            const percent = document.createElement("small");
            percent.textContent = `${Number(activity.progress || 0)}%`;

            const elapsed = document.createElement("small");
            const elapsedText = elapsedLabel(activity.started_at);
            elapsed.textContent = elapsedText ? `Elapsed ${elapsedText}` : "Working…";

            const cancelHint = document.createElement("small");
            cancelHint.textContent = "Stop cancels setup safely.";

            meta.append(percent, elapsed, cancelHint);
            el.environmentLive.appendChild(meta);
        }

        const image = (
            environment.execution_image
            || environment.image_tag
            || environment.current_image_tag
        );

        if (image) {
            const imageLine = document.createElement("small");
            imageLine.className = "agent-environment-image";
            imageLine.textContent = `Image: ${image}`;
            el.environmentLive.appendChild(imageLine);
        }

        el.environmentDependencies.replaceChildren();

        const dependencies = (
            environment.current_requirements
            || environment.requested_requirements
            || []
        );

        if (dependencies.length) {
            const heading = document.createElement("small");
            heading.className = "agent-environment-subheading";
            const resolvedCount = (
                environment.resolved_manifest
                || []
            ).length;

            heading.textContent = (
                resolvedCount
                    ? `Dependencies · ${resolvedCount} resolved packages`
                    : "Dependencies"
            );

            const chips = document.createElement("div");
            chips.className = "agent-environment-chips";

            for (const dependency of dependencies.slice(0, 24)) {
                const chip = document.createElement("span");
                chip.textContent = dependency;
                chips.appendChild(chip);
            }

            el.environmentDependencies.append(heading, chips);
        }

        el.environmentBuilds.replaceChildren();

        if (profile === "project" && builds.length) {
            const heading = document.createElement("small");
            heading.className = "agent-environment-subheading";
            heading.textContent = "Build history";
            el.environmentBuilds.appendChild(heading);

            for (const build of builds.slice(-3).reverse()) {
                const row = document.createElement("div");
                row.className = "agent-environment-build-row";

                const top = document.createElement("div");
                const status = document.createElement("strong");
                status.textContent = build.cached
                    ? "Cache hit"
                    : formatEnvironmentStage(build.status);

                const duration = document.createElement("small");
                const ms = Number(build.duration_ms || 0);
                duration.textContent = build.cached
                    ? "reused locally"
                    : (
                        ms >= 1000
                            ? `${(ms / 1000).toFixed(1)}s`
                            : `${ms}ms`
                    );

                top.append(status, duration);

                const tag = document.createElement("small");
                tag.textContent = build.image_tag || "";

                row.append(top, tag);
                el.environmentBuilds.appendChild(row);
            }
        }
    }

    function stateLabel(state) {
        return ({
            queued: "Queued",
            running: "Running",
            pausing: "Pausing...",
            paused: "Paused",
            waiting_input: "Needs input",
            completed: "Completed",
            failed: "Failed",
            cancelled: "Cancelled",
            interrupted: "Interrupted",
        })[state] || state || "Unknown";
    }

    function isActiveState(state) {
        return ["queued", "running", "pausing"].includes(state);
    }

    function makeButton(label, className, onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className || "secondary-button compact-button";
        button.textContent = label;
        button.addEventListener("click", onClick);
        return button;
    }

    function renderRuns(runs) {
        el.runList.replaceChildren();
        if (!runs.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty";
            empty.textContent = "No agent runs yet.";
            el.runList.appendChild(empty);
            return;
        }
        for (const run of runs) {
            const card = document.createElement("article");
            card.className = "agent-run-row";
            if (run.id === selectedRunId) card.classList.add("selected");

            const top = document.createElement("div");
            top.className = "agent-run-row-top";
            const titleWrap = document.createElement("div");
            titleWrap.className = "agent-run-title-wrap";
            const title = document.createElement("strong");
            title.textContent = run.title || "Agent run";
            const meta = document.createElement("small");
            const runtimeLabel = (
                run.effective_runtime === "node"
                    ? "Node.js"
                    : (run.allow_code ? "Python" : "")
            );
            const envLabel = (
                run.allow_code
                    ? ` · ${runtimeLabel} ${run.sandbox_profile === "project" ? "project" : "strict"}`
                    : ""
            );
            meta.textContent = `${run.current_step || 0}/${run.max_steps || 0} steps · ${run.model_mode || "auto"}${envLabel}`;
            titleWrap.append(title, meta);
            const state = document.createElement("span");
            state.className = `agent-state-pill state-${run.state || "unknown"}`;
            state.textContent = stateLabel(run.state);
            top.append(titleWrap, state);

            const goal = document.createElement("div");
            goal.className = "agent-run-goal";
            goal.textContent = run.goal || "";

            const footer = document.createElement("div");
            footer.className = "agent-run-footer";
            const time = document.createElement("small");
            time.textContent = formatDate(run.updated_at || run.created_at);
            const open = makeButton("Open", "secondary-button compact-button", () => selectRun(run.id));
            footer.append(time, open);
            card.append(top, goal, footer);
            card.addEventListener("dblclick", () => selectRun(run.id));
            el.runList.appendChild(card);
        }
    }

    function renderDetailActions(run) {
        el.detailActions.replaceChildren();
        if (run.state === "running" || run.state === "queued") {
            el.detailActions.appendChild(makeButton("Pause", "secondary-button compact-button", () => pauseRun(run.id)));
            el.detailActions.appendChild(makeButton("Stop", "danger-button compact-button", () => cancelRun(run.id)));
            return;
        }
        if (run.state === "pausing") {
            el.detailActions.appendChild(makeButton("Stop", "danger-button compact-button", () => cancelRun(run.id)));
            return;
        }
        if (["paused", "interrupted", "cancelled", "failed"].includes(run.state)) {
            el.detailActions.appendChild(makeButton("Resume", "primary-button compact-button", () => resumeRun(run.id)));
        }
        if (!["running", "queued", "pausing"].includes(run.state)) {
            el.detailActions.appendChild(makeButton("Delete", "danger-button compact-button", () => deleteRun(run.id)));
        }
    }

    function renderSteps(steps) {
        el.stepList.replaceChildren();
        el.stepCount.textContent = `${steps.length} recorded`;
        if (!steps.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty";
            empty.textContent = "The agent has not completed a step yet.";
            el.stepList.appendChild(empty);
            return;
        }
        for (const step of steps) {
            const item = document.createElement("article");
            item.className = "agent-step-item";
            const head = document.createElement("div");
            head.className = "agent-step-head";
            const name = document.createElement("strong");
            name.textContent = `Step ${step.step_index} · ${step.action || step.phase || "action"}`;
            const status = document.createElement("span");
            status.className = `agent-step-status step-${step.status || "unknown"}`;
            status.textContent = step.status || "";
            head.append(name, status);
            const reason = document.createElement("div");
            reason.className = "agent-step-reason";
            reason.textContent = step.reason || "";
            const output = document.createElement("pre");
            output.className = "agent-step-output";
            output.textContent = step.output || "Working...";
            item.append(head);
            if (reason.textContent) item.appendChild(reason);
            item.appendChild(output);
            el.stepList.appendChild(item);
        }
    }

    function renderEvidence(items) {
        el.evidenceList.replaceChildren();
        if (!items.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No structured evidence yet.";
            el.evidenceList.appendChild(empty);
            return;
        }
        for (const item of items) {
            const card = document.createElement("div");
            card.className = "agent-evidence-item";
            const top = document.createElement("div");
            top.className = "agent-evidence-top";
            const pill = document.createElement("span");
            pill.className = `evidence-pill evidence-${item.status || "unverified"}`;
            pill.textContent = item.status || "unverified";
            const refs = document.createElement("small");
            refs.textContent = (item.source_refs || []).join(", ");
            top.append(pill, refs);
            const claim = document.createElement("div");
            claim.className = "agent-evidence-claim";
            claim.textContent = item.claim || "";
            card.append(top, claim);
            if (item.notes) {
                const notes = document.createElement("small");
                notes.className = "agent-evidence-notes";
                notes.textContent = item.notes;
                card.appendChild(notes);
            }
            el.evidenceList.appendChild(card);
        }
    }

    function renderSources(webSources, documentSources) {
        el.sourceList.replaceChildren();
        const all = [];
        for (const source of webSources || []) {
            all.push({ key: source.source_key, title: source.title || "Web source", href: source.url || "", detail: source.domain || "web", external: true });
        }
        for (const source of documentSources || []) {
            const page = source.page_number ? ` · page ${source.page_number}` : "";
            all.push({
                key: source.source_key,
                title: source.document_name || "Document",
                href: source.attachment_id ? `/api/attachments/${encodeURIComponent(source.attachment_id)}/content` : "",
                detail: `local document${page}`,
                external: false,
            });
        }
        if (!all.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No sources recorded yet.";
            el.sourceList.appendChild(empty);
            return;
        }
        for (const source of all) {
            const row = document.createElement(source.href ? "a" : "div");
            row.className = "agent-source-item";
            if (source.href) {
                row.href = source.href;
                if (source.external) {
                    row.target = "_blank";
                    row.rel = "noopener noreferrer";
                }
            }
            const key = document.createElement("strong");
            key.textContent = source.key || "S";
            const text = document.createElement("span");
            text.textContent = source.title;
            const detail = document.createElement("small");
            detail.textContent = source.detail;
            row.append(key, text, detail);
            el.sourceList.appendChild(row);
        }
    }


    function fileVersionLabel(version) {
        if (!version) return "Current";
        return `v${version.version_number || "?"}`;
    }

    function closeFileModal() {
        if (!el.fileModal) return;

        el.fileModal.hidden = true;
        el.fileModal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("agent-file-modal-open");

        activeFileArtifact = null;
        activeFileVersion = null;
        activeFileVersions = [];
    }

    function setFilePreviewMode(mode) {
        const diff = mode === "diff";

        el.fileDiffPreview.hidden = !diff;

        if (!diff) {
            const hasRendered = Boolean(
                el.fileRenderedPreview.innerHTML.trim()
            );

            el.fileRenderedPreview.hidden = !hasRendered;
            el.fileSourcePreview.hidden = hasRendered;
        } else {
            el.fileRenderedPreview.hidden = true;
            el.fileSourcePreview.hidden = true;
        }
    }

    function renderFileVersionList() {
        el.fileVersionList.replaceChildren();

        if (!activeFileVersions.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No version history yet.";
            el.fileVersionList.appendChild(empty);
            return;
        }

        const latestId = activeFileVersions[0]?.id;

        for (const version of activeFileVersions) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "agent-file-version-row";

            if (activeFileVersion?.id === version.id) {
                button.classList.add("selected");
            }

            const top = document.createElement("div");
            const label = document.createElement("strong");
            label.textContent = fileVersionLabel(version);

            const badge = document.createElement("span");
            badge.textContent = (
                version.id === latestId
                    ? "Current"
                    : (version.source || "snapshot")
            );

            top.append(label, badge);

            const detail = document.createElement("small");
            detail.textContent = formatDate(version.created_at);

            const note = document.createElement("small");
            note.textContent = (
                version.note
                || `${Math.max(
                    1,
                    Math.round((version.size_bytes || 0) / 1024),
                )} KB`
            );

            button.append(top, detail, note);

            button.addEventListener("click", () => {
                loadFileVersion(version.id);
            });

            el.fileVersionList.appendChild(button);
        }
    }

    function renderFilePreview(data) {
        const artifact = data.artifact || activeFileArtifact || {};

        activeFileArtifact = artifact;
        activeFileVersion = (
            data.selected_version
            || data.current_version
            || null
        );

        el.fileTitle.textContent = artifact.filename || "File preview";

        const versionText = activeFileVersion
            ? fileVersionLabel(activeFileVersion)
            : "Current";

        el.fileMeta.textContent = [
            versionText,
            `${Math.max(
                1,
                Math.round((artifact.size_bytes || 0) / 1024),
            )} KB`,
            data.is_current ? "current workspace" : "historical snapshot",
        ].join(" · ");

        el.fileSecurityNote.hidden = !data.security_note;
        el.fileSecurityNote.textContent = data.security_note || "";

        el.fileRenderedPreview.innerHTML = "";
        el.fileSourcePreview.textContent = data.text || "";

        if (
            data.preview_mode === "markdown"
            && typeof data.rendered_html === "string"
            && data.rendered_html.trim()
        ) {
            el.fileRenderedPreview.innerHTML = data.rendered_html;
            enhanceRenderedResult(el.fileRenderedPreview);
        }

        el.fileDiffPreview.textContent = "";
        setFilePreviewMode("preview");

        const historical = Boolean(
            !data.is_current
            && activeFileVersion
        );

        el.fileDiffButton.hidden = !historical;
        el.fileRestoreButton.hidden = !historical;

        if (historical) {
            el.fileDownload.href = (
                `/api/agents/artifacts/${encodeURIComponent(artifact.id)}`
                + `/versions/${encodeURIComponent(activeFileVersion.id)}/content`
            );
        } else {
            el.fileDownload.href = (
                `/api/agents/artifacts/${encodeURIComponent(artifact.id)}/content`
            );
        }

        renderFileVersionList();
    }

    async function loadFileVersion(versionId) {
        if (!activeFileArtifact) return;

        try {
            const data = await api(
                `/api/agents/artifacts/${encodeURIComponent(activeFileArtifact.id)}`
                + `/versions/${encodeURIComponent(versionId)}/preview`
            );

            renderFilePreview(data);
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function openFilePreview(item) {
        if (!item?.id || !el.fileModal) return;

        activeFileArtifact = item;
        activeFileVersion = null;
        activeFileVersions = [];

        el.fileTitle.textContent = item.filename || "File preview";
        el.fileMeta.textContent = "Loading...";
        el.fileVersionList.replaceChildren();
        el.fileSourcePreview.textContent = "Loading preview...";
        el.fileRenderedPreview.innerHTML = "";
        el.fileDiffPreview.textContent = "";

        el.fileModal.hidden = false;
        el.fileModal.setAttribute("aria-hidden", "false");
        document.body.classList.add("agent-file-modal-open");

        try {
            const [preview, versionsData] = await Promise.all([
                api(
                    `/api/agents/artifacts/${encodeURIComponent(item.id)}/preview`
                ),
                api(
                    `/api/agents/artifacts/${encodeURIComponent(item.id)}/versions`
                ),
            ]);

            activeFileVersions = versionsData.versions || [];
            renderFilePreview(preview);
        } catch (error) {
            el.fileSourcePreview.textContent = error.message;
            el.fileMeta.textContent = "Preview unavailable";
        }
    }

    async function compareActiveFileVersion() {
        if (!activeFileArtifact || !activeFileVersion) return;

        try {
            const result = await api(
                `/api/agents/artifacts/${encodeURIComponent(activeFileArtifact.id)}`
                + `/versions/${encodeURIComponent(activeFileVersion.id)}/diff`
            );

            el.fileDiffPreview.textContent = (
                result.changed
                    ? result.diff
                    : "No differences from the current workspace file."
            );

            setFilePreviewMode("diff");
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function restoreActiveFileVersion() {
        if (!activeFileArtifact || !activeFileVersion) return;

        const label = fileVersionLabel(activeFileVersion);

        if (!window.confirm(
            `Restore ${activeFileArtifact.filename} to ${label}? `
            + "The run will be marked for re-verification."
        )) {
            return;
        }

        el.fileRestoreButton.disabled = true;
        el.fileRestoreButton.textContent = "Restoring...";

        try {
            const result = await api(
                `/api/agents/artifacts/${encodeURIComponent(activeFileArtifact.id)}`
                + `/versions/${encodeURIComponent(activeFileVersion.id)}/restore`,
                {
                    method: "POST",
                },
            );

            if (result.no_change) {
                showNotice(
                    "That version is already the current workspace file.",
                    "info",
                );
            } else {
                showNotice(
                    "File restored. The Agent run now requires re-verification.",
                    "success",
                );
            }

            closeFileModal();

            if (selectedRunId) {
                await loadRunDetail(selectedRunId);
                await loadRuns();
            }
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            el.fileRestoreButton.disabled = false;
            el.fileRestoreButton.textContent = "Restore this version";
        }
    }

    function visibleArtifacts(items) {
        const latestWorkspace = new Map();
        const other = [];
        for (const item of items || []) {
            if (item.kind === "workspace_file") {
                latestWorkspace.set(item.filename || item.id, item);
            } else {
                other.push(item);
            }
        }
        return [...latestWorkspace.values(), ...other];
    }

    function renderArtifacts(items) {
        el.artifactList.replaceChildren();
        const visible = visibleArtifacts(items);

        if (!visible.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No files created yet.";
            el.artifactList.appendChild(empty);
            return;
        }

        for (const item of visible) {
            const row = document.createElement("div");
            row.className = "agent-artifact-item agent-artifact-file-row";

            const info = document.createElement("div");
            info.className = "agent-artifact-file-info";

            const name = document.createElement("strong");
            name.textContent = item.filename || "artifact";

            const meta = document.createElement("small");
            const kb = Math.max(
                1,
                Math.round((item.size_bytes || 0) / 1024),
            );

            const versions = Number(item.version_count || 0);

            meta.textContent = [
                item.kind || "artifact",
                `${kb} KB`,
                (
                    item.kind === "workspace_file" && versions
                        ? `${versions} version${versions === 1 ? "" : "s"}`
                        : ""
                ),
            ].filter(Boolean).join(" · ");

            info.append(name, meta);

            const actions = document.createElement("div");
            actions.className = "agent-artifact-file-actions";

            if (item.kind === "workspace_file") {
                actions.appendChild(
                    makeButton(
                        "Preview",
                        "secondary-button compact-button",
                        () => openFilePreview(item),
                    )
                );
            }

            const download = document.createElement("a");
            download.className = "secondary-button compact-button";
            download.href = (
                `/api/agents/artifacts/${encodeURIComponent(item.id)}/content`
            );
            download.textContent = "Download";

            actions.appendChild(download);
            row.append(info, actions);
            el.artifactList.appendChild(row);
        }
    }

    function renderDetail(data) {
        const run = data.run;
        if (!run) {
            el.detailCard.hidden = true;
            return;
        }
        selectedRunId = run.id;
        el.detailCard.hidden = false;
        el.detailTitle.textContent = run.title || "Agent run";
        el.detailState.textContent = stateLabel(run.state);
        el.detailState.className = `agent-state-pill state-${run.state || "unknown"}`;
        const runtimeLabel = (
            run.effective_runtime === "node"
                ? "Node.js"
                : (run.allow_code ? "Python" : "")
        );
        const envLabel = (
            run.allow_code
                ? ` · Docker ${runtimeLabel} ${run.sandbox_profile === "project" ? "Project" : "Strict"}`
                : ""
        );
        el.detailMeta.textContent = `${run.current_step || 0}/${run.max_steps || 0} steps · ${run.model_mode || "auto"}${envLabel} · updated ${formatDate(run.updated_at)}`;
        el.detailGoal.textContent = run.goal || "";
        renderRichResult(
            run,
            data.rendered_result_html,
        );
        const waiting = run.state === "waiting_input" && run.pending_question;
        el.questionSection.hidden = !waiting;
        el.pendingQuestion.textContent = waiting ? run.pending_question : "";
        renderDetailActions(run);
        renderSteps(data.steps || []);
        renderEvidence(data.evidence || []);
        renderSources(data.sources || [], data.document_sources || []);
        renderArtifacts(data.artifacts || []);
        renderEnvironment(
            run,
            data.project_environment || {},
            data.environment_builds || [],
        );
    }

    function selectedRuntimeStatus() {
        const selected = el.sandboxRuntime?.value || "auto";
        return runtimeStatuses[selected] || null;
    }

    function updateEnvironmentHint() {
        if (!el.environmentHint || !el.sandboxProfile) return;

        const project = el.sandboxProfile.value === "project";
        const runtime = el.sandboxRuntime?.value || "auto";
        const runtimeName = (
            runtime === "node"
                ? "Node.js"
                : (runtime === "python" ? "Python" : "Auto runtime")
        );
        const registry = runtime === "node" ? "npm" : (runtime === "python" ? "PyPI" : "the matching package registry");

        el.environmentHint.textContent = project
            ? (
                `${runtimeName} · Project: ATLAS may download sanitized dependencies `
                + `from ${registry} during an isolated setup build. Project code still `
                + "executes with network OFF."
            )
            : (
                `${runtimeName} · Strict: no dependency downloads. Uses only the selected `
                + "base image/preinstalled packages; execution network is OFF."
            );

        const status = selectedRuntimeStatus();
        if (
            el.allowCode?.checked
            && !project
            && status
            && status.id !== "auto"
            && !status.image_ready
        ) {
            el.environmentHint.textContent += ` ${status.pull_command || status.message || "Runtime image is missing."}`;
        }
    }

    async function loadStatus() {
        try {
            const data = await api("/api/agents/status");
            const engine = data.engine || {};
            const sandbox = data.sandbox || {};
            const active = engine.active_threads || 0;

            runtimeStatuses = {};
            for (const item of (data.sandbox_runtimes || [])) {
                runtimeStatuses[item.id] = item;
            }

            // Docker engine readiness is the common requirement. Individual
            // Python/Node base-image readiness is runtime-specific.
            sandboxReady = Boolean(sandbox.docker_daemon);
            projectEnvironmentAllowed = Boolean(
                data.capabilities?.project_environment
            );

            if (el.sandboxProfile) {
                const projectOption = Array.from(el.sandboxProfile.options).find(
                    (option) => option.value === "project"
                );
                if (projectOption) {
                    projectOption.disabled = !projectEnvironmentAllowed;
                }
                if (!projectEnvironmentAllowed && el.sandboxProfile.value === "project") {
                    el.sandboxProfile.value = "strict";
                }
                el.sandboxProfile.disabled = !canCode || !sandboxReady;
            }

            if (el.sandboxRuntime) {
                el.sandboxRuntime.disabled = !canCode || !sandboxReady;

                for (const option of Array.from(el.sandboxRuntime.options)) {
                    const status = runtimeStatuses[option.value];
                    if (!status || option.value === "auto") continue;
                    option.title = status.message || "";
                }
            }

            updateEnvironmentHint();

            if (el.sandboxHint) {
                const selected = selectedRuntimeStatus();
                const runtimeDetail = (
                    selected && selected.id !== "auto"
                        ? selected.message
                        : "Auto detects Python or Node.js from the goal and workspace."
                );
                el.sandboxHint.textContent = sandboxReady
                    ? `Docker sandbox engine is ready. ${runtimeDetail || ""}`
                    : (sandbox.message || "Docker sandbox is unavailable.");
            }

            if (el.allowCode) {
                el.allowCode.disabled = !canCode || !sandboxReady;
                if (!sandboxReady) el.allowCode.checked = false;
                el.allowCode.title = sandbox.message || "";
            }

            el.engineStatus.textContent = active
                ? `${active} agent active`
                : (sandboxReady ? "Workspace ready · sandbox ready" : "Workspace ready");
            el.engineStatus.dataset.active = active ? "1" : "0";
        } catch (_) {
            sandboxReady = false;
            runtimeStatuses = {};
            el.engineStatus.textContent = "Workspace status unavailable";
        }
    }

    async function loadRuns() {
        try {
            const data = await api("/api/agents/runs?limit=50");
            renderRuns(data.runs || []);
            clearStaleLoadNotice();
            return data.runs || [];
        } catch (error) {
            showNotice(error.message, "error");
            return [];
        }
    }

    async function loadRunDetail(runId) {
        if (!runId) return null;
        try {
            const data = await api(`/api/agents/runs/${encodeURIComponent(runId)}`);
            renderDetail(data);
            clearStaleLoadNotice();
            return data;
        } catch (error) {
            showNotice(error.message, "error");
            return null;
        }
    }

    async function selectRun(runId) {
        selectedRunId = runId;
        await Promise.all([loadRuns(), loadRunDetail(runId)]);
        el.detailCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    async function startAgent(event) {
        event.preventDefault();
        if (requestBusy) return;
        const goal = el.goal.value.trim();
        if (!goal) {
            showNotice("Add an agent goal first.", "error");
            return;
        }
        if (el.allowCode?.checked && !sandboxReady) {
            showNotice("The Docker sandbox engine is not ready yet.", "error");
            return;
        }

        if (
            el.allowCode?.checked
            && (el.sandboxProfile?.value || "strict") === "strict"
        ) {
            const status = selectedRuntimeStatus();
            if (
                status
                && status.id !== "auto"
                && !status.image_ready
            ) {
                showNotice(
                    status.pull_command
                        ? `Strict ${status.label} runtime needs its base image first: ${status.pull_command}`
                        : (status.message || "Selected runtime image is not ready."),
                    "error",
                );
                return;
            }
        }
        requestBusy = true;
        el.startButton.disabled = true;
        el.startButton.textContent = "Starting...";
        showNotice("Creating persistent agent workspace...", "info");
        try {
            const data = await api("/api/agents/runs", {
                method: "POST",
                body: {
                    title: el.title.value.trim(),
                    goal,
                    model_mode: el.model.value,
                    max_steps: Number(el.maxSteps.value || 6),
                    allow_web: el.allowWeb.checked,
                    allow_rag: el.allowRag.checked,
                    allow_memory: el.allowMemory.checked,
                    allow_code: Boolean(el.allowCode?.checked),
                    sandbox_runtime: (
                        el.allowCode?.checked
                            ? (el.sandboxRuntime?.value || "auto")
                            : "auto"
                    ),
                    sandbox_profile: (
                        el.allowCode?.checked
                            ? (el.sandboxProfile?.value || "strict")
                            : "strict"
                    ),
                },
            });
            el.form.reset();
            el.maxSteps.value = "6";
            el.model.value = "auto";
            if (el.sandboxRuntime) el.sandboxRuntime.value = "auto";
            if (el.sandboxProfile) el.sandboxProfile.value = "strict";
            updateEnvironmentHint();
            selectedRunId = data.run.id;
            showNotice("Agent started. You can leave this page; the run state is persisted locally.", "success");
            await Promise.all([loadRuns(), loadRunDetail(selectedRunId), loadStatus()]);
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            requestBusy = false;
            el.startButton.disabled = false;
            el.startButton.textContent = "Start agent";
        }
    }

    async function pauseRun(runId) {
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}/pause`, { method: "POST", body: {} });
            showNotice("Pause requested. The agent will stop at a safe step boundary.", "info");
            await Promise.all([loadRuns(), loadRunDetail(runId)]);
        } catch (error) { showNotice(error.message, "error"); }
    }

    async function cancelRun(runId) {
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: {} });
            showNotice("Stop requested.", "info");
            await Promise.all([loadRuns(), loadRunDetail(runId)]);
        } catch (error) { showNotice(error.message, "error"); }
    }

    async function resumeRun(runId) {
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}/resume`, { method: "POST", body: {} });
            showNotice("Agent resumed with its existing steps, sources and workspace.", "success");
            await Promise.all([loadRuns(), loadRunDetail(runId), loadStatus()]);
        } catch (error) { showNotice(error.message, "error"); }
    }

    async function deleteRun(runId) {
        if (!window.confirm("Delete this agent run and its local workspace files?")) return;
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
            if (selectedRunId === runId) {
                selectedRunId = null;
                el.detailCard.hidden = true;
            }
            showNotice("Agent run deleted.", "success");
            await loadRuns();
        } catch (error) { showNotice(error.message, "error"); }
    }

    async function submitAgentInput(event) {
        event.preventDefault();
        if (!selectedRunId) return;
        const content = el.inputText.value.trim();
        if (!content) return;
        try {
            await api(`/api/agents/runs/${encodeURIComponent(selectedRunId)}/input`, { method: "POST", body: { content } });
            el.inputText.value = "";
            showNotice("Input added. The same agent run is continuing.", "success");
            await Promise.all([loadRuns(), loadRunDetail(selectedRunId), loadStatus()]);
        } catch (error) { showNotice(error.message, "error"); }
    }

    async function poll() {
        await loadStatus();
        const runs = await loadRuns();
        if (selectedRunId) await loadRunDetail(selectedRunId);
        const anyActive = runs.some((run) => isActiveState(run.state));
        window.clearTimeout(pollHandle);
        pollHandle = window.setTimeout(poll, anyActive ? 2200 : 6000);
    }

    el.result?.addEventListener(
        "click",
        async (event) => {
            const button = event.target.closest(
                ".code-copy-button"
            );

            if (!button) return;

            const code = button
                .closest(".code-block")
                ?.querySelector(".code-content");

            if (!code) return;

            const text = code.textContent || "";

            try {
                await navigator.clipboard.writeText(text);
                button.textContent = "Copied";

                window.setTimeout(() => {
                    button.textContent = "Copy";
                }, 1200);
            } catch (_) {
                window.prompt(
                    "Copy code:",
                    text,
                );
            }
        },
    );

    el.form.addEventListener("submit", startAgent);
    el.refreshButton.addEventListener("click", async () => {
        await Promise.all([
            loadStatus(),
            loadRuns(),
            selectedRunId ? loadRunDetail(selectedRunId) : Promise.resolve(),
        ]);
    });
    el.inputForm.addEventListener("submit", submitAgentInput);
    el.sandboxRuntime?.addEventListener("change", () => {
        updateEnvironmentHint();
        loadStatus();
    });
    el.sandboxProfile?.addEventListener("change", updateEnvironmentHint);
    el.allowCode?.addEventListener("change", updateEnvironmentHint);

    el.fileClose?.addEventListener(
        "click",
        closeFileModal,
    );

    el.fileModalBackdrop?.addEventListener(
        "click",
        closeFileModal,
    );

    el.filePreviewButton?.addEventListener(
        "click",
        () => setFilePreviewMode("preview"),
    );

    el.fileDiffButton?.addEventListener(
        "click",
        compareActiveFileVersion,
    );

    el.fileRestoreButton?.addEventListener(
        "click",
        restoreActiveFileVersion,
    );

    document.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key === "Escape"
                && el.fileModal
                && !el.fileModal.hidden
            ) {
                closeFileModal();
            }
        },
    );

    updateEnvironmentHint();
    poll();
})();
