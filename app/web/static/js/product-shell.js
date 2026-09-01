(() => {
    "use strict";

    const body = document.body;

    if (!body) {
        return;
    }

    const product = body.dataset.productName || "ATLAS";
    const company = body.dataset.productCompany || "BEGLOO";
    const mark = body.dataset.productMark || "A";
    const canLibrary = body.dataset.canLibrary === "1";

    function applyBrand() {
        for (const element of document.querySelectorAll(".brand-mark")) {
            const current = (element.textContent || "").trim();

            if (
                ["P", "A", ""].includes(current)
                && current !== mark
            ) {
                element.textContent = mark;
            }
        }

        const sidebarBrand = document.querySelector(".sidebar-brand-label");

        if (
            sidebarBrand
            && sidebarBrand.textContent.trim() !== product
        ) {
            sidebarBrand.textContent = product;
        }

        const chatTitle = document.querySelector(".chat-title");

        if (
            chatTitle
            && chatTitle.textContent.trim() !== product
        ) {
            chatTitle.textContent = product;
        }

        const agentTitle = document.querySelector(".agent-title-row h1");

        if (
            agentTitle
            && agentTitle.textContent.trim() === "Agent Workspace"
        ) {
            agentTitle.textContent = `${product} Agent Workspace`;
        }

        if (
            document.title.includes("Private AI")
        ) {
            document.title = document.title.replace(
                "Private AI",
                product,
            );
        }
    }

    function addLibraryNavigation() {
        if (!canLibrary) {
            return;
        }

        const footer = document.querySelector(".sidebar-footer");

        if (
            footer
            && !footer.querySelector('[data-atlas-library-link="1"]')
        ) {
            const link = document.createElement("a");
            link.href = "/library";
            link.className = "sidebar-link";
            link.dataset.atlasLibraryLink = "1";

            const icon = document.createElement("span");
            icon.textContent = "▣";

            const label = document.createTextNode("Library");

            link.append(icon, label);

            const automationLink = Array.from(
                footer.querySelectorAll("a.sidebar-link")
            ).find(
                (item) => (
                    (item.textContent || "")
                    .toLowerCase()
                    .includes("automation")
                )
            );

            if (automationLink) {
                footer.insertBefore(link, automationLink);
            } else {
                footer.prepend(link);
            }
        }

        const agentBack = document.querySelector(".agent-back-link");

        if (
            agentBack
            && !document.querySelector(".atlas-agent-library-link")
        ) {
            const link = document.createElement("a");
            link.href = "/library";
            link.className = "atlas-agent-library-link";
            link.textContent = "Library";
            agentBack.insertAdjacentElement("afterend", link);
        }
    }

    function upgradeAgentBudget() {
        const select = document.getElementById("agentMaxSteps");

        if (!select || select.dataset.atlasBudget === "1") {
            return;
        }

        select.dataset.atlasBudget = "1";
        select.replaceChildren();

        const choices = [
            ["6", "6 steps · quick"],
            ["12", "12 steps · standard"],
            ["25", "25 steps · deep"],
            ["40", "40 steps · project"],
        ];

        for (const [value, label] of choices) {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;

            if (value === "12") {
                option.selected = true;
            }

            select.appendChild(option);
        }

        const form = document.getElementById("agentForm");

        if (form) {
            form.addEventListener("reset", () => {
                window.setTimeout(() => {
                    select.value = "12";
                }, 0);
            });
        }
    }

    function updateWorkspaceZip() {
        const list = document.getElementById("agentArtifactList");

        if (!list) {
            return;
        }

        const firstArtifact = list.querySelector(
            'a.agent-artifact-item[href*="/api/agents/artifacts/"]'
        );

        const section = list.closest(".agent-detail-section");

        if (!section) {
            return;
        }

        let link = section.querySelector(".atlas-workspace-zip");

        if (!firstArtifact) {
            if (link) {
                link.remove();
            }

            return;
        }

        const match = firstArtifact.href.match(
            /\/api\/agents\/artifacts\/([^/]+)\/content/
        );

        if (!match) {
            return;
        }

        if (!link) {
            link = document.createElement("a");
            link.className = "secondary-button compact-button atlas-workspace-zip";
            link.textContent = "Download all (.zip)";

            const heading = section.querySelector("h3");

            if (heading) {
                heading.insertAdjacentElement("afterend", link);
            } else {
                section.prepend(link);
            }
        }

        link.href = (
            `/api/agents/artifacts/${encodeURIComponent(match[1])}/workspace.zip`
        );
    }

    function addAdminOnboardingLink() {
        const adminHeading = Array.from(
            document.querySelectorAll("h1")
        ).find(
            (item) => (
                (item.textContent || "")
                .trim()
                .toLowerCase()
                === "admin control panel"
            )
        );

        if (
            !adminHeading
            || document.querySelector(".atlas-onboarding-admin-link")
        ) {
            return;
        }

        const headerRow = adminHeading.closest(".page-header-row");

        if (!headerRow) {
            return;
        }

        const link = document.createElement("a");
        link.href = "/admin/onboarding";
        link.className = (
            "secondary-button "
            + "atlas-onboarding-admin-link"
        );
        link.textContent = "Create internal user";

        headerRow.appendChild(link);
    }

    function clarifyAgentRunName() {
        const titleInput = document.getElementById("agentTitle");

        if (!titleInput) {
            return;
        }

        titleInput.placeholder = (
            "Optional — ATLAS can name the run from its goal"
        );
    }

    function getCookie(name) {
        const prefix = `${name}=`;

        for (const part of document.cookie.split(";")) {
            const value = part.trim();

            if (value.startsWith(prefix)) {
                return decodeURIComponent(
                    value.slice(prefix.length)
                );
            }
        }

        return "";
    }

    function setAgentIdentityCookie(identityId) {
        document.cookie = (
            "atlas_agent_identity="
            + encodeURIComponent(identityId || "")
            + "; path=/; SameSite=Lax"
        );
    }

    async function addAgentIdentitySelector() {
        const form = document.getElementById("agentForm");

        if (
            !form
            || form.dataset.atlasIdentityReady === "1"
            || form.dataset.atlasIdentityLoading === "1"
        ) {
            return;
        }

        form.dataset.atlasIdentityLoading = "1";

        try {
            const response = await fetch(
                "/api/agent-identities",
                {
                    headers: {
                        Accept: "application/json",
                    },
                },
            );

            if (!response.ok) {
                form.dataset.atlasIdentityLoading = "0";
                return;
            }

            const data = await response.json();
            const identities = data.identities || [];

            if (!identities.length) {
                form.dataset.atlasIdentityLoading = "0";
                return;
            }

            const row = document.createElement("div");
            row.className = "atlas-agent-identity-picker";

            const label = document.createElement("label");
            label.textContent = "Agent identity";

            const select = document.createElement("select");
            select.id = "atlasAgentIdentitySelect";

            for (const identity of identities) {
                const option = document.createElement("option");
                option.value = identity.id;
                option.textContent = (
                    identity.name
                    + (
                        identity.is_default
                            ? " · default"
                            : ""
                    )
                    + (
                        identity.memory_count
                            ? ` · ${identity.memory_count} memories`
                            : ""
                    )
                );

                select.appendChild(option);
            }

            const requested = getCookie(
                "atlas_agent_identity"
            );

            const valid = identities.some(
                (identity) => identity.id === requested
            );

            select.value = valid
                ? requested
                : (
                    data.default_id
                    || identities[0].id
                );

            setAgentIdentityCookie(
                select.value
            );

            select.addEventListener("change", () => {
                setAgentIdentityCookie(
                    select.value
                );
            });

            const manage = document.createElement("a");
            manage.href = "/agents/identities";
            manage.className = "atlas-agent-manage-link";
            manage.textContent = "Manage Agents";

            const note = document.createElement("small");
            note.className = "privacy-note";
            note.textContent = (
                "This Agent's own memory is isolated from personal memory. "
                + "Public web-query planning never receives Agent Memory."
            );

            label.appendChild(select);
            row.append(label, manage, note);

            const goalLabel = Array.from(
                form.querySelectorAll("label")
            ).find(
                (item) => (
                    (item.firstChild?.textContent || "")
                    .trim()
                    .toLowerCase()
                    === "goal"
                )
            );

            if (goalLabel) {
                form.insertBefore(
                    row,
                    goalLabel,
                );
            } else {
                form.prepend(row);
            }

            form.dataset.atlasIdentityReady = "1";
            form.dataset.atlasIdentityLoading = "0";

            form.addEventListener("reset", () => {
                window.setTimeout(() => {
                    const current = getCookie(
                        "atlas_agent_identity"
                    );

                    if (
                        current
                        && Array.from(select.options)
                            .some((option) => option.value === current)
                    ) {
                        select.value = current;
                    }
                }, 0);
            });
        } catch (_) {
            form.dataset.atlasIdentityLoading = "0";
        }
    }

    function addAgentIdentityNavigation() {
        const agentBack = document.querySelector(".agent-back-link");

        if (
            !agentBack
            || document.querySelector(".atlas-agent-identities-link")
        ) {
            return;
        }

        const link = document.createElement("a");
        link.href = "/agents/identities";
        link.className = "atlas-agent-identities-link";
        link.textContent = "Agents";

        agentBack.insertAdjacentElement(
            "afterend",
            link,
        );
    }

    function addProductSignature() {
        const sidebarUser = document.querySelector(".sidebar-user");

        if (
            sidebarUser
            && !sidebarUser.parentElement.querySelector(".atlas-product-signature")
        ) {
            const signature = document.createElement("div");
            signature.className = "atlas-product-signature";
            signature.textContent = `${product} by ${company}`;
            sidebarUser.insertAdjacentElement("afterend", signature);
        }
    }

    function applyAll() {
        applyBrand();
        addLibraryNavigation();
        upgradeAgentBudget();
        updateWorkspaceZip();
        addAdminOnboardingLink();
        clarifyAgentRunName();
        addAgentIdentitySelector();
        addAgentIdentityNavigation();
        addProductSignature();
    }

    applyAll();

    const observer = new MutationObserver(() => {
        applyAll();
    });

    observer.observe(
        document.body,
        {
            childList: true,
            subtree: true,
        },
    );
})();
