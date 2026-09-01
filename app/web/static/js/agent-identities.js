(() => {
    "use strict";

    const app = document.getElementById("agentIdentitiesApp");

    if (!app) {
        return;
    }

    const csrfToken = (
        document.querySelector(
            'meta[name="csrf-token"]'
        )?.content
        || ""
    );

    const el = {
        notice: document.getElementById("agentIdentityNotice"),
        list: document.getElementById("agentIdentityList"),
        editor: document.getElementById("agentIdentityEditor"),
        newButton: document.getElementById("newAgentIdentityButton"),
    };

    let identities = [];
    let selectedId = null;

    function notice(message, kind = "info") {
        el.notice.textContent = message || "";
        el.notice.dataset.kind = kind;
        el.notice.hidden = !message;
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

        if (
            config.method !== "GET"
            && config.method !== "HEAD"
        ) {
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
            throw new Error(
                data.error
                || `Request failed (${response.status}).`
            );
        }

        return data;
    }

    function renderList() {
        el.list.replaceChildren();

        if (!identities.length) {
            el.list.textContent = "No Agents yet.";
            return;
        }

        for (const identity of identities) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "agent-identity-row";

            if (identity.id === selectedId) {
                button.classList.add("selected");
            }

            const top = document.createElement("div");
            top.className = "agent-identity-row-top";

            const name = document.createElement("strong");
            name.textContent = identity.name;

            top.appendChild(name);

            if (identity.is_default) {
                const badge = document.createElement("span");
                badge.className = "agent-identity-badge";
                badge.textContent = "Default";
                top.appendChild(badge);
            }

            const description = document.createElement("p");
            description.textContent = (
                identity.description
                || "Persistent ATLAS Agent"
            );

            const meta = document.createElement("small");
            meta.textContent = (
                `${identity.memory_count || 0} memories`
                + ` · ${identity.run_count || 0} runs`
            );

            button.append(top, description, meta);

            button.addEventListener("click", () => {
                selectIdentity(identity.id);
            });

            el.list.appendChild(button);
        }
    }

    function field(label, input) {
        const wrapper = document.createElement("label");
        wrapper.append(
            document.createTextNode(label),
            input,
        );
        return wrapper;
    }

    function checkbox(label, checked) {
        const wrapper = document.createElement("label");
        wrapper.className = "agent-identity-check";

        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(checked);

        const text = document.createElement("span");
        text.textContent = label;

        wrapper.append(input, text);

        return {
            wrapper,
            input,
        };
    }

    function renderNewEditor() {
        selectedId = null;
        renderList();
        el.editor.replaceChildren();

        const shell = document.createElement("div");
        shell.className = "agent-identity-editor";

        const heading = document.createElement("div");
        heading.className = "agent-identity-editor-heading";

        const titleWrap = document.createElement("div");
        const title = document.createElement("h2");
        title.textContent = "Create Agent";
        const subtitle = document.createElement("small");
        subtitle.textContent = (
            "A persistent identity with its own isolated memory."
        );
        titleWrap.append(title, subtitle);
        heading.appendChild(titleWrap);

        const form = document.createElement("form");
        form.className = "agent-identity-form";

        const name = document.createElement("input");
        name.required = true;
        name.maxLength = 80;
        name.placeholder = "Software Engineer";

        const description = document.createElement("input");
        description.maxLength = 800;
        description.placeholder = "Builds and reviews local software projects";

        const instructions = document.createElement("textarea");
        instructions.rows = 7;
        instructions.maxLength = 6000;
        instructions.placeholder = (
            "Standing instructions for this Agent. "
            + "Example: test before finalizing, prefer maintainable code..."
        );

        const memory = checkbox(
            "Use this Agent's own memory",
            true,
        );

        const reflection = checkbox(
            "Learn conservatively after completed runs",
            true,
        );

        const toggles = document.createElement("div");
        toggles.className = "agent-identity-toggle-grid";
        toggles.append(
            memory.wrapper,
            reflection.wrapper,
        );

        const save = document.createElement("button");
        save.type = "submit";
        save.className = "primary-button";
        save.textContent = "Create Agent";

        form.append(
            field("Name", name),
            field("Description", description),
            field("Standing instructions", instructions),
            toggles,
            save,
        );

        form.addEventListener("submit", async (event) => {
            event.preventDefault();

            try {
                const data = await api("/api/agent-identities", {
                    method: "POST",
                    body: {
                        name: name.value.trim(),
                        description: description.value.trim(),
                        instructions: instructions.value.trim(),
                        memory_enabled: memory.input.checked,
                        reflection_enabled: reflection.input.checked,
                    },
                });

                notice("Agent created.", "success");
                await loadIdentities();
                await selectIdentity(data.identity.id);
            } catch (error) {
                notice(error.message, "error");
            }
        });

        shell.append(heading, form);
        el.editor.appendChild(shell);
    }

    function renderMemoryList(identity, memories, container) {
        container.replaceChildren();

        if (!memories.length) {
            const empty = document.createElement("div");
            empty.className = "muted";
            empty.textContent = (
                "No Agent memories yet. Add one manually or let reflection "
                + "learn conservative lessons from completed runs."
            );
            container.appendChild(empty);
            return;
        }

        for (const memory of memories) {
            const item = document.createElement("article");
            item.className = "agent-memory-item";

            if (memory.status === "archived") {
                item.classList.add("archived");
            }

            const top = document.createElement("div");
            top.className = "agent-memory-top";

            const content = document.createElement("strong");
            content.textContent = memory.content;

            top.appendChild(content);

            if (memory.status === "active") {
                const archive = document.createElement("button");
                archive.type = "button";
                archive.className = "agent-memory-archive";
                archive.textContent = "Archive";

                archive.addEventListener("click", async () => {
                    try {
                        await api(
                            `/api/agent-identities/memories/${memory.id}`,
                            {
                                method: "DELETE",
                            },
                        );

                        await selectIdentity(identity.id);
                    } catch (error) {
                        notice(error.message, "error");
                    }
                });

                top.appendChild(archive);
            }

            const meta = document.createElement("div");
            meta.className = "agent-memory-meta";
            meta.textContent = (
                `${memory.category}`
                + ` · importance ${memory.importance}`
                + ` · confidence ${Number(memory.confidence || 0).toFixed(2)}`
                + ` · ${memory.source}`
                + (
                    memory.source_run_id
                        ? " · run provenance saved"
                        : ""
                )
            );

            item.append(top, meta);
            container.appendChild(item);
        }
    }

    function renderReflections(items, container) {
        container.replaceChildren();

        if (!items.length) {
            const empty = document.createElement("div");
            empty.className = "muted";
            empty.textContent = "No post-run reflections recorded yet.";
            container.appendChild(empty);
            return;
        }

        for (const reflection of items.slice(0, 12)) {
            const item = document.createElement("article");
            item.className = "agent-reflection-item";

            const text = document.createElement("p");
            text.textContent = (
                reflection.summary
                || "Reflection completed with no durable lesson stored."
            );

            const meta = document.createElement("small");
            meta.textContent = (
                `${reflection.stored_count}/${reflection.proposed_count} memories stored`
                + ` · ${reflection.created_at || ""}`
            );

            item.append(text, meta);
            container.appendChild(item);
        }
    }

    async function renderIdentityEditor(identity, detail) {
        el.editor.replaceChildren();

        const shell = document.createElement("div");
        shell.className = "agent-identity-editor";

        const heading = document.createElement("div");
        heading.className = "agent-identity-editor-heading";

        const titleWrap = document.createElement("div");
        const title = document.createElement("h2");
        title.textContent = identity.name;
        const subtitle = document.createElement("small");
        subtitle.textContent = (
            `${identity.run_count || 0} runs`
            + ` · ${identity.memory_count || 0} active memories`
        );
        titleWrap.append(title, subtitle);
        heading.appendChild(titleWrap);

        const form = document.createElement("form");
        form.className = "agent-identity-form";

        const name = document.createElement("input");
        name.value = identity.name;
        name.required = true;
        name.maxLength = 80;

        const description = document.createElement("input");
        description.value = identity.description || "";
        description.maxLength = 800;

        const instructions = document.createElement("textarea");
        instructions.value = identity.instructions || "";
        instructions.rows = 8;
        instructions.maxLength = 6000;

        const memory = checkbox(
            "Use this Agent's own memory",
            identity.memory_enabled,
        );

        const reflection = checkbox(
            "Learn conservatively after completed runs",
            identity.reflection_enabled,
        );

        const toggles = document.createElement("div");
        toggles.className = "agent-identity-toggle-grid";
        toggles.append(
            memory.wrapper,
            reflection.wrapper,
        );

        const actions = document.createElement("div");
        actions.className = "agent-identity-actions";

        const save = document.createElement("button");
        save.type = "submit";
        save.className = "primary-button";
        save.textContent = "Save";

        actions.appendChild(save);

        if (!identity.is_default) {
            const makeDefault = document.createElement("button");
            makeDefault.type = "button";
            makeDefault.className = "secondary-button";
            makeDefault.textContent = "Make default";

            makeDefault.addEventListener("click", async () => {
                try {
                    await api(
                        `/api/agent-identities/${identity.id}`,
                        {
                            method: "PATCH",
                            body: {
                                is_default: true,
                            },
                        },
                    );

                    notice("Default Agent updated.", "success");
                    await loadIdentities();
                    await selectIdentity(identity.id);
                } catch (error) {
                    notice(error.message, "error");
                }
            });

            actions.appendChild(makeDefault);

            const archive = document.createElement("button");
            archive.type = "button";
            archive.className = "secondary-button";
            archive.textContent = "Archive Agent";

            archive.addEventListener("click", async () => {
                if (!window.confirm(
                    `Archive ${identity.name}? Existing runs and memories remain stored.`
                )) {
                    return;
                }

                try {
                    await api(
                        `/api/agent-identities/${identity.id}`,
                        {
                            method: "DELETE",
                        },
                    );

                    notice("Agent archived.", "success");
                    selectedId = null;
                    await loadIdentities();

                    if (identities.length) {
                        await selectIdentity(identities[0].id);
                    } else {
                        renderNewEditor();
                    }
                } catch (error) {
                    notice(error.message, "error");
                }
            });

            actions.appendChild(archive);
        }

        form.append(
            field("Name", name),
            field("Description", description),
            field("Standing instructions", instructions),
            toggles,
            actions,
        );

        form.addEventListener("submit", async (event) => {
            event.preventDefault();

            try {
                await api(
                    `/api/agent-identities/${identity.id}`,
                    {
                        method: "PATCH",
                        body: {
                            name: name.value.trim(),
                            description: description.value.trim(),
                            instructions: instructions.value.trim(),
                            memory_enabled: memory.input.checked,
                            reflection_enabled: reflection.input.checked,
                        },
                    },
                );

                notice("Agent updated.", "success");
                await loadIdentities();
                await selectIdentity(identity.id);
            } catch (error) {
                notice(error.message, "error");
            }
        });

        const memorySection = document.createElement("section");
        memorySection.className = "agent-memory-section";

        const memoryHeading = document.createElement("div");
        memoryHeading.className = "agent-memory-heading";

        const memoryHeadingText = document.createElement("div");
        const memoryTitle = document.createElement("h3");
        memoryTitle.textContent = "Agent Memory";
        const memorySub = document.createElement("small");
        memorySub.textContent = (
            "Separate from your personal ATLAS memory. "
            + "Reflection can only add conservative working memories."
        );

        memoryHeadingText.append(memoryTitle, memorySub);
        memoryHeading.appendChild(memoryHeadingText);

        const memoryForm = document.createElement("form");
        memoryForm.className = "agent-memory-form";

        const memoryText = document.createElement("textarea");
        memoryText.rows = 3;
        memoryText.required = true;
        memoryText.maxLength = 3000;
        memoryText.placeholder = (
            "Example: When modifying Python code, re-run tests after each repair."
        );

        const category = document.createElement("select");

        for (const value of [
            "procedure",
            "lesson",
            "preference",
            "domain",
            "project_pattern",
            "general",
        ]) {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value.replace("_", " ");
            category.appendChild(option);
        }

        const importance = document.createElement("input");
        importance.type = "number";
        importance.min = "1";
        importance.max = "10";
        importance.value = "6";

        const add = document.createElement("button");
        add.type = "submit";
        add.className = "secondary-button";
        add.textContent = "Add memory";

        memoryForm.append(
            field("Memory", memoryText),
            field("Category", category),
            field("Importance", importance),
            add,
        );

        const memoryList = document.createElement("div");
        memoryList.className = "agent-memory-list";

        memoryForm.addEventListener("submit", async (event) => {
            event.preventDefault();

            try {
                const data = await api(
                    `/api/agent-identities/${identity.id}/memories`,
                    {
                        method: "POST",
                        body: {
                            content: memoryText.value.trim(),
                            category: category.value,
                            importance: Number(importance.value || 6),
                            confidence: 0.98,
                        },
                    },
                );

                memoryText.value = "";

                notice(
                    data.memory?.duplicate
                        ? "A very similar Agent memory already exists."
                        : "Agent memory added.",
                    "success",
                );

                await selectIdentity(identity.id);
            } catch (error) {
                notice(error.message, "error");
            }
        });

        memorySection.append(
            memoryHeading,
            memoryForm,
            memoryList,
        );

        const reflectionSection = document.createElement("section");
        reflectionSection.className = "agent-reflection-section";

        const reflectionHeading = document.createElement("div");
        reflectionHeading.className = "agent-memory-heading";

        const reflectionHeadingText = document.createElement("div");
        const reflectionTitle = document.createElement("h3");
        reflectionTitle.textContent = "Learning history";
        const reflectionSub = document.createElement("small");
        reflectionSub.textContent = (
            "Post-run reflection provenance. "
            + "No automatic instruction or permission changes."
        );

        reflectionHeadingText.append(
            reflectionTitle,
            reflectionSub,
        );

        reflectionHeading.appendChild(
            reflectionHeadingText
        );

        const reflectionList = document.createElement("div");
        reflectionList.className = "agent-reflection-list";

        renderMemoryList(
            identity,
            detail.memories || [],
            memoryList,
        );

        renderReflections(
            detail.reflections || [],
            reflectionList,
        );

        reflectionSection.append(
            reflectionHeading,
            reflectionList,
        );

        shell.append(
            heading,
            form,
            memorySection,
            reflectionSection,
        );

        el.editor.appendChild(shell);
    }

    async function selectIdentity(id) {
        selectedId = id;
        renderList();

        try {
            const detail = await api(
                `/api/agent-identities/${encodeURIComponent(id)}`
            );

            await renderIdentityEditor(
                detail.identity,
                detail,
            );

            renderList();
        } catch (error) {
            notice(error.message, "error");
        }
    }

    async function loadIdentities() {
        try {
            const data = await api("/api/agent-identities");
            identities = data.identities || [];
            renderList();

            return data;
        } catch (error) {
            notice(error.message, "error");
            identities = [];
            renderList();
            return {};
        }
    }

    el.newButton.addEventListener("click", renderNewEditor);

    (async () => {
        const data = await loadIdentities();

        const target = (
            selectedId
            || data.default_id
            || identities[0]?.id
        );

        if (target) {
            await selectIdentity(target);
        } else {
            renderNewEditor();
        }
    })();
})();
