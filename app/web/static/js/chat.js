(() => {
    "use strict";

    const app = document.getElementById(
        "chatApp"
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

    const sidebar = document.getElementById(
        "sidebar"
    );

    const sidebarOverlay = document.getElementById(
        "sidebarOverlay"
    );

    const mobileMenuButton = document.getElementById(
        "mobileMenuButton"
    );

    const desktopSidebarCollapseButton =
        document.getElementById(
            "desktopSidebarCollapseButton"
        );

    const desktopSidebarExpandButton =
        document.getElementById(
            "desktopSidebarExpandButton"
        );

    const sidebarResizeHandle =
        document.getElementById(
            "sidebarResizeHandle"
        );

    const newChatButton = document.getElementById(
        "newChatButton"
    );

    const conversationList = document.getElementById(
        "conversationList"
    );

    const messages = document.getElementById(
        "messages"
    );

    const welcomeState = document.getElementById(
        "welcomeState"
    );

    const form = document.getElementById(
        "composerForm"
    );

    const input = document.getElementById(
        "messageInput"
    );

    const modelSelect = document.getElementById(
        "modelSelect"
    );

    const sendButton = document.getElementById(
        "sendButton"
    );

    const stopButton = document.getElementById(
        "stopButton"
    );

    const attachmentButton = document.getElementById(
        "attachmentButton"
    );

    const attachmentInput = document.getElementById(
        "attachmentInput"
    );

    const pendingAttachmentsElement =
        document.getElementById(
            "pendingAttachments"
        );

    const microphoneButton = document.getElementById(
        "microphoneButton"
    );

    const attachmentNotice = document.getElementById(
        "attachmentNotice"
    );

    const SIDEBAR_WIDTH_KEY =
        "private_ai_sidebar_width";

    const SIDEBAR_COLLAPSED_KEY =
        "private_ai_sidebar_collapsed";

    const MIN_SIDEBAR_WIDTH = 220;
    const MAX_SIDEBAR_WIDTH = 380;
    const DEFAULT_SIDEBAR_WIDTH = 280;

    const MAX_ATTACHMENTS = 4;
    const MAX_ATTACHMENT_BYTES = (
        25 * 1024 * 1024
    );

    let currentConversationId = null;
    let activeController = null;
    let generating = false;
    let resizeActive = false;
    let attachmentUploadActive = false;
    let pendingAttachments = [];

    modelSelect.value = (
        app.dataset.defaultModel
        || "auto"
    );

    const ACTIVITY_LABELS = {
        preparing: "Preparing...",
        routing: "Routing...",
        generating: "Generating...",
        thinking: "Thinking...",
        responding: "Responding...",
        formatting: "Formatting...",
        naming: "Naming chat...",
        memory: "Updating memory...",
        searching: "Searching web...",
        reading: "Reading sources...",
        analyzing_image: "Analyzing image...",
        generating_image: "Generating image...",
        uploading: "Uploading...",
        transcribing: "Transcribing...",
        speaking: "Speaking...",
        working: "Working...",
    };


    // =====================================================
    // UTILITIES
    // =====================================================

    function isMobile() {
        return window.matchMedia(
            "(max-width: 760px)"
        ).matches;
    }


    function syncViewportHeight() {
        if (!isMobile()) {
            document.documentElement
            .style.removeProperty(
                "--app-height"
            );

            return;
        }

        const viewportHeight = (
            window.visualViewport
            ?.height
            || window.innerHeight
        );

        document.documentElement
        .style.setProperty(
            "--app-height",
            `${Math.round(
                viewportHeight
            )}px`
        );
    }


    function clampSidebarWidth(width) {
        return Math.max(
            MIN_SIDEBAR_WIDTH,
            Math.min(
                MAX_SIDEBAR_WIDTH,
                width
            )
        );
    }


    function scrollToBottom() {
        messages.scrollTop = (
            messages.scrollHeight
        );
    }


    function hideWelcome() {
        if (welcomeState) {
            welcomeState.hidden = true;
        }
    }


    function showWelcome() {
        if (welcomeState) {
            welcomeState.hidden = false;
        }
    }


    function closeMobileSidebar() {
        sidebar.classList.remove(
            "open"
        );

        sidebarOverlay.classList.remove(
            "open"
        );
    }


    function openMobileSidebar() {
        sidebar.classList.add(
            "open"
        );

        sidebarOverlay.classList.add(
            "open"
        );
    }


    function setDesktopSidebarCollapsed(
        collapsed,
        persist = true
    ) {
        app.classList.toggle(
            "sidebar-collapsed",
            Boolean(collapsed)
        );

        if (persist) {
            localStorage.setItem(
                SIDEBAR_COLLAPSED_KEY,
                collapsed
                    ? "1"
                    : "0"
            );
        }
    }


    function setSidebarWidth(
        width,
        persist = true
    ) {
        const safeWidth = (
            clampSidebarWidth(
                Number(width)
                || DEFAULT_SIDEBAR_WIDTH
            )
        );

        app.style.setProperty(
            "--sidebar-width",
            `${safeWidth}px`
        );

        if (persist) {
            localStorage.setItem(
                SIDEBAR_WIDTH_KEY,
                String(safeWidth)
            );
        }
    }


    function applySidebarPreferences() {
        const savedWidth = Number(
            localStorage.getItem(
                SIDEBAR_WIDTH_KEY
            )
        );

        setSidebarWidth(
            savedWidth
            || DEFAULT_SIDEBAR_WIDTH,
            false
        );

        const collapsed = (
            localStorage.getItem(
                SIDEBAR_COLLAPSED_KEY
            )
            === "1"
        );

        setDesktopSidebarCollapsed(
            collapsed,
            false
        );
    }


    function showNotice(message) {
        attachmentNotice.textContent = (
            message
        );

        attachmentNotice.hidden = false;

        window.setTimeout(
            () => {
                attachmentNotice.hidden = true;
            },
            3200
        );
    }


    function closeConversationMenus() {
        document
        .querySelectorAll(
            ".conversation-menu.open"
        )
        .forEach(
            (menu) => {
                menu.classList.remove(
                    "open"
                );
            }
        );
    }


    async function requestJson(
        url,
        options = {}
    ) {
        const headers = new Headers(
            options.headers || {}
        );

        if (
            options.method
            && options.method !== "GET"
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
            window.location.href = (
                "/login"
            );

            throw new Error(
                "Authentication required."
            );
        }

        if (!response.ok) {
            let message = (
                `Request failed (${response.status})`
            );

            try {
                const data = (
                    await response.json()
                );

                message = (
                    data.error
                    || message
                );

            } catch (_) {
                // Keep fallback.
            }

            throw new Error(
                message
            );
        }

        return response.json();
    }


    function autoResizeInput() {
        input.style.height = "auto";

        input.style.height = (
            Math.min(
                input.scrollHeight,
                180
            )
            + "px"
        );
    }


    // =====================================================
    // ATTACHMENTS
    // =====================================================

    function formatFileSize(bytes) {
        const value = Number(bytes || 0);

        if (value < 1024) {
            return `${value} B`;
        }

        if (value < 1024 * 1024) {
            return (
                `${(value / 1024).toFixed(1)} KB`
            );
        }

        return (
            `${(value / (1024 * 1024)).toFixed(1)} MB`
        );
    }


    function attachmentTypeLabel(
        attachment
    ) {
        const name = (
            attachment.name
            || "Attachment"
        );

        const extension = (
            name.includes(".")
            ? name.split(".").pop().toUpperCase()
            : "FILE"
        );

        if (attachment.kind === "image") {
            return "Image";
        }

        return extension;
    }


    function createAttachmentCard(
        attachment,
        removable = false
    ) {
        const card = document.createElement(
            removable ? "div" : "a"
        );

        card.className = (
            "attachment-card"
            + (
                removable
                ? " pending-attachment-card"
                : ""
            )
        );

        if (!removable) {
            card.href = (
                attachment.content_url
                || "#"
            );
            card.target = "_blank";
            card.rel = (
                "noopener noreferrer"
            );
        }

        const visual = document.createElement(
            "div"
        );

        visual.className = (
            "attachment-visual"
        );

        if (
            attachment.kind === "image"
            && attachment.content_url
        ) {
            const image = document.createElement(
                "img"
            );

            image.src = attachment.content_url;
            image.alt = "";
            image.loading = "lazy";

            visual.appendChild(image);

        } else {
            const badge = document.createElement(
                "span"
            );

            badge.textContent = (
                attachmentTypeLabel(
                    attachment
                )
            );

            visual.appendChild(badge);
        }

        const meta = document.createElement(
            "div"
        );

        meta.className = (
            "attachment-meta"
        );

        const name = document.createElement(
            "strong"
        );

        name.className = (
            "attachment-name"
        );

        name.textContent = (
            attachment.name
            || "Attachment"
        );

        const detail = document.createElement(
            "small"
        );

        detail.textContent = (
            attachmentTypeLabel(
                attachment
            )
            + " · "
            + formatFileSize(
                attachment.size_bytes
            )
        );

        meta.append(
            name,
            detail
        );

        card.append(
            visual,
            meta
        );

        if (removable) {
            const remove = document.createElement(
                "button"
            );

            remove.type = "button";
            remove.className = (
                "attachment-remove-button"
            );
            remove.setAttribute(
                "aria-label",
                `Remove ${attachment.name || "attachment"}`
            );
            remove.title = "Remove attachment";
            remove.textContent = "×";

            remove.addEventListener(
                "click",
                () => {
                    removePendingAttachment(
                        attachment.id
                    );
                }
            );

            card.appendChild(remove);
        }

        return card;
    }


    function appendMessageAttachments(
        container,
        attachments
    ) {
        if (!attachments?.length) {
            return;
        }

        const list = document.createElement(
            "div"
        );

        list.className = (
            "message-attachments"
        );

        for (const attachment of attachments) {
            list.appendChild(
                createAttachmentCard(
                    attachment,
                    false
                )
            );
        }

        container.appendChild(list);
    }


    function renderPendingAttachments() {
        pendingAttachmentsElement.innerHTML = "";

        if (!pendingAttachments.length) {
            pendingAttachmentsElement.hidden = true;
            return;
        }

        pendingAttachmentsElement.hidden = false;

        for (
            const attachment
            of pendingAttachments
        ) {
            pendingAttachmentsElement.appendChild(
                createAttachmentCard(
                    attachment,
                    true
                )
            );
        }
    }


    async function removePendingAttachment(
        attachmentId
    ) {
        const attachment = (
            pendingAttachments.find(
                (item) => (
                    item.id === attachmentId
                )
            )
        );

        pendingAttachments = (
            pendingAttachments.filter(
                (item) => (
                    item.id !== attachmentId
                )
            )
        );

        renderPendingAttachments();

        if (!attachment) {
            return;
        }

        try {
            await requestJson(
                (
                    "/api/attachments/"
                    + encodeURIComponent(
                        attachmentId
                    )
                ),
                {
                    method: "DELETE",
                }
            );

        } catch (_) {
            // Stale pending files are cleaned server-side.
        }
    }


    function discardPendingAttachments() {
        const stale = [
            ...pendingAttachments
        ];

        pendingAttachments = [];
        renderPendingAttachments();

        for (const attachment of stale) {
            fetch(
                (
                    "/api/attachments/"
                    + encodeURIComponent(
                        attachment.id
                    )
                ),
                {
                    method: "DELETE",
                    headers: {
                        "X-CSRF-Token":
                            csrfToken,
                    },
                    keepalive: true,
                }
            ).catch(
                () => {}
            );
        }
    }


    function uploadErrorMessage(code) {
        const messagesByCode = {
            unsupported_file_type:
                "That file type is not supported yet.",
            file_too_large:
                "That file is larger than 25 MB.",
            empty_file:
                "That file is empty.",
            file_required:
                "No file was selected.",
            conversation_not_found:
                "That chat is no longer available.",
        };

        return (
            messagesByCode[code]
            || code
            || "Attachment upload failed."
        );
    }


    async function uploadAttachment(file) {
        const formData = new FormData();

        formData.append(
            "file",
            file
        );

        if (currentConversationId) {
            formData.append(
                "conversation_id",
                currentConversationId
            );
        }

        const response = await fetch(
            "/api/attachments",
            {
                method: "POST",
                headers: {
                    "X-CSRF-Token":
                        csrfToken,
                },
                body: formData,
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
            // Keep fallback error.
        }

        if (!response.ok) {
            throw new Error(
                uploadErrorMessage(
                    data.error
                )
            );
        }

        return data.attachment;
    }


    async function uploadFiles(fileList) {
        if (
            generating
            || attachmentUploadActive
        ) {
            return;
        }

        const files = Array.from(
            fileList || []
        );

        if (!files.length) {
            return;
        }

        const slots = (
            MAX_ATTACHMENTS
            - pendingAttachments.length
        );

        if (slots <= 0) {
            showNotice(
                `Up to ${MAX_ATTACHMENTS} attachments per message.`
            );
            return;
        }

        const selected = files.slice(
            0,
            slots
        );

        if (files.length > slots) {
            showNotice(
                `Only ${MAX_ATTACHMENTS} attachments can be added to one message.`
            );
        }

        attachmentUploadActive = true;
        attachmentButton.disabled = true;

        try {
            for (const file of selected) {
                if (file.size > MAX_ATTACHMENT_BYTES) {
                    showNotice(
                        `${file.name} is larger than 25 MB.`
                    );
                    continue;
                }

                showNotice(
                    `Uploading ${file.name}...`
                );

                try {
                    const attachment = (
                        await uploadAttachment(
                            file
                        )
                    );

                    pendingAttachments.push(
                        attachment
                    );

                    renderPendingAttachments();

                } catch (error) {
                    showNotice(
                        error.message
                    );
                }
            }

        } finally {
            attachmentUploadActive = false;
            attachmentButton.disabled = false;

            if (pendingAttachments.length) {
                showNotice(
                    "Attachment stored locally. File analysis is connected in the next capability step."
                );
            }
        }
    }


    // =====================================================
    // MESSAGE RENDERING
    // =====================================================

    function createUserMessage(
        text,
        attachments = []
    ) {
        hideWelcome();

        const article = document.createElement(
            "article"
        );

        article.className = (
            "message user-message"
        );

        const inner = document.createElement(
            "div"
        );

        inner.className = (
            "message-inner"
        );

        const content = document.createElement(
            "div"
        );

        content.className = (
            "message-text"
        );

        content.textContent = text;

        appendMessageAttachments(
            inner,
            attachments
        );

        inner.appendChild(
            content
        );

        article.appendChild(
            inner
        );

        messages.appendChild(
            article
        );

        scrollToBottom();

        return article;
    }


    function createAssistantMessage() {
        hideWelcome();

        const article = document.createElement(
            "article"
        );

        article.className = (
            "message assistant-message"
        );

        const inner = document.createElement(
            "div"
        );

        inner.className = (
            "message-inner"
        );

        const meta = document.createElement(
            "div"
        );

        meta.className = (
            "assistant-meta"
        );

        const modelTag = document.createElement(
            "span"
        );

        modelTag.className = (
            "model-tag"
        );

        meta.appendChild(
            modelTag
        );

        const thinking = document.createElement(
            "details"
        );

        thinking.className = (
            "thinking-panel"
        );

        thinking.hidden = true;

        const summary = document.createElement(
            "summary"
        );

        summary.textContent = (
            "Thinking..."
        );

        const thinkingContent =
            document.createElement(
                "div"
            );

        thinkingContent.className = (
            "thinking-content"
        );

        thinking.append(
            summary,
            thinkingContent
        );

        const content = document.createElement(
            "div"
        );

        content.className = (
            "message-text "
            + "assistant-content "
            + "streaming-plain"
        );

        const activity = document.createElement(
            "div"
        );

        activity.className = (
            "activity-row"
        );

        activity.dataset.phase = (
            "preparing"
        );

        const activityIndicator =
            document.createElement(
                "span"
            );

        activityIndicator.className = (
            "activity-indicator"
        );

        activityIndicator.setAttribute(
            "aria-hidden",
            "true"
        );

        const activityText =
            document.createElement(
                "span"
            );

        activityText.className = (
            "activity-text"
        );

        const activityLabel =
            document.createElement(
                "span"
            );

        activityLabel.className = (
            "activity-label"
        );

        activityLabel.textContent = (
            ACTIVITY_LABELS.preparing
        );

        const activityDetail =
            document.createElement(
                "span"
            );

        activityDetail.className = (
            "activity-detail"
        );

        activityDetail.hidden = true;

        activityText.append(
            activityLabel,
            activityDetail
        );

        activity.append(
            activityIndicator,
            activityText
        );

        inner.append(
            meta,
            thinking,
            content,
            activity
        );

        article.appendChild(
            inner
        );

        messages.appendChild(
            article
        );

        scrollToBottom();

        return {
            article,
            modelTag,
            thinking,
            thinkingSummary: summary,
            thinkingContent,
            content,
            activity,
            activityLabel,
            activityDetail,
        };
    }


    function createImageGenerationPlaceholder(
        label = "Generating image..."
    ) {
        const template = document.getElementById(
            "imageGenerationTemplate"
        );

        const fragment = (
            template.content.cloneNode(
                true
            )
        );

        const card = fragment.querySelector(
            ".image-generation-card"
        );

        const labelElement =
            fragment.querySelector(
                ".image-generation-label"
            );

        labelElement.textContent = (
            label
        );

        messages.appendChild(
            fragment
        );

        scrollToBottom();

        return card;
    }


    function setActivity(
        assistant,
        phase = "working",
        label = null,
        detail = null
    ) {
        const safePhase = (
            String(
                phase
                || "working"
            )
            .trim()
            .toLowerCase()
            || "working"
        );

        assistant.activity.hidden = false;

        assistant.activity.dataset.phase = (
            safePhase
        );

        assistant.activityLabel.textContent = (
            label
            || ACTIVITY_LABELS[safePhase]
            || ACTIVITY_LABELS.working
        );

        if (detail) {
            assistant.activityDetail.textContent = (
                String(detail)
            );

            assistant.activityDetail.hidden = false;

        } else {
            assistant.activityDetail.textContent = "";
            assistant.activityDetail.hidden = true;
        }

        if (
            safePhase !== "thinking"
            && !assistant.thinking.hidden
        ) {
            assistant.thinkingSummary.textContent = (
                "Thinking"
            );
        }

        scrollToBottom();
    }


    function finishActivity(
        assistant
    ) {
        assistant.activity.hidden = true;

        if (!assistant.thinking.hidden) {
            assistant.thinkingSummary.textContent = (
                "Thinking"
            );
        }
    }


    function handleActivityEvent(
        event,
        assistant
    ) {
        const state = String(
            event.state
            || "update"
        ).toLowerCase();

        if (
            state === "end"
            || state === "done"
        ) {
            finishActivity(
                assistant
            );

            return;
        }

        setActivity(
            assistant,
            event.phase
            || event.status
            || "working",
            event.label,
            event.detail
        );
    }


    function setStatus(
        assistant,
        label,
        phase = "working"
    ) {
        setActivity(
            assistant,
            phase,
            label
        );
    }


    function finishStatus(
        assistant
    ) {
        finishActivity(
            assistant
        );
    }


    function enhanceRenderedContent(
        container
    ) {
        container
        .querySelectorAll("a")
        .forEach(
            (link) => {
                link.target = "_blank";
                link.rel = (
                    "noopener noreferrer"
                );
            }
        );
    }


    function applyRenderedHtml(
        container,
        renderedHtml
    ) {
        if (
            typeof renderedHtml
            !== "string"
        ) {
            return;
        }

        container.innerHTML = (
            renderedHtml
        );

        container.classList.remove(
            "streaming-plain"
        );

        container.classList.add(
            "rendered-markdown"
        );

        enhanceRenderedContent(
            container
        );
    }


    async function copyCodeBlock(
        button
    ) {
        const codeBlock = button.closest(
            ".code-block"
        );

        const code = (
            codeBlock
            ?.querySelector(
                "code"
            )
        );

        if (!code) {
            return;
        }

        const text = (
            code.textContent
            || ""
        );

        let copied = false;

        try {
            await navigator.clipboard
            .writeText(
                text
            );

            copied = true;

        } catch (_) {
            const textarea = (
                document.createElement(
                    "textarea"
                )
            );

            textarea.value = text;
            textarea.setAttribute(
                "readonly",
                ""
            );

            textarea.style.position = (
                "fixed"
            );

            textarea.style.opacity = "0";

            document.body.appendChild(
                textarea
            );

            textarea.select();

            try {
                copied = document.execCommand(
                    "copy"
                );

            } catch (_) {
                copied = false;
            }

            textarea.remove();
        }

        if (!copied) {
            showNotice(
                "Could not copy code."
            );

            return;
        }

        const originalLabel = (
            button.textContent
        );

        button.textContent = (
            "Copied"
        );

        window.setTimeout(
            () => {
                button.textContent = (
                    originalLabel
                );
            },
            1400
        );
    }


    // =====================================================
    // CONVERSATION ACTIONS
    // =====================================================

    async function renameConversation(
        conversation
    ) {
        closeConversationMenus();

        const requestedTitle = window.prompt(
            "Rename chat:",
            conversation.title
            || "New Chat"
        );

        if (requestedTitle === null) {
            return;
        }

        const title = requestedTitle.trim();

        if (!title) {
            showNotice(
                "Chat name cannot be empty."
            );

            return;
        }

        try {
            await requestJson(
                (
                    "/api/conversations/"
                    + conversation.id
                ),
                {
                    method: "PATCH",

                    headers: {
                        "Content-Type":
                            "application/json",
                    },

                    body: JSON.stringify({
                        title,
                    }),
                }
            );

            await refreshConversations();

        } catch (error) {
            showNotice(
                error.message
            );
        }
    }


    async function removeConversation(
        conversation
    ) {
        closeConversationMenus();

        const confirmed = window.confirm(
            (
                'Delete "'
                + (
                    conversation.title
                    || "New Chat"
                )
                + '"?\n\n'
                + "This removes the chat history permanently."
            )
        );

        if (!confirmed) {
            return;
        }

        try {
            await requestJson(
                (
                    "/api/conversations/"
                    + conversation.id
                ),
                {
                    method: "DELETE",
                }
            );

            if (
                conversation.id
                === currentConversationId
            ) {
                startNewChat();
            }

            await refreshConversations();

        } catch (error) {
            showNotice(
                error.message
            );
        }
    }


    function createConversationRow(
        conversation
    ) {
        const row = document.createElement(
            "div"
        );

        row.className = (
            "conversation-row"
        );

        if (
            conversation.id
            === currentConversationId
        ) {
            row.classList.add(
                "active"
            );
        }

        const openButton = document.createElement(
            "button"
        );

        openButton.type = "button";
        openButton.className = (
            "conversation-item"
        );

        openButton.textContent = (
            conversation.title
            || "New Chat"
        );

        openButton.title = (
            conversation.title
            || "New Chat"
        );

        openButton.addEventListener(
            "click",
            () => {
                loadConversation(
                    conversation.id
                );
            }
        );

        const menuWrap = document.createElement(
            "div"
        );

        menuWrap.className = (
            "conversation-menu-wrap"
        );

        const menuButton = document.createElement(
            "button"
        );

        menuButton.type = "button";
        menuButton.className = (
            "conversation-menu-button"
        );

        menuButton.textContent = "⋯";
        menuButton.setAttribute(
            "aria-label",
            (
                "Chat options for "
                + (
                    conversation.title
                    || "New Chat"
                )
            )
        );

        const menu = document.createElement(
            "div"
        );

        menu.className = (
            "conversation-menu"
        );

        const renameButton =
            document.createElement(
                "button"
            );

        renameButton.type = "button";
        renameButton.textContent = (
            "Rename"
        );

        renameButton.addEventListener(
            "click",
            () => {
                renameConversation(
                    conversation
                );
            }
        );

        const deleteButton =
            document.createElement(
                "button"
            );

        deleteButton.type = "button";
        deleteButton.className = (
            "danger"
        );

        deleteButton.textContent = (
            "Delete"
        );

        deleteButton.addEventListener(
            "click",
            () => {
                removeConversation(
                    conversation
                );
            }
        );

        menu.append(
            renameButton,
            deleteButton
        );

        menuButton.addEventListener(
            "click",
            (event) => {
                event.stopPropagation();

                const wasOpen =
                    menu.classList.contains(
                        "open"
                    );

                closeConversationMenus();

                if (!wasOpen) {
                    menu.classList.add(
                        "open"
                    );
                }
            }
        );

        menuWrap.append(
            menuButton,
            menu
        );

        row.append(
            openButton,
            menuWrap
        );

        return row;
    }


    // =====================================================
    // CONVERSATIONS
    // =====================================================

    async function refreshConversations() {
        try {
            const data = await requestJson(
                "/api/conversations?limit=100"
            );

            conversationList.innerHTML = "";

            if (
                !data.conversations.length
            ) {
                const empty = document.createElement(
                    "div"
                );

                empty.className = (
                    "sidebar-empty"
                );

                empty.textContent = (
                    "No chats yet"
                );

                conversationList.appendChild(
                    empty
                );

                return;
            }

            for (
                const conversation
                of data.conversations
            ) {
                conversationList.appendChild(
                    createConversationRow(
                        conversation
                    )
                );
            }

        } catch (error) {
            conversationList.innerHTML = "";

            const failed = document.createElement(
                "div"
            );

            failed.className = (
                "sidebar-empty"
            );

            failed.textContent = (
                "Could not load chats"
            );

            conversationList.appendChild(
                failed
            );
        }
    }


    async function loadConversation(
        conversationId
    ) {
        if (generating) {
            return;
        }

        if (
            pendingAttachments.length
            && conversationId !== currentConversationId
        ) {
            discardPendingAttachments();
        }

        try {
            const data = await requestJson(
                (
                    "/api/conversations/"
                    + conversationId
                    + "/messages"
                )
            );

            currentConversationId = (
                conversationId
            );

            messages.innerHTML = "";

            if (!data.messages.length) {
                messages.appendChild(
                    welcomeState
                );

                showWelcome();

            } else {
                for (
                    const message
                    of data.messages
                ) {
                    if (
                        message.role
                        === "user"
                    ) {
                        createUserMessage(
                            message.content,
                            message.attachments
                            || []
                        );

                    } else {
                        const assistant =
                            createAssistantMessage();

                        if (
                            message.rendered_html
                        ) {
                            applyRenderedHtml(
                                assistant.content,
                                message.rendered_html
                            );

                        } else {
                            assistant.content.textContent = (
                                message.content
                            );
                        }

                        finishStatus(
                            assistant
                        );
                    }
                }
            }

            await refreshConversations();

            closeMobileSidebar();

            input.focus();

        } catch (error) {
            showNotice(
                error.message
            );
        }
    }


    function startNewChat() {
        if (generating) {
            return;
        }

        if (pendingAttachments.length) {
            discardPendingAttachments();
        }

        currentConversationId = null;

        messages.innerHTML = "";

        messages.appendChild(
            welcomeState
        );

        showWelcome();

        refreshConversations();

        closeMobileSidebar();

        input.focus();
    }


    // =====================================================
    // STREAMING CHAT
    // =====================================================

    function handleStreamEvent(
        event,
        assistant
    ) {
        switch (event.type) {
            case "conversation":
                currentConversationId = (
                    event.conversation_id
                );

                refreshConversations();

                break;

            case "conversation_title":
                refreshConversations();

                break;

            case "status":
                setActivity(
                    assistant,
                    event.status
                    || "working",
                    event.label
                );

                break;

            case "activity":
                handleActivityEvent(
                    event,
                    assistant
                );

                break;

            case "route":
                assistant.modelTag.textContent = (
                    `${event.mode} · ${event.model}`
                );

                assistant.modelTag.classList.add(
                    "visible"
                );

                break;

            case "thinking":
                assistant.thinking.hidden = false;

                assistant.thinkingSummary.textContent = (
                    "Thinking..."
                );

                assistant.thinkingContent.textContent += (
                    event.content
                    || ""
                );

                scrollToBottom();

                break;

            case "content":
                assistant.content.textContent += (
                    event.content
                    || ""
                );

                scrollToBottom();

                break;

            case "response_complete":
                setStatus(
                    assistant,
                    "Formatting...",
                    "formatting"
                );

                break;

            case "rendered_content":
                applyRenderedHtml(
                    assistant.content,
                    event.html
                    || ""
                );

                scrollToBottom();

                break;

            case "done":
                finishStatus(
                    assistant
                );

                refreshConversations();

                break;

            case "error":
                if (
                    !assistant.content.textContent
                ) {
                    assistant.content.textContent = (
                        "Error: "
                        + (
                            event.message
                            || "Generation failed."
                        )
                    );
                }

                finishStatus(
                    assistant
                );

                break;

            default:
                break;
        }
    }


    async function sendMessage(
        text,
        attachments = []
    ) {
        if (
            generating
            || !text.trim()
        ) {
            return;
        }

        generating = true;

        createUserMessage(
            text,
            attachments
        );

        const assistant = (
            createAssistantMessage()
        );

        activeController = (
            new AbortController()
        );

        sendButton.hidden = true;
        stopButton.hidden = false;

        try {
            const response = await fetch(
                "/api/chat/stream",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json",

                        "X-CSRF-Token":
                            csrfToken,
                    },

                    body: JSON.stringify({
                        conversation_id:
                            currentConversationId,

                        message:
                            text,

                        model_mode:
                            modelSelect.value,

                        attachment_ids:
                            attachments.map(
                                (attachment) => (
                                    attachment.id
                                )
                            ),
                    }),

                    signal:
                        activeController
                        .signal,
                }
            );

            if (response.status === 401) {
                window.location.href = (
                    "/login"
                );

                return;
            }

            if (!response.ok) {
                let message = (
                    `Request failed (${response.status})`
                );

                try {
                    const data = (
                        await response.json()
                    );

                    message = (
                        data.error
                        || message
                    );

                } catch (_) {
                    // Keep fallback.
                }

                throw new Error(
                    message
                );
            }

            if (!response.body) {
                throw new Error(
                    "Streaming is unavailable."
                );
            }

            const reader = (
                response.body.getReader()
            );

            const decoder = (
                new TextDecoder()
            );

            let buffer = "";

            while (true) {
                const {
                    value,
                    done,
                } = await reader.read();

                if (done) {
                    break;
                }

                buffer += decoder.decode(
                    value,
                    {
                        stream: true
                    }
                );

                const lines = (
                    buffer.split("\n")
                );

                buffer = (
                    lines.pop()
                    || ""
                );

                for (const line of lines) {
                    if (!line.trim()) {
                        continue;
                    }

                    const event = JSON.parse(
                        line
                    );

                    handleStreamEvent(
                        event,
                        assistant
                    );
                }
            }

            if (buffer.trim()) {
                const event = JSON.parse(
                    buffer
                );

                handleStreamEvent(
                    event,
                    assistant
                );
            }

        } catch (error) {
            if (
                error.name
                === "AbortError"
            ) {
                setStatus(
                    assistant,
                    "Stopped"
                );

                window.setTimeout(
                    () => {
                        finishStatus(
                            assistant
                        );
                    },
                    700
                );

            } else {
                if (
                    !assistant.content
                    .textContent
                ) {
                    assistant.content.textContent = (
                        "Error: "
                        + error.message
                    );
                }

                finishStatus(
                    assistant
                );
            }

        } finally {
            generating = false;
            activeController = null;

            sendButton.hidden = false;
            stopButton.hidden = true;

            input.focus();
        }
    }


    // =====================================================
    // SIDEBAR RESIZE
    // =====================================================

    function startSidebarResize(event) {
        if (isMobile()) {
            return;
        }

        resizeActive = true;

        document.body.classList.add(
            "sidebar-resizing"
        );

        sidebarResizeHandle.setPointerCapture(
            event.pointerId
        );

        event.preventDefault();
    }


    function moveSidebarResize(event) {
        if (
            !resizeActive
            || isMobile()
        ) {
            return;
        }

        setSidebarWidth(
            event.clientX,
            false
        );
    }


    function finishSidebarResize(event) {
        if (!resizeActive) {
            return;
        }

        resizeActive = false;

        document.body.classList.remove(
            "sidebar-resizing"
        );

        const width = parseFloat(
            getComputedStyle(app)
            .getPropertyValue(
                "--sidebar-width"
            )
        );

        setSidebarWidth(
            width,
            true
        );

        try {
            sidebarResizeHandle
            .releasePointerCapture(
                event.pointerId
            );

        } catch (_) {
            // Pointer may already be released.
        }
    }


    // =====================================================
    // EVENTS
    // =====================================================

    form.addEventListener(
        "submit",
        (event) => {
            event.preventDefault();

            const text = (
                input.value.trim()
            );

            if (attachmentUploadActive) {
                showNotice(
                    "Please wait for the attachment upload to finish."
                );
                return;
            }

            if (!text) {
                if (pendingAttachments.length) {
                    showNotice(
                        "Add a message with the attachment for now."
                    );
                }
                return;
            }

            const attachments = [
                ...pendingAttachments
            ];

            pendingAttachments = [];
            renderPendingAttachments();

            input.value = "";
            autoResizeInput();

            sendMessage(
                text,
                attachments
            );
        }
    );


    input.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key === "Enter"
                && !event.shiftKey
            ) {
                event.preventDefault();

                form.requestSubmit();
            }
        }
    );


    input.addEventListener(
        "input",
        autoResizeInput
    );


    stopButton.addEventListener(
        "click",
        () => {
            if (activeController) {
                activeController.abort();
            }
        }
    );


    newChatButton.addEventListener(
        "click",
        startNewChat
    );


    mobileMenuButton.addEventListener(
        "click",
        openMobileSidebar
    );


    sidebarOverlay.addEventListener(
        "click",
        closeMobileSidebar
    );


    desktopSidebarCollapseButton
    .addEventListener(
        "click",
        () => {
            if (!isMobile()) {
                setDesktopSidebarCollapsed(
                    true
                );
            }
        }
    );


    desktopSidebarExpandButton
    .addEventListener(
        "click",
        () => {
            if (!isMobile()) {
                setDesktopSidebarCollapsed(
                    false
                );
            }
        }
    );


    sidebarResizeHandle.addEventListener(
        "pointerdown",
        startSidebarResize
    );

    sidebarResizeHandle.addEventListener(
        "pointermove",
        moveSidebarResize
    );

    sidebarResizeHandle.addEventListener(
        "pointerup",
        finishSidebarResize
    );

    sidebarResizeHandle.addEventListener(
        "pointercancel",
        finishSidebarResize
    );


    document.addEventListener(
        "click",
        (event) => {
            const copyButton = (
                event.target.closest(
                    ".code-copy-button"
                )
            );

            if (copyButton) {
                copyCodeBlock(
                    copyButton
                );

                return;
            }

            if (
                !event.target.closest(
                    ".conversation-menu-wrap"
                )
            ) {
                closeConversationMenus();
            }
        }
    );


    window.addEventListener(
        "resize",
        () => {
            syncViewportHeight();

            if (!isMobile()) {
                closeMobileSidebar();
            }
        }
    );


    if (window.visualViewport) {
        window.visualViewport.addEventListener(
            "resize",
            syncViewportHeight
        );

        window.visualViewport.addEventListener(
            "scroll",
            syncViewportHeight
        );
    }


    attachmentButton.addEventListener(
        "click",
        () => {
            if (
                generating
                || attachmentUploadActive
            ) {
                return;
            }

            attachmentInput.click();
        }
    );


    attachmentInput.addEventListener(
        "change",
        async () => {
            const files = Array.from(
                attachmentInput.files || []
            );

            attachmentInput.value = "";

            await uploadFiles(
                files
            );
        }
    );


    microphoneButton.addEventListener(
        "click",
        () => {
            showNotice(
                "Speech foundation is reserved. STT/TTS will be connected later."
            );
        }
    );


    modelSelect.addEventListener(
        "change",
        async () => {
            try {
                await requestJson(
                    "/api/settings",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json",
                        },

                        body: JSON.stringify({
                            default_model_mode:
                                modelSelect.value,
                        }),
                    }
                );

            } catch (error) {
                showNotice(
                    "Could not save model preference."
                );
            }
        }
    );


    // =====================================================
    // START
    // =====================================================

    syncViewportHeight();

    applySidebarPreferences();

    autoResizeInput();

    refreshConversations();

    input.focus();

    // Reserved for future image-generation service:
    // createImageGenerationPlaceholder("Generating image...");
    void createImageGenerationPlaceholder;
})();
