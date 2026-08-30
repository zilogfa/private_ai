(() => {
    "use strict";

    const app = document.getElementById("agentApp");
    if (!app) {
        return;
    }

    const csrfToken = (
        document.querySelector('meta[name="csrf-token"]')?.content
        || ""
    );

    const elements = {
        form: document.getElementById("agentForm"),
        title: document.getElementById("agentTitle"),
        goal: document.getElementById("agentGoal"),
        model: document.getElementById("agentModelMode"),
        maxSteps: document.getElementById("agentMaxSteps"),
        allowWeb: document.getElementById("agentAllowWeb"),
        allowRag: document.getElementById("agentAllowRag"),
        allowMemory: document.getElementById("agentAllowMemory"),
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
        questionSection: document.getElementById("agentQuestionSection"),
        pendingQuestion: document.getElementById("agentPendingQuestion"),
        inputForm: document.getElementById("agentInputForm"),
        inputText: document.getElementById("agentInputText"),
    };

    let selectedRunId = null;
    let pollHandle = null;
    let requestBusy = false;

    function showNotice(message, kind = "info") {
        if (!elements.notice) {
            return;
        }
        elements.notice.textContent = message;
        elements.notice.dataset.kind = kind;
        elements.notice.hidden = !message;
    }


    function clearStaleLoadNotice() {
        if (!elements.notice || elements.notice.hidden) {
            return;
        }
        const kind = elements.notice.dataset.kind || "";
        const text = (elements.notice.textContent || "").trim().toLowerCase();
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
            headers: {
                Accept: "application/json",
                ...(options.headers || {}),
            },
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
        try {
            data = await response.json();
        } catch (_) {
            data = {};
        }

        if (!response.ok) {
            throw new Error(data.error || `Request failed (${response.status}).`);
        }

        return data;
    }

    function formatDate(value) {
        if (!value) {
            return "";
        }
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return value;
        }
        return date.toLocaleString();
    }

    function stateLabel(state) {
        const labels = {
            queued: "Queued",
            running: "Running",
            pausing: "Pausing...",
            paused: "Paused",
            waiting_input: "Needs input",
            completed: "Completed",
            failed: "Failed",
            cancelled: "Cancelled",
            interrupted: "Interrupted",
        };
        return labels[state] || state || "Unknown";
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
        elements.runList.replaceChildren();

        if (!runs.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty";
            empty.textContent = "No agent runs yet.";
            elements.runList.appendChild(empty);
            return;
        }

        for (const run of runs) {
            const card = document.createElement("article");
            card.className = "agent-run-row";
            if (run.id === selectedRunId) {
                card.classList.add("selected");
            }

            const top = document.createElement("div");
            top.className = "agent-run-row-top";

            const titleWrap = document.createElement("div");
            titleWrap.className = "agent-run-title-wrap";

            const title = document.createElement("strong");
            title.textContent = run.title || "Agent run";

            const meta = document.createElement("small");
            meta.textContent = `${run.current_step || 0}/${run.max_steps || 0} steps · ${run.model_mode || "auto"}`;

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

            const open = makeButton(
                "Open",
                "secondary-button compact-button",
                () => selectRun(run.id),
            );

            footer.append(time, open);
            card.append(top, goal, footer);
            card.addEventListener("dblclick", () => selectRun(run.id));
            elements.runList.appendChild(card);
        }
    }

    function renderDetailActions(run) {
        elements.detailActions.replaceChildren();

        if (run.state === "running" || run.state === "queued") {
            elements.detailActions.appendChild(
                makeButton("Pause", "secondary-button compact-button", () => pauseRun(run.id)),
            );
            elements.detailActions.appendChild(
                makeButton("Stop", "danger-button compact-button", () => cancelRun(run.id)),
            );
            return;
        }

        if (run.state === "pausing") {
            elements.detailActions.appendChild(
                makeButton("Stop", "danger-button compact-button", () => cancelRun(run.id)),
            );
            return;
        }

        if (["paused", "interrupted", "cancelled", "failed"].includes(run.state)) {
            elements.detailActions.appendChild(
                makeButton("Resume", "primary-button compact-button", () => resumeRun(run.id)),
            );
        }

        if (!["running", "queued", "pausing"].includes(run.state)) {
            elements.detailActions.appendChild(
                makeButton("Delete", "danger-button compact-button", () => deleteRun(run.id)),
            );
        }
    }

    function renderSteps(steps) {
        elements.stepList.replaceChildren();
        elements.stepCount.textContent = `${steps.length} recorded`;

        if (!steps.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty";
            empty.textContent = "The agent has not completed a step yet.";
            elements.stepList.appendChild(empty);
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
            if (reason.textContent) {
                item.appendChild(reason);
            }
            item.appendChild(output);
            elements.stepList.appendChild(item);
        }
    }

    function renderEvidence(items) {
        elements.evidenceList.replaceChildren();
        if (!items.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No structured evidence yet.";
            elements.evidenceList.appendChild(empty);
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

            elements.evidenceList.appendChild(card);
        }
    }

    function renderSources(webSources, documentSources) {
        elements.sourceList.replaceChildren();

        const all = [];
        for (const source of webSources || []) {
            all.push({
                key: source.source_key,
                title: source.title || "Web source",
                href: source.url || "",
                detail: source.domain || "web",
                external: true,
            });
        }

        for (const source of documentSources || []) {
            const page = source.page_number ? ` · page ${source.page_number}` : "";
            all.push({
                key: source.source_key,
                title: source.document_name || "Document",
                href: source.attachment_id
                    ? `/api/attachments/${encodeURIComponent(source.attachment_id)}/content`
                    : "",
                detail: `local document${page}`,
                external: false,
            });
        }

        if (!all.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No sources recorded yet.";
            elements.sourceList.appendChild(empty);
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
            elements.sourceList.appendChild(row);
        }
    }

    function renderArtifacts(items) {
        elements.artifactList.replaceChildren();
        if (!items.length) {
            const empty = document.createElement("div");
            empty.className = "agent-empty compact";
            empty.textContent = "No files created yet.";
            elements.artifactList.appendChild(empty);
            return;
        }

        for (const item of items) {
            const link = document.createElement("a");
            link.className = "agent-artifact-item";
            link.href = `/api/agents/artifacts/${encodeURIComponent(item.id)}/content`;

            const name = document.createElement("strong");
            name.textContent = item.filename || "artifact";

            const meta = document.createElement("small");
            const kb = Math.max(1, Math.round((item.size_bytes || 0) / 1024));
            meta.textContent = `${item.kind || "artifact"} · ${kb} KB`;

            link.append(name, meta);
            elements.artifactList.appendChild(link);
        }
    }

    function renderDetail(data) {
        const run = data.run;
        if (!run) {
            elements.detailCard.hidden = true;
            return;
        }

        selectedRunId = run.id;
        elements.detailCard.hidden = false;
        elements.detailTitle.textContent = run.title || "Agent run";
        elements.detailState.textContent = stateLabel(run.state);
        elements.detailState.className = `agent-state-pill state-${run.state || "unknown"}`;
        elements.detailMeta.textContent = `${run.current_step || 0}/${run.max_steps || 0} steps · ${run.model_mode || "auto"} · updated ${formatDate(run.updated_at)}`;
        elements.detailGoal.textContent = run.goal || "";
        elements.result.classList.remove("muted", "error-text");

        if (run.result) {
            elements.result.textContent = run.result;
            elements.result.classList.remove("muted");
        } else if (run.error) {
            elements.result.textContent = run.error;
            elements.result.classList.add("error-text");
        } else {
            elements.result.textContent = isActiveState(run.state)
                ? "Agent is working..."
                : "Agent has not produced a final result yet.";
            elements.result.classList.add("muted");
        }

        const waiting = run.state === "waiting_input" && run.pending_question;
        elements.questionSection.hidden = !waiting;
        elements.pendingQuestion.textContent = waiting ? run.pending_question : "";

        renderDetailActions(run);
        renderSteps(data.steps || []);
        renderEvidence(data.evidence || []);
        renderSources(data.sources || [], data.document_sources || []);
        renderArtifacts(data.artifacts || []);
    }

    async function loadStatus() {
        try {
            const data = await api("/api/agents/status");
            const engine = data.engine || {};
            const active = engine.active_threads || 0;
            elements.engineStatus.textContent = active
                ? `${active} agent active`
                : "Workspace ready";
            elements.engineStatus.dataset.active = active ? "1" : "0";
        } catch (error) {
            elements.engineStatus.textContent = "Workspace status unavailable";
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
        if (!runId) {
            return null;
        }
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
        await Promise.all([
            loadRuns(),
            loadRunDetail(runId),
        ]);
        elements.detailCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    async function startAgent(event) {
        event.preventDefault();
        if (requestBusy) {
            return;
        }

        const goal = elements.goal.value.trim();
        if (!goal) {
            showNotice("Add an agent goal first.", "error");
            return;
        }

        requestBusy = true;
        elements.startButton.disabled = true;
        elements.startButton.textContent = "Starting...";
        showNotice("Creating persistent agent workspace...", "info");

        try {
            const data = await api("/api/agents/runs", {
                method: "POST",
                body: {
                    title: elements.title.value.trim(),
                    goal,
                    model_mode: elements.model.value,
                    max_steps: Number(elements.maxSteps.value || 6),
                    allow_web: elements.allowWeb.checked,
                    allow_rag: elements.allowRag.checked,
                    allow_memory: elements.allowMemory.checked,
                },
            });

            elements.form.reset();
            elements.maxSteps.value = "6";
            elements.model.value = "auto";
            selectedRunId = data.run.id;
            showNotice("Agent started. You can leave this page; the run state is persisted locally.", "success");
            await Promise.all([loadRuns(), loadRunDetail(selectedRunId), loadStatus()]);
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            requestBusy = false;
            elements.startButton.disabled = false;
            elements.startButton.textContent = "Start agent";
        }
    }

    async function pauseRun(runId) {
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}/pause`, {
                method: "POST",
                body: {},
            });
            showNotice("Pause requested. The agent will stop at a safe step boundary.", "info");
            await Promise.all([loadRuns(), loadRunDetail(runId)]);
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function cancelRun(runId) {
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}/cancel`, {
                method: "POST",
                body: {},
            });
            showNotice("Stop requested.", "info");
            await Promise.all([loadRuns(), loadRunDetail(runId)]);
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function resumeRun(runId) {
        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}/resume`, {
                method: "POST",
                body: {},
            });
            showNotice("Agent resumed with its existing steps, sources and workspace.", "success");
            await Promise.all([loadRuns(), loadRunDetail(runId), loadStatus()]);
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function deleteRun(runId) {
        if (!window.confirm("Delete this agent run and its local workspace files?")) {
            return;
        }

        try {
            await api(`/api/agents/runs/${encodeURIComponent(runId)}`, {
                method: "DELETE",
            });
            if (selectedRunId === runId) {
                selectedRunId = null;
                elements.detailCard.hidden = true;
            }
            showNotice("Agent run deleted.", "success");
            await loadRuns();
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function submitAgentInput(event) {
        event.preventDefault();
        if (!selectedRunId) {
            return;
        }
        const content = elements.inputText.value.trim();
        if (!content) {
            return;
        }

        try {
            await api(`/api/agents/runs/${encodeURIComponent(selectedRunId)}/input`, {
                method: "POST",
                body: { content },
            });
            elements.inputText.value = "";
            showNotice("Input added. The same agent run is continuing.", "success");
            await Promise.all([
                loadRuns(),
                loadRunDetail(selectedRunId),
                loadStatus(),
            ]);
        } catch (error) {
            showNotice(error.message, "error");
        }
    }

    async function poll() {
        await loadStatus();
        const runs = await loadRuns();
        if (selectedRunId) {
            await loadRunDetail(selectedRunId);
        }

        const anyActive = runs.some((run) => isActiveState(run.state));
        const interval = anyActive ? 2200 : 6000;
        window.clearTimeout(pollHandle);
        pollHandle = window.setTimeout(poll, interval);
    }

    elements.form.addEventListener("submit", startAgent);
    elements.refreshButton.addEventListener("click", async () => {
        await Promise.all([
            loadStatus(),
            loadRuns(),
            selectedRunId ? loadRunDetail(selectedRunId) : Promise.resolve(),
        ]);
    });
    elements.inputForm.addEventListener("submit", submitAgentInput);

    poll();
})();
