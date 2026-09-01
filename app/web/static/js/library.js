(() => {
    "use strict";

    const app = document.getElementById("libraryApp");

    if (!app) {
        return;
    }

    const csrfToken = (
        document.querySelector(
            'meta[name="csrf-token"]'
        )?.content
        || ""
    );

    const elements = {
        search: document.getElementById("librarySearch"),
        upload: document.getElementById("libraryUploadInput"),
        addLink: document.getElementById("libraryAddLinkButton"),
        linkPanel: document.getElementById("libraryLinkPanel"),
        linkForm: document.getElementById("libraryLinkForm"),
        linkTitle: document.getElementById("libraryLinkTitle"),
        linkUrl: document.getElementById("libraryLinkUrl"),
        cancelLink: document.getElementById("libraryCancelLinkButton"),
        kindFilters: document.getElementById("libraryKindFilters"),
        origin: document.getElementById("libraryOriginFilter"),
        favorites: document.getElementById("libraryFavoritesOnly"),
        refresh: document.getElementById("libraryRefreshButton"),
        notice: document.getElementById("libraryNotice"),
        count: document.getElementById("libraryResultCount"),
        grid: document.getElementById("libraryGrid"),
    };

    const state = {
        kind: "",
        query: "",
        origin: "",
        favorites: false,
        timer: null,
        busy: false,
    };

    function notice(message, kind = "info") {
        elements.notice.textContent = message || "";
        elements.notice.dataset.kind = kind;
        elements.notice.hidden = !message;
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

    function bytes(value) {
        const size = Number(value || 0);

        if (!size) {
            return "";
        }

        if (size < 1024) {
            return `${size} B`;
        }

        if (size < 1024 * 1024) {
            return `${Math.round(size / 1024)} KB`;
        }

        if (size < 1024 * 1024 * 1024) {
            return `${(size / (1024 * 1024)).toFixed(1)} MB`;
        }

        return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    }

    function dateLabel(value) {
        if (!value) {
            return "";
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return "";
        }

        return date.toLocaleDateString();
    }

    function originLabel(origin) {
        const labels = {
            upload: "Upload",
            chat: "Chat",
            generated: "Generated",
            agent: "Agent",
            link: "Link",
        };

        return labels[origin] || origin || "Resource";
    }

    function iconLabel(kind) {
        const labels = {
            image: "IMG",
            document: "DOC",
            code: "</>",
            data: "DATA",
            audio: "♫",
            video: "▶",
            link: "↗",
            archive: "ZIP",
            other: "FILE",
        };

        return labels[kind] || "FILE";
    }

    function card(item) {
        const article = document.createElement("article");
        article.className = "library-card";

        const preview = document.createElement("div");
        preview.className = "library-preview";

        if (
            item.kind === "image"
            && item.content_url
        ) {
            const image = document.createElement("img");
            image.src = item.content_url;
            image.alt = "";
            image.loading = "lazy";
            preview.appendChild(image);
        } else {
            const icon = document.createElement("div");
            icon.className = "library-file-icon";
            icon.textContent = iconLabel(item.kind);
            preview.appendChild(icon);
        }

        const body = document.createElement("div");
        body.className = "library-card-body";

        const top = document.createElement("div");
        top.className = "library-card-top";

        const name = document.createElement("h3");
        name.className = "library-card-name";
        name.textContent = item.name || "Resource";
        name.title = item.name || "";

        const favorite = document.createElement("button");
        favorite.type = "button";
        favorite.className = (
            "library-favorite-button"
            + (item.favorite ? " active" : "")
        );
        favorite.textContent = item.favorite ? "★" : "☆";
        favorite.title = item.favorite
            ? "Remove from favorites"
            : "Add to favorites";

        favorite.addEventListener("click", async () => {
            try {
                await api(
                    `/api/library/items/${encodeURIComponent(item.id)}`,
                    {
                        method: "PATCH",
                        body: {
                            favorite: !item.favorite,
                        },
                    },
                );

                await load();
            } catch (error) {
                notice(error.message, "error");
            }
        });

        top.append(name, favorite);

        const meta = document.createElement("div");
        meta.className = "library-card-meta";

        const pill = document.createElement("span");
        pill.className = "library-origin-pill";
        pill.textContent = originLabel(item.origin);

        const detail = document.createElement("span");

        const details = [
            item.kind,
            bytes(item.size_bytes),
            dateLabel(item.created_at),
        ].filter(Boolean);

        detail.textContent = details.join(" · ");

        meta.append(pill, detail);

        const runTitle = (
            item.metadata?.run_title
            || item.metadata?.conversation_title
            || ""
        );

        if (runTitle) {
            const source = document.createElement("div");
            source.textContent = runTitle;
            source.title = runTitle;
            meta.appendChild(source);
        }

        const actions = document.createElement("div");
        actions.className = "library-card-actions";

        if (item.external_url) {
            const open = document.createElement("a");
            open.href = item.external_url;
            open.target = "_blank";
            open.rel = "noopener noreferrer";
            open.textContent = "Open link";
            actions.appendChild(open);
        } else if (item.download_url) {
            const download = document.createElement("a");
            download.href = item.download_url;
            download.textContent = "Download";
            actions.appendChild(download);
        }

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "library-remove";
        remove.textContent = "×";
        remove.title = (
            item.origin === "upload"
            || item.origin === "link"
        )
            ? "Delete"
            : "Hide from Library";

        remove.addEventListener("click", async () => {
            const verb = (
                item.origin === "upload"
                || item.origin === "link"
            )
                ? "Delete this Library item?"
                : "Hide this derived resource from Library? The original chat/agent file will remain.";

            if (!window.confirm(verb)) {
                return;
            }

            try {
                await api(
                    `/api/library/items/${encodeURIComponent(item.id)}`,
                    {
                        method: "DELETE",
                    },
                );

                await load();
            } catch (error) {
                notice(error.message, "error");
            }
        });

        actions.appendChild(remove);

        body.append(top, meta, actions);
        article.append(preview, body);

        return article;
    }

    function render(items) {
        elements.grid.replaceChildren();
        elements.count.textContent = String(items.length);

        if (!items.length) {
            const empty = document.createElement("div");
            empty.className = "library-empty";
            empty.textContent = (
                "No resources match these filters. "
                + "Upload a file, save a link, or create something in chat/Agent Workspace."
            );
            elements.grid.appendChild(empty);
            return;
        }

        for (const item of items) {
            elements.grid.appendChild(card(item));
        }
    }

    function queryString() {
        const params = new URLSearchParams();

        if (state.query) {
            params.set("q", state.query);
        }

        if (state.kind) {
            params.set("kind", state.kind);
        }

        if (state.origin) {
            params.set("origin", state.origin);
        }

        if (state.favorites) {
            params.set("favorites", "1");
        }

        return params.toString();
    }

    async function load() {
        try {
            const qs = queryString();
            const data = await api(
                `/api/library/items${qs ? `?${qs}` : ""}`
            );

            render(data.items || []);
            notice("");
        } catch (error) {
            notice(error.message, "error");
        }
    }

    async function uploadFile(file) {
        const body = new FormData();
        body.append("file", file);

        const response = await fetch(
            "/api/library/upload",
            {
                method: "POST",
                headers: {
                    "X-CSRF-Token": csrfToken,
                    Accept: "application/json",
                },
                body,
            },
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
                || `Upload failed (${response.status}).`
            );
        }

        return data;
    }

    elements.upload.addEventListener("change", async () => {
        const files = Array.from(
            elements.upload.files || []
        );

        elements.upload.value = "";

        if (!files.length || state.busy) {
            return;
        }

        state.busy = true;

        try {
            for (let index = 0; index < files.length; index += 1) {
                notice(
                    `Uploading ${index + 1}/${files.length}: ${files[index].name}`,
                    "info",
                );

                await uploadFile(files[index]);
            }

            notice(
                `${files.length} Library upload${files.length === 1 ? "" : "s"} saved.`,
                "success",
            );

            await load();
        } catch (error) {
            notice(error.message, "error");
        } finally {
            state.busy = false;
        }
    });

    elements.addLink.addEventListener("click", () => {
        elements.linkPanel.hidden = false;
        elements.linkUrl.focus();
    });

    elements.cancelLink.addEventListener("click", () => {
        elements.linkPanel.hidden = true;
        elements.linkForm.reset();
    });

    elements.linkForm.addEventListener("submit", async (event) => {
        event.preventDefault();

        try {
            await api("/api/library/links", {
                method: "POST",
                body: {
                    title: elements.linkTitle.value.trim(),
                    url: elements.linkUrl.value.trim(),
                },
            });

            elements.linkForm.reset();
            elements.linkPanel.hidden = true;

            notice("Link saved to Library.", "success");

            await load();
        } catch (error) {
            notice(error.message, "error");
        }
    });

    elements.kindFilters.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-kind]");

        if (!button) {
            return;
        }

        state.kind = button.dataset.kind || "";

        for (const item of elements.kindFilters.querySelectorAll("button")) {
            item.classList.toggle(
                "active",
                item === button,
            );
        }

        load();
    });

    elements.origin.addEventListener("change", () => {
        state.origin = elements.origin.value;
        load();
    });

    elements.favorites.addEventListener("change", () => {
        state.favorites = elements.favorites.checked;
        load();
    });

    elements.search.addEventListener("input", () => {
        state.query = elements.search.value.trim();

        window.clearTimeout(state.timer);

        state.timer = window.setTimeout(
            load,
            220,
        );
    });

    elements.refresh.addEventListener("click", load);

    load();
})();
