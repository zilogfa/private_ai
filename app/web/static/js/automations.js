(() => {
    "use strict";

    const app = document.getElementById(
        "automationApp"
    );

    if (!app) {
        return;
    }

    const csrfToken = (
        document
        .querySelector(
            'meta[name="csrf-token"]'
        )
        ?.getAttribute("content")
        || ""
    );

    const canWeb = (
        app.dataset.canWeb === "1"
    );

    const form = document.getElementById(
        "automationForm"
    );
    const editingTaskId = document.getElementById(
        "editingTaskId"
    );
    const taskFormTitle = document.getElementById(
        "taskFormTitle"
    );
    const saveTaskButton = document.getElementById(
        "saveTaskButton"
    );
    const cancelEditButton = document.getElementById(
        "cancelEditButton"
    );
    const reviewTaskButton = document.getElementById(
        "reviewTaskButton"
    );
    const preflightPanel = document.getElementById(
        "preflightPanel"
    );
    const preflightStatus = document.getElementById(
        "preflightStatus"
    );
    const preflightModel = document.getElementById(
        "preflightModel"
    );
    const preflightSummary = document.getElementById(
        "preflightSummary"
    );
    const preflightClarification = document.getElementById(
        "preflightClarification"
    );
    const preflightTools = document.getElementById(
        "preflightTools"
    );
    const applyRecommendedToolsButton = document.getElementById(
        "applyRecommendedToolsButton"
    );

    const taskTitle = document.getElementById(
        "taskTitle"
    );
    const taskType = document.getElementById(
        "taskType"
    );
    const taskInstruction = document.getElementById(
        "taskInstruction"
    );
    const conditionField = document.getElementById(
        "conditionField"
    );
    const conditionText = document.getElementById(
        "conditionText"
    );

    const taskTimezone = document.getElementById(
        "taskTimezone"
    );
    const timezoneLabel = document.getElementById(
        "timezoneLabel"
    );
    const scheduleKind = document.getElementById(
        "scheduleKind"
    );
    const scheduleOnce = document.getElementById(
        "scheduleOnce"
    );
    const scheduleInterval = document.getElementById(
        "scheduleInterval"
    );
    const scheduleDaily = document.getElementById(
        "scheduleDaily"
    );
    const scheduleWeekly = document.getElementById(
        "scheduleWeekly"
    );

    const onceRunAt = document.getElementById(
        "onceRunAt"
    );
    const intervalEvery = document.getElementById(
        "intervalEvery"
    );
    const intervalUnit = document.getElementById(
        "intervalUnit"
    );
    const intervalFirstRun = document.getElementById(
        "intervalFirstRun"
    );
    const dailyEvery = document.getElementById(
        "dailyEvery"
    );
    const dailyTime = document.getElementById(
        "dailyTime"
    );
    const weeklyEvery = document.getElementById(
        "weeklyEvery"
    );
    const weeklyDay = document.getElementById(
        "weeklyDay"
    );
    const weeklyTime = document.getElementById(
        "weeklyTime"
    );

    const aiOptions = document.getElementById(
        "aiOptions"
    );
    const taskModelMode = document.getElementById(
        "taskModelMode"
    );
    const allowRag = document.getElementById(
        "allowRag"
    );
    const allowMemory = document.getElementById(
        "allowMemory"
    );
    const allowWeb = document.getElementById(
        "allowWeb"
    );
    const notifyChangeField = document.getElementById(
        "notifyChangeField"
    );
    const notifyOnChange = document.getElementById(
        "notifyOnChange"
    );

    const taskList = document.getElementById(
        "taskList"
    );
    const notificationList = document.getElementById(
        "notificationList"
    );
    const notificationCount = document.getElementById(
        "notificationCount"
    );
    const runList = document.getElementById(
        "runList"
    );
    const engineStatus = document.getElementById(
        "engineStatus"
    );
    const notice = document.getElementById(
        "automationNotice"
    );

    const refreshTasksButton = document.getElementById(
        "refreshTasksButton"
    );
    const markAllReadButton = document.getElementById(
        "markAllReadButton"
    );

    let tasks = [];
    let refreshTimer = null;
    let lastPreflight = null;


    function showNotice(message, isError = false) {
        notice.textContent = String(
            message || ""
        );
        notice.hidden = false;
        notice.classList.toggle(
            "error",
            Boolean(isError)
        );

        window.clearTimeout(
            showNotice.timer
        );

        showNotice.timer = window.setTimeout(
            () => {
                notice.hidden = true;
            },
            isError ? 7000 : 4200
        );
    }


    async function requestJson(url, options = {}) {
        const headers = new Headers(
            options.headers || {}
        );

        if (
            options.method
            && options.method.toUpperCase()
            !== "GET"
        ) {
            headers.set(
                "X-CSRF-Token",
                csrfToken
            );
        }

        const response = await fetch(
            url,
            {
                ...options,
                headers,
            }
        );

        if (response.status === 401) {
            window.location.href = "/login";
            throw new Error(
                "Authentication required."
            );
        }

        let data = {};

        try {
            data = await response.json();
        } catch (_) {
            // Keep fallback.
        }

        if (!response.ok) {
            const error = new Error(
                data.error
                || `Request failed (${response.status})`
            );
            error.data = data;
            error.status = response.status;
            throw error;
        }

        return data;
    }


    function localInputValue(date) {
        const pad = (value) => (
            String(value).padStart(2, "0")
        );

        return (
            `${date.getFullYear()}-`
            + `${pad(date.getMonth() + 1)}-`
            + `${pad(date.getDate())}T`
            + `${pad(date.getHours())}:`
            + `${pad(date.getMinutes())}`
        );
    }


    function localDateValue(date = new Date()) {
        const pad = (value) => (
            String(value).padStart(2, "0")
        );

        return (
            `${date.getFullYear()}-`
            + `${pad(date.getMonth() + 1)}-`
            + `${pad(date.getDate())}`
        );
    }


    function formatDateTime(value) {
        if (!value) {
            return "Not scheduled";
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value);
        }

        return new Intl.DateTimeFormat(
            undefined,
            {
                dateStyle: "medium",
                timeStyle: "short",
            }
        ).format(date);
    }


    function formatRelative(value) {
        if (!value) {
            return "";
        }

        const date = new Date(value);
        const diff = date.getTime() - Date.now();
        const absolute = Math.abs(diff);

        if (absolute < 60_000) {
            return diff >= 0
                ? "in less than a minute"
                : "less than a minute ago";
        }

        const units = [
            [86_400_000, "day"],
            [3_600_000, "hour"],
            [60_000, "minute"],
        ];

        for (const [size, label] of units) {
            if (absolute >= size) {
                const count = Math.round(
                    absolute / size
                );
                const plural = (
                    count === 1 ? "" : "s"
                );

                return diff >= 0
                    ? `in ${count} ${label}${plural}`
                    : `${count} ${label}${plural} ago`;
            }
        }

        return "";
    }


    function scheduleSummary(task) {
        const schedule = task.schedule || {};

        if (task.schedule_kind === "once") {
            return "One time";
        }

        if (task.schedule_kind === "interval") {
            return (
                `Every ${schedule.every || 1} `
                + `${schedule.unit || "minutes"}`
            );
        }

        if (task.schedule_kind === "daily") {
            const every = Number(
                schedule.every || 1
            );
            return (
                every === 1
                ? `Daily at ${schedule.time}`
                : `Every ${every} days at ${schedule.time}`
            );
        }

        if (task.schedule_kind === "weekly") {
            const names = [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ];
            const every = Number(
                schedule.every || 1
            );
            const day = names[
                Number(schedule.weekday || 0)
            ] || "Weekly";

            return (
                every === 1
                ? `${day} at ${schedule.time}`
                : `Every ${every} weeks · ${day} at ${schedule.time}`
            );
        }

        return task.schedule_kind;
    }


    function setTimezone() {
        let zone = "UTC";

        try {
            zone = (
                Intl.DateTimeFormat()
                .resolvedOptions()
                .timeZone
                || "UTC"
            );
        } catch (_) {
            zone = "UTC";
        }

        taskTimezone.value = zone;
        timezoneLabel.textContent = zone;
    }


    function setDefaultTimes() {
        const next = new Date(
            Date.now() + 5 * 60_000
        );
        next.setSeconds(0, 0);

        onceRunAt.value = (
            localInputValue(next)
        );
        intervalFirstRun.value = (
            localInputValue(next)
        );

        const hour = String(
            next.getHours()
        ).padStart(2, "0");
        const minute = String(
            next.getMinutes()
        ).padStart(2, "0");

        dailyTime.value = `${hour}:${minute}`;
        weeklyTime.value = `${hour}:${minute}`;
        weeklyDay.value = String(
            next.getDay() === 0
            ? 6
            : next.getDay() - 1
        );
    }


    function syncTaskType() {
        const type = taskType.value;
        const isAi = (
            type === "ai"
            || type === "condition"
        );
        const isCondition = (
            type === "condition"
        );

        conditionField.hidden = !isCondition;
        aiOptions.hidden = !isAi;
        notifyChangeField.hidden = (
            !isCondition
        );

        conditionText.required = isCondition;
    }


    function syncSchedulePanels() {
        const kind = scheduleKind.value;

        scheduleOnce.hidden = (
            kind !== "once"
        );
        scheduleInterval.hidden = (
            kind !== "interval"
        );
        scheduleDaily.hidden = (
            kind !== "daily"
        );
        scheduleWeekly.hidden = (
            kind !== "weekly"
        );
    }


    function currentEditingTask() {
        const taskId = Number(
            editingTaskId.value
        );

        if (!taskId) {
            return null;
        }

        return taskById(taskId);
    }


    function buildSchedulePayload() {
        const kind = scheduleKind.value;
        const editingTask = currentEditingTask();

        if (kind === "once") {
            return {
                run_at_local:
                    onceRunAt.value,
            };
        }

        if (kind === "interval") {
            return {
                every: Number(
                    intervalEvery.value
                ),
                unit: intervalUnit.value,
                first_run_local:
                    intervalFirstRun.value,
            };
        }

        if (kind === "daily") {
            return {
                every: Number(
                    dailyEvery.value
                ),
                time: dailyTime.value,
                anchor_date:
                    (
                        editingTask
                        && editingTask.schedule_kind === "daily"
                        && editingTask.schedule?.anchor_date
                    )
                    || localDateValue(),
            };
        }

        return {
            every: Number(
                weeklyEvery.value
            ),
            weekday: Number(
                weeklyDay.value
            ),
            time: weeklyTime.value,
            anchor_date:
                (
                    editingTask
                    && editingTask.schedule_kind === "weekly"
                    && editingTask.schedule?.anchor_date
                )
                || localDateValue(),
        };
    }


    function toolLabel(name) {
        return ({
            web: "Web",
            rag: "RAG",
            memory: "Memory",
        })[name] || name;
    }


    function renderPreflight(preflight) {
        lastPreflight = preflight || null;

        if (!preflight) {
            preflightPanel.hidden = true;
            return;
        }

        const status = String(
            preflight.status || "ready"
        );
        const ready = status === "ready";

        preflightPanel.hidden = false;
        preflightPanel.classList.toggle(
            "needs-attention",
            !ready
        );

        preflightStatus.textContent = (
            status === "needs_input"
                ? "Needs clarification"
                : status === "needs_tools"
                    ? "Needs tools"
                    : "Ready"
        );
        preflightStatus.className = (
            "automation-tag "
            + (ready
                ? "state-completed"
                : "state-needs_input")
        );

        preflightModel.textContent = (
            preflight.model
            && preflight.model !== "none"
                ? `Reviewed by ${preflight.model}`
                : "Local validation"
        );

        preflightSummary.textContent = (
            preflight.summary
            || "Task review completed."
        );

        const clarification = String(
            preflight.clarification || ""
        ).trim();
        preflightClarification.hidden = !clarification;
        preflightClarification.textContent = clarification;

        preflightTools.innerHTML = "";
        const recommended = (
            preflight.recommended_tools || {}
        );
        const required = (
            preflight.required_tools || {}
        );
        const recommendedNames = [
            "web",
            "rag",
            "memory",
        ].filter(
            (name) => Boolean(recommended[name])
        );

        const toolText = document.createElement(
            "span"
        );
        toolText.textContent = (
            "Recommended tools: "
            + (recommendedNames.length
                ? recommendedNames
                    .map(toolLabel)
                    .join(", ")
                : "None")
        );
        preflightTools.appendChild(toolText);

        const requiredNames = [
            "web",
            "rag",
            "memory",
        ].filter(
            (name) => Boolean(required[name])
        );

        if (requiredNames.length) {
            const requiredText = document.createElement(
                "span"
            );
            requiredText.textContent = (
                "Required: "
                + requiredNames
                    .map(toolLabel)
                    .join(", ")
            );
            preflightTools.appendChild(
                requiredText
            );
        }

        const current = {
            web: canWeb && allowWeb.checked,
            rag: allowRag.checked,
            memory: allowMemory.checked,
        };
        const differs = [
            "web",
            "rag",
            "memory",
        ].some(
            (name) => Boolean(recommended[name])
                !== Boolean(current[name])
        );

        applyRecommendedToolsButton.hidden = (
            !differs
            || taskType.value === "reminder"
        );
    }


    function applyRecommendedTools() {
        if (!lastPreflight) {
            return;
        }

        const recommended = (
            lastPreflight.recommended_tools || {}
        );

        allowWeb.checked = (
            canWeb
            && Boolean(recommended.web)
        );
        allowRag.checked = Boolean(
            recommended.rag
        );
        allowMemory.checked = Boolean(
            recommended.memory
        );

        renderPreflight(lastPreflight);
        showNotice(
            "Recommended tool access applied."
        );
    }


    async function reviewTask() {
        const payload = buildTaskPayload();
        reviewTaskButton.disabled = true;
        const previousText = reviewTaskButton.textContent;
        reviewTaskButton.textContent = "Reviewing...";

        try {
            const data = await requestJson(
                "/api/automations/preflight",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json",
                    },
                    body: JSON.stringify(payload),
                }
            );

            renderPreflight(
                data.preflight
            );
        } catch (error) {
            if (error.data?.preflight) {
                renderPreflight(
                    error.data.preflight
                );
            }
            showNotice(
                error.message,
                true
            );
        } finally {
            reviewTaskButton.disabled = false;
            reviewTaskButton.textContent = previousText;
        }
    }


    function buildTaskPayload() {
        return {
            title: taskTitle.value.trim(),
            task_type: taskType.value,
            instruction:
                taskInstruction.value.trim(),
            condition_text:
                conditionText.value.trim(),
            schedule_kind:
                scheduleKind.value,
            schedule:
                buildSchedulePayload(),
            timezone:
                taskTimezone.value || "UTC",
            model_mode:
                taskModelMode.value,
            allow_web:
                canWeb
                && allowWeb.checked,
            allow_rag:
                allowRag.checked,
            allow_memory:
                allowMemory.checked,
            notify_on_change:
                notifyOnChange.checked,
        };
    }


    function resetForm() {
        form.reset();
        editingTaskId.value = "";
        taskFormTitle.textContent = (
            "Create automation"
        );
        saveTaskButton.textContent = (
            "Create automation"
        );
        cancelEditButton.hidden = true;

        intervalEvery.value = "1";
        intervalUnit.value = "minutes";
        dailyEvery.value = "1";
        weeklyEvery.value = "1";
        taskModelMode.value = "default";
        notifyOnChange.checked = true;
        allowWeb.checked = false;
        allowRag.checked = false;
        allowMemory.checked = false;
        lastPreflight = null;
        preflightPanel.hidden = true;

        setTimezone();
        setDefaultTimes();
        syncTaskType();
        syncSchedulePanels();
    }


    function createTag(text, className = "") {
        const span = document.createElement(
            "span"
        );
        span.className = (
            "automation-tag "
            + className
        ).trim();
        span.textContent = text;
        return span;
    }


    function createActionButton(
        label,
        action,
        taskId,
        className = "secondary-button compact-button"
    ) {
        const button = document.createElement(
            "button"
        );
        button.type = "button";
        button.className = className;
        button.dataset.action = action;
        button.dataset.taskId = String(
            taskId
        );
        button.textContent = label;
        return button;
    }


    function renderTasks() {
        taskList.innerHTML = "";

        if (!tasks.length) {
            const empty = document.createElement(
                "div"
            );
            empty.className = "automation-empty";
            empty.textContent = (
                "No automations yet. Create your first task above."
            );
            taskList.appendChild(empty);
            return;
        }

        for (const task of tasks) {
            const card = document.createElement(
                "article"
            );
            card.className = "task-card";

            const top = document.createElement(
                "div"
            );
            top.className = "task-card-top";

            const heading = document.createElement(
                "div"
            );
            heading.className = "task-heading";

            const title = document.createElement(
                "h3"
            );
            title.textContent = task.title;

            const meta = document.createElement(
                "div"
            );
            meta.className = "task-meta";
            meta.append(
                createTag(
                    task.task_type === "condition"
                        ? "Conditional"
                        : task.task_type === "ai"
                            ? "AI task"
                            : "Reminder"
                ),
                createTag(
                    task.state,
                    `state-${task.state}`
                )
            );

            if (task.allow_web) {
                meta.appendChild(
                    createTag("Web")
                );
            }

            if (task.allow_rag) {
                meta.appendChild(
                    createTag("RAG")
                );
            }

            if (task.allow_memory) {
                meta.appendChild(
                    createTag("Memory")
                );
            }

            heading.append(
                title,
                meta
            );

            const next = document.createElement(
                "div"
            );
            next.className = "task-next";

            const nextLabel = document.createElement(
                "strong"
            );
            nextLabel.textContent = (
                task.state === "running"
                    ? "Running now"
                    : task.state === "cancelling"
                        ? "Stopping..."
                    : task.state === "needs_input"
                        ? "Needs clarification"
                        : task.state === "cancelled"
                            ? "Cancelled"
                        : task.enabled
                            ? formatDateTime(
                                task.next_run_at
                            )
                            : "Paused"
            );

            const relative = document.createElement(
                "small"
            );
            relative.textContent = (
                task.enabled
                && task.next_run_at
                ? formatRelative(
                    task.next_run_at
                )
                : scheduleSummary(task)
            );

            next.append(
                nextLabel,
                relative
            );

            top.append(
                heading,
                next
            );

            const instruction = document.createElement(
                "p"
            );
            instruction.className = "task-instruction";
            instruction.textContent = (
                task.instruction
            );

            const schedule = document.createElement(
                "div"
            );
            schedule.className = "task-schedule";
            schedule.textContent = (
                `${scheduleSummary(task)} · ${task.timezone}`
            );

            let outcome = null;

            if (
                task.task_type === "condition"
                && task.last_run_at
                && !task.last_error
                && task.state !== "cancelling"
            ) {
                outcome = document.createElement(
                    "div"
                );
                outcome.className = "task-run-outcome";

                if (task.last_condition_met === false) {
                    outcome.textContent = (
                        "Last check: no match · no notification"
                    );
                } else if (
                    task.last_condition_met === true
                    && task.last_notified
                ) {
                    outcome.textContent = (
                        "Last check: matched · notification sent"
                    );
                } else if (task.last_condition_met === true) {
                    outcome.textContent = (
                        "Last check: matched · unchanged result, notification suppressed"
                    );
                }
            }

            let clarificationBox = null;

            if (task.state === "needs_input") {
                clarificationBox = document.createElement(
                    "div"
                );
                clarificationBox.className = (
                    "task-needs-input"
                );
                clarificationBox.textContent = (
                    task.preflight_message
                    || task.last_error
                    || "This task needs clarification before it can run again."
                );
            }

            const actions = document.createElement(
                "div"
            );
            actions.className = "task-actions";

            const isRunning = (
                task.state === "running"
            );
            const isCancelling = (
                task.state === "cancelling"
            );
            const isBusy = (
                isRunning || isCancelling
            );

            const runButton = createActionButton(
                task.manual_run_requested
                    ? "Run queued"
                    : "Run now",
                "run",
                task.id
            );
            runButton.disabled = (
                isBusy
                || task.state === "needs_input"
                || task.manual_run_requested
            );

            const pauseButton = createActionButton(
                task.enabled
                    ? "Pause"
                    : "Resume",
                "toggle",
                task.id
            );
            pauseButton.dataset.enabled = (
                task.enabled ? "0" : "1"
            );
            pauseButton.disabled = (
                isBusy
                || task.state === "needs_input"
            );

            let stopButton = null;
            let stopPauseButton = null;

            if (isBusy) {
                stopButton = createActionButton(
                    isCancelling
                        ? "Stopping..."
                        : "Stop run",
                    "cancel",
                    task.id,
                    "secondary-button compact-button stop-run-button"
                );
                stopButton.disabled = isCancelling;

                stopPauseButton = createActionButton(
                    isCancelling
                        ? "Stopping..."
                        : "Stop & pause",
                    "cancel_pause",
                    task.id,
                    "secondary-button compact-button danger-button"
                );
                stopPauseButton.disabled = isCancelling;
            }

            const editButton = createActionButton(
                task.state === "needs_input"
                    ? "Edit to fix"
                    : "Edit",
                "edit",
                task.id
            );
            editButton.disabled = isBusy;

            const deleteButton = createActionButton(
                "Delete",
                "delete",
                task.id,
                "secondary-button compact-button danger-button"
            );
            deleteButton.disabled = isBusy;

            actions.append(
                runButton,
                pauseButton
            );

            if (stopButton) {
                actions.append(
                    stopButton,
                    stopPauseButton
                );
            }

            actions.append(
                editButton,
                deleteButton
            );


            if (
                task.last_error
                || task.last_result
            ) {
                const details = document.createElement(
                    "details"
                );
                details.className = "task-last-run";

                const summary = document.createElement(
                    "summary"
                );
                summary.textContent = (
                    task.last_error
                    ? "Last run error"
                    : "Last result"
                );

                const pre = document.createElement(
                    "pre"
                );
                pre.textContent = (
                    task.last_error
                    || task.last_result
                );

                details.append(
                    summary,
                    pre
                );
                card.append(
                    top,
                    instruction,
                    schedule,
                    ...(outcome ? [outcome] : []),
                    ...(clarificationBox ? [clarificationBox] : []),
                    actions,
                    details
                );
            } else {
                card.append(
                    top,
                    instruction,
                    schedule,
                    ...(outcome ? [outcome] : []),
                    ...(clarificationBox ? [clarificationBox] : []),
                    actions
                );
            }

            taskList.appendChild(card);
        }
    }


    function renderNotifications(data) {
        const items = data.notifications || [];
        const unread = Number(
            data.unread_count || 0
        );

        notificationCount.textContent = (
            unread
            ? `${unread} unread`
            : "All caught up"
        );
        markAllReadButton.disabled = (
            unread === 0
        );
        notificationList.innerHTML = "";

        if (!items.length) {
            const empty = document.createElement(
                "div"
            );
            empty.className = "automation-empty";
            empty.textContent = (
                "Automation notifications will appear here."
            );
            notificationList.appendChild(empty);
            return;
        }

        for (const item of items) {
            const card = document.createElement(
                "article"
            );
            card.className = (
                "notification-card"
                + (item.is_read ? "" : " unread")
            );

            const header = document.createElement(
                "div"
            );
            header.className = "notification-header";

            const title = document.createElement(
                "strong"
            );
            title.textContent = item.title;

            const time = document.createElement(
                "small"
            );
            time.textContent = (
                formatRelative(
                    item.created_at
                )
                || formatDateTime(
                    item.created_at
                )
            );

            header.append(
                title,
                time
            );

            const body = document.createElement(
                "p"
            );
            body.textContent = item.body;

            card.append(
                header,
                body
            );

            if (!item.is_read) {
                const readButton = document.createElement(
                    "button"
                );
                readButton.type = "button";
                readButton.className = (
                    "notification-read-button"
                );
                readButton.dataset.notificationId = (
                    String(item.id)
                );
                readButton.textContent = "Mark read";
                card.appendChild(
                    readButton
                );
            }

            notificationList.appendChild(card);
        }
    }


    function renderRuns(runs) {
        runList.innerHTML = "";

        if (!runs.length) {
            const empty = document.createElement(
                "div"
            );
            empty.className = "automation-empty";
            empty.textContent = (
                "No automation runs yet."
            );
            runList.appendChild(empty);
            return;
        }

        for (const run of runs) {
            const row = document.createElement(
                "details"
            );
            row.className = "run-row";

            const summary = document.createElement(
                "summary"
            );

            const left = document.createElement(
                "span"
            );
            left.className = "run-summary-left";

            const title = document.createElement(
                "strong"
            );
            title.textContent = run.task_title;

            const tags = document.createElement(
                "span"
            );
            tags.className = "run-tags";
            tags.append(
                createTag(
                    run.status,
                    run.status === "success"
                        ? "state-completed"
                        : run.status === "needs_input"
                            ? "state-needs_input"
                            : run.status === "cancelled"
                                ? "state-cancelled"
                                : run.status === "running"
                                    ? "state-running"
                                    : "state-failed"
                ),
                createTag(
                    run.trigger_type
                )
            );

            left.append(
                title,
                tags
            );

            const right = document.createElement(
                "span"
            );
            right.className = "run-summary-time";
            right.textContent = formatDateTime(
                run.started_at
            );

            summary.append(
                left,
                right
            );

            const content = document.createElement(
                "div"
            );
            content.className = "run-detail";

            const pre = document.createElement(
                "pre"
            );
            pre.textContent = (
                run.error
                || run.result
                || "No result text."
            );
            content.appendChild(pre);

            if (
                Array.isArray(run.tool_log)
                && run.tool_log.length
            ) {
                const tools = document.createElement(
                    "div"
                );
                tools.className = "run-tools";
                tools.textContent = (
                    "Tools: "
                    + run.tool_log
                    .map(
                        (item) => (
                            item.tool || "tool"
                        )
                    )
                    .join(", ")
                );
                content.appendChild(tools);
            }

            row.append(
                summary,
                content
            );
            runList.appendChild(row);
        }
    }


    async function loadStatus() {
        try {
            const data = await requestJson(
                "/api/automations/status"
            );
            const engine = data.engine || {};

            if (!engine.enabled) {
                engineStatus.textContent = (
                    "Engine disabled"
                );
                engineStatus.className = (
                    "engine-status error"
                );
            } else if (engine.running_task_id) {
                engineStatus.textContent = (
                    `Running task #${engine.running_task_id}`
                );
                engineStatus.className = (
                    "engine-status active"
                );
            } else if (engine.started) {
                engineStatus.textContent = (
                    "Engine active"
                );
                engineStatus.className = (
                    "engine-status active"
                );
            } else {
                engineStatus.textContent = (
                    "Engine starting..."
                );
                engineStatus.className = (
                    "engine-status"
                );
            }
        } catch (error) {
            engineStatus.textContent = (
                "Engine status unavailable"
            );
            engineStatus.className = (
                "engine-status error"
            );
        }
    }


    async function loadTasks() {
        const data = await requestJson(
            "/api/automations/tasks"
        );
        tasks = data.tasks || [];
        renderTasks();
    }


    async function loadNotifications() {
        const data = await requestJson(
            "/api/automations/notifications?limit=30"
        );
        renderNotifications(data);
    }


    async function loadRuns() {
        const data = await requestJson(
            "/api/automations/runs?limit=40"
        );
        renderRuns(
            data.runs || []
        );
    }


    async function refreshAll(showError = true) {
        try {
            await Promise.all([
                loadStatus(),
                loadTasks(),
                loadNotifications(),
                loadRuns(),
            ]);
        } catch (error) {
            if (showError) {
                showNotice(
                    error.message,
                    true
                );
            }
        }
    }


    function taskById(taskId) {
        return tasks.find(
            (task) => (
                Number(task.id)
                === Number(taskId)
            )
        ) || null;
    }


    function fillEditForm(task) {
        editingTaskId.value = String(
            task.id
        );
        taskFormTitle.textContent = (
            `Edit automation #${task.id}`
        );
        saveTaskButton.textContent = (
            "Save changes"
        );
        cancelEditButton.hidden = false;

        taskTitle.value = task.title;
        taskType.value = task.task_type;
        taskInstruction.value = (
            task.instruction
        );
        conditionText.value = (
            task.condition_text || ""
        );
        taskTimezone.value = (
            task.timezone || taskTimezone.value
        );
        timezoneLabel.textContent = (
            taskTimezone.value
        );
        taskModelMode.value = (
            task.model_mode || "auto"
        );
        allowWeb.checked = Boolean(
            task.allow_web
        );
        allowRag.checked = Boolean(
            task.allow_rag
        );
        allowMemory.checked = Boolean(
            task.allow_memory
        );
        notifyOnChange.checked = Boolean(
            task.notify_on_change
        );

        scheduleKind.value = (
            task.schedule_kind
        );
        const schedule = task.schedule || {};

        if (task.schedule_kind === "once") {
            onceRunAt.value = (
                schedule.run_at_local || ""
            );
        } else if (
            task.schedule_kind === "interval"
        ) {
            intervalEvery.value = String(
                schedule.every || 1
            );
            intervalUnit.value = (
                schedule.unit || "minutes"
            );

            if (schedule.anchor_local) {
                intervalFirstRun.value = (
                    schedule.anchor_local
                );
            } else {
                const anchor = new Date(
                    schedule.anchor_utc
                );
                if (!Number.isNaN(anchor.getTime())) {
                    intervalFirstRun.value = (
                        localInputValue(anchor)
                    );
                }
            }
        } else if (
            task.schedule_kind === "daily"
        ) {
            dailyEvery.value = String(
                schedule.every || 1
            );
            dailyTime.value = (
                schedule.time || "08:00"
            );
        } else if (
            task.schedule_kind === "weekly"
        ) {
            weeklyEvery.value = String(
                schedule.every || 1
            );
            weeklyDay.value = String(
                schedule.weekday || 0
            );
            weeklyTime.value = (
                schedule.time || "08:00"
            );
        }

        syncTaskType();
        syncSchedulePanels();

        if (task.state === "needs_input") {
            renderPreflight({
                status: "needs_input",
                summary: "This automation paused because the last unattended run needed clarification.",
                clarification: (
                    task.preflight_message
                    || task.last_error
                    || "Add the missing detail and save the task."
                ),
                recommended_tools: (
                    task.compiled_spec?.recommended_tools
                    || {}
                ),
                required_tools: (
                    task.compiled_spec?.required_tools
                    || {}
                ),
                model: "",
            });
        } else {
            lastPreflight = null;
            preflightPanel.hidden = true;
        }

        window.scrollTo({
            top: 0,
            behavior: "smooth",
        });
        taskTitle.focus();
    }


    async function submitTask(event) {
        event.preventDefault();

        const payload = buildTaskPayload();
        const taskId = editingTaskId.value;
        saveTaskButton.disabled = true;
        reviewTaskButton.disabled = true;
        const previousSaveText = saveTaskButton.textContent;
        saveTaskButton.textContent = (
            taskType.value === "reminder"
                ? previousSaveText
                : "Reviewing & saving..."
        );

        try {
            if (taskId) {
                await requestJson(
                    `/api/automations/tasks/${taskId}`,
                    {
                        method: "PATCH",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify(
                            payload
                        ),
                    }
                );
                showNotice(
                    "Automation updated."
                );
            } else {
                await requestJson(
                    "/api/automations/tasks",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify(
                            payload
                        ),
                    }
                );
                showNotice(
                    "Automation created."
                );
            }

            resetForm();
            await refreshAll(false);
        } catch (error) {
            if (error.data?.preflight) {
                renderPreflight(
                    error.data.preflight
                );
            }

            showNotice(
                error.message,
                true
            );
        } finally {
            saveTaskButton.disabled = false;
            reviewTaskButton.disabled = false;
            saveTaskButton.textContent = previousSaveText;
        }
    }


    async function handleTaskAction(button) {
        const taskId = Number(
            button.dataset.taskId
        );
        const action = button.dataset.action;
        const task = taskById(
            taskId
        );

        if (!task) {
            return;
        }

        button.disabled = true;

        try {
            if (action === "edit") {
                fillEditForm(task);
                return;
            }

            if (action === "delete") {
                const confirmed = window.confirm(
                    `Delete automation "${task.title}"?\n\nRun history is retained, but the task itself is removed.`
                );

                if (!confirmed) {
                    return;
                }

                await requestJson(
                    `/api/automations/tasks/${taskId}`,
                    {
                        method: "DELETE",
                    }
                );
                showNotice(
                    "Automation deleted."
                );
            }

            if (action === "toggle") {
                const enabled = (
                    button.dataset.enabled === "1"
                );
                await requestJson(
                    `/api/automations/tasks/${taskId}/enabled`,
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify({
                            enabled,
                        }),
                    }
                );
                showNotice(
                    enabled
                    ? "Automation resumed."
                    : "Automation paused."
                );
            }

            if (action === "run") {
                await requestJson(
                    `/api/automations/tasks/${taskId}/run`,
                    {
                        method: "POST",
                    }
                );
                showNotice(
                    "Run queued."
                );
                startShortPolling();
            }

            if (
                action === "cancel"
                || action === "cancel_pause"
            ) {
                await requestJson(
                    `/api/automations/tasks/${taskId}/cancel`,
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify({
                            pause_after:
                                action === "cancel_pause",
                        }),
                    }
                );
                showNotice(
                    action === "cancel_pause"
                        ? "Stopping run and pausing future runs..."
                        : "Stopping current run..."
                );
                startShortPolling();
            }

            await refreshAll(false);
        } catch (error) {
            showNotice(
                error.message,
                true
            );
        } finally {
            button.disabled = false;
        }
    }


    function startShortPolling() {
        if (refreshTimer) {
            window.clearInterval(
                refreshTimer
            );
        }

        let polls = 0;
        let quietPolls = 0;

        refreshTimer = window.setInterval(
            async () => {
                polls += 1;
                await refreshAll(false);

                const active = tasks.some(
                    (task) => (
                        task.state === "running"
                        || task.state === "cancelling"
                        || task.manual_run_requested
                    )
                );

                quietPolls = active
                    ? 0
                    : quietPolls + 1;

                if (
                    quietPolls >= 2
                    || polls >= 180
                ) {
                    window.clearInterval(
                        refreshTimer
                    );
                    refreshTimer = null;
                }
            },
            2000
        );
    }


    reviewTaskButton.addEventListener(
        "click",
        reviewTask
    );

    applyRecommendedToolsButton.addEventListener(
        "click",
        applyRecommendedTools
    );

    form.addEventListener(
        "submit",
        submitTask
    );

    taskType.addEventListener(
        "change",
        syncTaskType
    );

    scheduleKind.addEventListener(
        "change",
        syncSchedulePanels
    );

    cancelEditButton.addEventListener(
        "click",
        resetForm
    );

    refreshTasksButton.addEventListener(
        "click",
        () => refreshAll()
    );

    taskList.addEventListener(
        "click",
        (event) => {
            const button = event.target.closest(
                "button[data-action]"
            );

            if (button) {
                handleTaskAction(
                    button
                );
            }
        }
    );

    notificationList.addEventListener(
        "click",
        async (event) => {
            const button = event.target.closest(
                "button[data-notification-id]"
            );

            if (!button) {
                return;
            }

            button.disabled = true;

            try {
                await requestJson(
                    (
                        "/api/automations/notifications/"
                        + button.dataset.notificationId
                        + "/read"
                    ),
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify({
                            is_read: true,
                        }),
                    }
                );
                await loadNotifications();
            } catch (error) {
                showNotice(
                    error.message,
                    true
                );
            }
        }
    );

    form.addEventListener(
        "input",
        (event) => {
            if (
                event.target === applyRecommendedToolsButton
            ) {
                return;
            }

            if (lastPreflight) {
                lastPreflight = null;
                preflightPanel.hidden = true;
            }
        }
    );

    markAllReadButton.addEventListener(
        "click",
        async () => {
            try {
                await requestJson(
                    "/api/automations/notifications/read-all",
                    {
                        method: "POST",
                    }
                );
                await loadNotifications();
            } catch (error) {
                showNotice(
                    error.message,
                    true
                );
            }
        }
    );

    setTimezone();
    resetForm();
    refreshAll();

    // Keep task state and notification inbox reasonably fresh while the
    // workspace is open without making the scheduler depend on the browser.
    window.setInterval(
        () => refreshAll(false),
        30_000
    );
})();
