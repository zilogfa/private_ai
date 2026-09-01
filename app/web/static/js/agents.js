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
    };

    let selectedRunId = null;
    let pollHandle = null;
    let requestBusy = false;
    let sandboxReady = false;
    let projectEnvironmentAllowed = false;

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
                ? "Project · dependency-enabled"
                : "Strict · base Python"
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
                            ? "Project environment status is available."
                            : "Strict environment uses the base Python image."
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
            const envLabel = (
                run.allow_code && run.sandbox_profile === "project"
                    ? " · project env"
                    : (run.allow_code ? " · strict sandbox" : "")
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
            const link = document.createElement("a");
            link.className = "agent-artifact-item";
            link.href = `/api/agents/artifacts/${encodeURIComponent(item.id)}/content`;
            const name = document.createElement("strong");
            name.textContent = item.filename || "artifact";
            const meta = document.createElement("small");
            const kb = Math.max(1, Math.round((item.size_bytes || 0) / 1024));
            meta.textContent = `${item.kind || "artifact"} · ${kb} KB`;
            link.append(name, meta);
            el.artifactList.appendChild(link);
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
        const envLabel = (
            run.allow_code && run.sandbox_profile === "project"
                ? " · Docker Project environment"
                : (run.allow_code ? " · Docker Strict sandbox" : "")
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

    function updateEnvironmentHint() {
        if (!el.environmentHint || !el.sandboxProfile) return;

        const project = el.sandboxProfile.value === "project";
        el.environmentHint.textContent = project
            ? (
                "Project: ATLAS may download sanitized Python dependencies during an "
                + "isolated setup build. Project code still executes with network OFF."
            )
            : (
                "Strict: no dependency downloads. Uses only the base image/preinstalled "
                + "Python packages; execution network is OFF."
            );
    }

    async function loadStatus() {
        try {
            const data = await api("/api/agents/status");
            const engine = data.engine || {};
            const sandbox = data.sandbox || {};
            const active = engine.active_threads || 0;
            sandboxReady = Boolean(sandbox.ready);
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
                updateEnvironmentHint();
            }

            if (el.sandboxHint) {
                el.sandboxHint.textContent = sandbox.message || (sandboxReady ? "Docker Python sandbox is ready." : "Docker sandbox is unavailable.");
            }
            if (el.allowCode) {
                el.allowCode.disabled = !canCode || !sandboxReady;
                if (!sandboxReady) el.allowCode.checked = false;
                el.allowCode.title = sandbox.message || "";
            }
            el.engineStatus.textContent = active ? `${active} agent active` : (sandboxReady ? "Workspace ready · sandbox ready" : "Workspace ready");
            el.engineStatus.dataset.active = active ? "1" : "0";
        } catch (_) {
            sandboxReady = false;
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
            showNotice("The Docker Python sandbox is not ready yet.", "error");
            return;
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
    el.sandboxProfile?.addEventListener("change", updateEnvironmentHint);
    el.allowCode?.addEventListener("change", updateEnvironmentHint);
    updateEnvironmentHint();
    poll();
})();
