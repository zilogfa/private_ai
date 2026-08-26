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

    const sidebar = (
        document.getElementById(
            "sidebar"
        )
    );

    const sidebarOverlay = (
        document.getElementById(
            "sidebarOverlay"
        )
    );

    const mobileMenuButton = (
        document.getElementById(
            "mobileMenuButton"
        )
    );

    const newChatButton = (
        document.getElementById(
            "newChatButton"
        )
    );

    const conversationList = (
        document.getElementById(
            "conversationList"
        )
    );

    const messages = (
        document.getElementById(
            "messages"
        )
    );

    const welcomeState = (
        document.getElementById(
            "welcomeState"
        )
    );

    const form = (
        document.getElementById(
            "composerForm"
        )
    );

    const input = (
        document.getElementById(
            "messageInput"
        )
    );

    const modelSelect = (
        document.getElementById(
            "modelSelect"
        )
    );

    const sendButton = (
        document.getElementById(
            "sendButton"
        )
    );

    const stopButton = (
        document.getElementById(
            "stopButton"
        )
    );

    const attachmentButton = (
        document.getElementById(
            "attachmentButton"
        )
    );

    const microphoneButton = (
        document.getElementById(
            "microphoneButton"
        )
    );

    const attachmentNotice = (
        document.getElementById(
            "attachmentNotice"
        )
    );

    let currentConversationId = null;
    let activeController = null;
    let generating = false;

    modelSelect.value = (
        app.dataset.defaultModel
        || "auto"
    );


    // =====================================================
    // UTILITIES
    // =====================================================

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


    function closeSidebar() {
        sidebar.classList.remove(
            "open"
        );

        sidebarOverlay.classList.remove(
            "open"
        );
    }


    function openSidebar() {
        sidebar.classList.add(
            "open"
        );

        sidebarOverlay.classList.add(
            "open"
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
    // MESSAGE RENDERING
    // =====================================================

    function createUserMessage(text) {
        hideWelcome();

        const article = (
            document.createElement(
                "article"
            )
        );

        article.className = (
            "message user-message"
        );

        const inner = (
            document.createElement(
                "div"
            )
        );

        inner.className = (
            "message-inner"
        );

        const content = (
            document.createElement(
                "div"
            )
        );

        content.className = (
            "message-text"
        );

        content.textContent = text;

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

        const article = (
            document.createElement(
                "article"
            )
        );

        article.className = (
            "message assistant-message"
        );

        const inner = (
            document.createElement(
                "div"
            )
        );

        inner.className = (
            "message-inner"
        );

        const meta = (
            document.createElement(
                "div"
            )
        );

        meta.className = (
            "assistant-meta"
        );

        const modelTag = (
            document.createElement(
                "span"
            )
        );

        modelTag.className = (
            "model-tag"
        );

        meta.appendChild(
            modelTag
        );

        const thinking = (
            document.createElement(
                "details"
            )
        );

        thinking.className = (
            "thinking-panel"
        );

        thinking.hidden = true;

        const summary = (
            document.createElement(
                "summary"
            )
        );

        summary.textContent = (
            "Thinking..."
        );

        const thinkingContent = (
            document.createElement(
                "div"
            )
        );

        thinkingContent.className = (
            "thinking-content"
        );

        thinking.append(
            summary,
            thinkingContent
        );

        const content = (
            document.createElement(
                "div"
            )
        );

        content.className = (
            "message-text"
        );

        const status = (
            document.createElement(
                "div"
            )
        );

        status.className = (
            "status-row"
        );

        const statusDot = (
            document.createElement(
                "span"
            )
        );

        statusDot.className = (
            "status-dot"
        );

        const statusLabel = (
            document.createElement(
                "span"
            )
        );

        statusLabel.textContent = (
            "Preparing..."
        );

        status.append(
            statusDot,
            statusLabel
        );

        inner.append(
            meta,
            thinking,
            content,
            status
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
            thinkingContent,
            content,
            status,
            statusLabel,
        };
    }


    function createImageGenerationPlaceholder(
        label = "Generating image..."
    ) {
        const template = (
            document.getElementById(
                "imageGenerationTemplate"
            )
        );

        const fragment = (
            template.content.cloneNode(
                true
            )
        );

        const card = (
            fragment.querySelector(
                ".image-generation-card"
            )
        );

        const labelElement = (
            fragment.querySelector(
                ".image-generation-label"
            )
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


    function setStatus(
        assistant,
        label
    ) {
        assistant.status.hidden = false;

        assistant.statusLabel.textContent = (
            label
        );

        scrollToBottom();
    }


    function finishStatus(
        assistant
    ) {
        assistant.status.hidden = true;
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
                const empty = (
                    document.createElement(
                        "div"
                    )
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
                const button = (
                    document.createElement(
                        "button"
                    )
                );

                button.type = "button";

                button.className = (
                    "conversation-item"
                );

                if (
                    conversation.id
                    === currentConversationId
                ) {
                    button.classList.add(
                        "active"
                    );
                }

                button.textContent = (
                    conversation.title
                    || "New Chat"
                );

                button.addEventListener(
                    "click",
                    () => {
                        loadConversation(
                            conversation.id
                        );
                    }
                );

                conversationList.appendChild(
                    button
                );
            }

        } catch (error) {
            conversationList.innerHTML = "";

            const failed = (
                document.createElement(
                    "div"
                )
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
                            message.content
                        );

                    } else {
                        const assistant = (
                            createAssistantMessage()
                        );

                        assistant.content.textContent = (
                            message.content
                        );

                        finishStatus(
                            assistant
                        );
                    }
                }
            }

            await refreshConversations();

            closeSidebar();

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

        currentConversationId = null;

        messages.innerHTML = "";

        messages.appendChild(
            welcomeState
        );

        showWelcome();

        refreshConversations();

        closeSidebar();

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

            case "status":
                setStatus(
                    assistant,
                    event.label
                    || "Working..."
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
                    "Finishing..."
                );

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


    async function sendMessage(text) {
        if (
            generating
            || !text.trim()
        ) {
            return;
        }

        generating = true;

        createUserMessage(
            text
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
    // EVENTS
    // =====================================================

    form.addEventListener(
        "submit",
        (event) => {
            event.preventDefault();

            const text = (
                input.value.trim()
            );

            if (!text) {
                return;
            }

            input.value = "";
            autoResizeInput();

            sendMessage(
                text
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
        openSidebar
    );


    sidebarOverlay.addEventListener(
        "click",
        closeSidebar
    );


    attachmentButton.addEventListener(
        "click",
        () => {
            showNotice(
                "Attachment UI is ready. Vision/document processing will be connected next."
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

    autoResizeInput();

    refreshConversations();

    input.focus();

    // Reserved for the future image-generation service:
    // createImageGenerationPlaceholder("Generating image...");
    void createImageGenerationPlaceholder;
})();
