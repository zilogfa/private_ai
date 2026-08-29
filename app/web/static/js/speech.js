(() => {
    "use strict";

    const originalButton = document.getElementById(
        "microphoneButton"
    );

    const input = document.getElementById(
        "messageInput"
    );

    const notice = document.getElementById(
        "attachmentNotice"
    );

    const messages = document.getElementById(
        "messages"
    );

    const composerForm = document.getElementById(
        "composerForm"
    );

    const conversationList = document.getElementById(
        "conversationList"
    );

    if (
        !originalButton
        || !input
        || !notice
        || !messages
    ) {
        return;
    }

    // chat.js still owns the older placeholder mic click handler.
    // Replacing the node removes that listener without coupling speech
    // implementation details into the main chat UI file.
    const microphoneButton = (
        originalButton.cloneNode(true)
    );

    originalButton.replaceWith(
        microphoneButton
    );

    const csrfToken = (
        document
        .querySelector(
            'meta[name="csrf-token"]'
        )
        ?.getAttribute("content")
        || ""
    );

    const TARGET_SAMPLE_RATE = 16000;
    const MAX_RECORD_SECONDS = 90;

    let recording = false;
    let transcribing = false;
    let stream = null;
    let audioContext = null;
    let mediaSource = null;
    let processor = null;
    let sampleRate = 0;
    let chunks = [];
    let recordingTimeout = null;
    let firstTranscription = true;

    let ttsEnabled = false;
    let firstSynthesis = true;
    let currentSpeech = null;
    let ttsAudioContext = null;
    let suppressAutoSpeakUntil = (
        Date.now() + 1500
    );


    // =====================================================
    // UI HELPERS
    // =====================================================

    function ensureSpeechStylesheet() {
        if (
            document.querySelector(
                'link[data-private-ai-speech-style]'
            )
        ) {
            return;
        }

        const link = document.createElement(
            "link"
        );

        link.rel = "stylesheet";
        link.href = "/static/css/speech.css";
        link.dataset.privateAiSpeechStyle = "1";

        document.head.appendChild(
            link
        );
    }


    function showSpeechNotice(
        message,
        timeoutMs = 3600
    ) {
        notice.textContent = message;
        notice.hidden = false;

        if (timeoutMs > 0) {
            window.setTimeout(
                () => {
                    if (
                        notice.textContent
                        === message
                    ) {
                        notice.hidden = true;
                    }
                },
                timeoutMs
            );
        }
    }


    function resetRecordingVisuals() {
        microphoneButton.classList.remove(
            "speech-recording",
            "speech-transcribing"
        );

        microphoneButton.style.removeProperty(
            "transform"
        );

        microphoneButton.style.removeProperty(
            "box-shadow"
        );
    }


    function setIdleButton() {
        resetRecordingVisuals();

        microphoneButton.innerHTML = "◉";
        microphoneButton.disabled = false;
        microphoneButton.setAttribute(
            "aria-label",
            "Start voice input"
        );
        microphoneButton.title = (
            "Voice input (local transcription)"
        );
    }


    function setRecordingButton() {
        resetRecordingVisuals();

        microphoneButton.classList.add(
            "speech-recording"
        );

        microphoneButton.innerHTML = (
            '<span class="speech-stop-glyph" aria-hidden="true">■</span>'
            + '<span class="speech-level-bars" aria-hidden="true">'
            + '<span></span><span></span><span></span>'
            + '</span>'
        );

        microphoneButton.disabled = false;
        microphoneButton.setAttribute(
            "aria-label",
            "Stop voice recording"
        );
        microphoneButton.title = (
            "Recording locally — click to stop"
        );
    }


    function setRecordingLevel(level) {
        if (!recording) {
            return;
        }

        const safeLevel = Math.max(
            0,
            Math.min(
                1,
                Number(level) || 0
            )
        );

        const scale = (
            1 + safeLevel * 0.09
        );

        const ring = (
            2 + safeLevel * 6
        );

        microphoneButton.style.transform = (
            `scale(${scale.toFixed(3)})`
        );

        microphoneButton.style.boxShadow = (
            `0 0 0 ${ring.toFixed(1)}px rgba(220, 55, 65, ${(
                0.08 + safeLevel * 0.16
            ).toFixed(3)})`
        );

        const bars = (
            microphoneButton
            .querySelectorAll(
                ".speech-level-bars span"
            )
        );

        const multipliers = [
            0.75,
            1.15,
            0.9,
        ];

        bars.forEach(
            (bar, index) => {
                const height = (
                    2.5
                    + safeLevel
                    * 7
                    * multipliers[index]
                );

                bar.style.height = (
                    `${Math.min(10, height).toFixed(1)}px`
                );
            }
        );
    }


    function setTranscribingButton() {
        resetRecordingVisuals();

        microphoneButton.classList.add(
            "speech-transcribing"
        );

        microphoneButton.innerHTML = (
            '<span class="speech-transcribing-dots" aria-hidden="true">•••</span>'
        );

        microphoneButton.disabled = true;
        microphoneButton.setAttribute(
            "aria-label",
            "Transcribing voice"
        );
        microphoneButton.title = (
            "Transcribing locally"
        );
    }


    // =====================================================
    // STT AUDIO HELPERS
    // =====================================================

    function audioContextClass() {
        return (
            window.AudioContext
            || window.webkitAudioContext
            || null
        );
    }


    function concatenateChunks(
        sourceChunks
    ) {
        const totalLength = (
            sourceChunks.reduce(
                (
                    total,
                    chunk
                ) => total + chunk.length,
                0
            )
        );

        const result = new Float32Array(
            totalLength
        );

        let offset = 0;

        for (const chunk of sourceChunks) {
            result.set(
                chunk,
                offset
            );

            offset += chunk.length;
        }

        return result;
    }


    function mixToMono(audioBuffer) {
        const channelCount = (
            audioBuffer.numberOfChannels
        );

        const length = audioBuffer.length;

        if (channelCount === 1) {
            return new Float32Array(
                audioBuffer.getChannelData(0)
            );
        }

        const mono = new Float32Array(
            length
        );

        for (
            let channel = 0;
            channel < channelCount;
            channel += 1
        ) {
            const data = (
                audioBuffer.getChannelData(
                    channel
                )
            );

            for (
                let index = 0;
                index < length;
                index += 1
            ) {
                mono[index] += (
                    data[index]
                    / channelCount
                );
            }
        }

        return mono;
    }


    function resampleLinear(
        samples,
        sourceRate,
        targetRate
    ) {
        if (
            !samples.length
            || sourceRate === targetRate
        ) {
            return new Float32Array(
                samples
            );
        }

        const targetLength = Math.max(
            1,
            Math.round(
                samples.length
                * targetRate
                / sourceRate
            )
        );

        const output = new Float32Array(
            targetLength
        );

        const ratio = (
            sourceRate
            / targetRate
        );

        for (
            let index = 0;
            index < targetLength;
            index += 1
        ) {
            const sourcePosition = (
                index * ratio
            );

            const leftIndex = Math.min(
                samples.length - 1,
                Math.floor(
                    sourcePosition
                )
            );

            const rightIndex = Math.min(
                samples.length - 1,
                leftIndex + 1
            );

            const fraction = (
                sourcePosition
                - leftIndex
            );

            output[index] = (
                samples[leftIndex]
                + (
                    samples[rightIndex]
                    - samples[leftIndex]
                )
                * fraction
            );
        }

        return output;
    }


    function writeAscii(
        view,
        offset,
        value
    ) {
        for (
            let index = 0;
            index < value.length;
            index += 1
        ) {
            view.setUint8(
                offset + index,
                value.charCodeAt(index)
            );
        }
    }


    function encodePcm16Wav(
        samples,
        rate
    ) {
        const buffer = new ArrayBuffer(
            44 + samples.length * 2
        );

        const view = new DataView(
            buffer
        );

        writeAscii(
            view,
            0,
            "RIFF"
        );

        view.setUint32(
            4,
            36 + samples.length * 2,
            true
        );

        writeAscii(
            view,
            8,
            "WAVE"
        );

        writeAscii(
            view,
            12,
            "fmt "
        );

        view.setUint32(
            16,
            16,
            true
        );

        view.setUint16(
            20,
            1,
            true
        );

        view.setUint16(
            22,
            1,
            true
        );

        view.setUint32(
            24,
            rate,
            true
        );

        view.setUint32(
            28,
            rate * 2,
            true
        );

        view.setUint16(
            32,
            2,
            true
        );

        view.setUint16(
            34,
            16,
            true
        );

        writeAscii(
            view,
            36,
            "data"
        );

        view.setUint32(
            40,
            samples.length * 2,
            true
        );

        let offset = 44;

        for (
            let index = 0;
            index < samples.length;
            index += 1
        ) {
            const clamped = Math.max(
                -1,
                Math.min(
                    1,
                    samples[index]
                )
            );

            const value = (
                clamped < 0
                ? clamped * 32768
                : clamped * 32767
            );

            view.setInt16(
                offset,
                value,
                true
            );

            offset += 2;
        }

        return new Blob(
            [buffer],
            {
                type: "audio/wav",
            }
        );
    }


    function appendTranscript(text) {
        const transcript = String(
            text || ""
        ).trim();

        if (!transcript) {
            return;
        }

        const existing = input.value;

        if (!existing.trim()) {
            input.value = transcript;

        } else {
            const separator = (
                /\s$/.test(existing)
                ? ""
                : " "
            );

            input.value = (
                existing
                + separator
                + transcript
            );
        }

        input.dispatchEvent(
            new Event(
                "input",
                {
                    bubbles: true,
                }
            )
        );

        input.focus();
    }


    async function transcribeWavBlob(
        wavBlob
    ) {
        transcribing = true;
        setTranscribingButton();

        if (firstTranscription) {
            showSpeechNotice(
                (
                    "Transcribing locally... First use may take longer "
                    + "while the Whisper model downloads."
                ),
                0
            );

        } else {
            showSpeechNotice(
                "Transcribing locally...",
                0
            );
        }

        try {
            const formData = new FormData();

            formData.append(
                "audio",
                wavBlob,
                "voice.wav"
            );

            const response = await fetch(
                "/api/speech/transcribe",
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
                window.location.href = (
                    "/login"
                );
                return;
            }

            let data = {};

            try {
                data = await response.json();

            } catch (_) {
                // Keep fallback below.
            }

            if (!response.ok) {
                throw new Error(
                    data.error
                    || `Transcription failed (${response.status})`
                );
            }

            appendTranscript(
                data.text
            );

            firstTranscription = false;

            const languageDetail = (
                data.language
                ? ` (${data.language})`
                : ""
            );

            showSpeechNotice(
                (
                    "Voice transcribed locally"
                    + languageDetail
                    + ". Review or edit, then send."
                )
            );

        } catch (error) {
            showSpeechNotice(
                (
                    "Voice transcription failed: "
                    + error.message
                ),
                7000
            );

        } finally {
            transcribing = false;
            setIdleButton();
        }
    }


    function clearRecordingResources() {
        if (recordingTimeout) {
            window.clearTimeout(
                recordingTimeout
            );

            recordingTimeout = null;
        }

        try {
            if (processor) {
                processor.disconnect();
            }
        } catch (_) {
            // Already disconnected.
        }

        try {
            if (mediaSource) {
                mediaSource.disconnect();
            }
        } catch (_) {
            // Already disconnected.
        }

        if (stream) {
            for (const track of stream.getTracks()) {
                track.stop();
            }
        }

        processor = null;
        mediaSource = null;
        stream = null;
    }


    async function stopRecording() {
        if (!recording) {
            return;
        }

        recording = false;
        setRecordingLevel(0);

        const capturedChunks = chunks;
        chunks = [];

        const sourceRate = sampleRate;

        clearRecordingResources();

        if (audioContext) {
            try {
                await audioContext.close();
            } catch (_) {
                // Ignore close failures.
            }

            audioContext = null;
        }

        setTranscribingButton();

        const samples = concatenateChunks(
            capturedChunks
        );

        if (!samples.length) {
            transcribing = false;
            setIdleButton();
            showSpeechNotice(
                "No microphone audio was captured."
            );
            return;
        }

        const resampled = resampleLinear(
            samples,
            sourceRate,
            TARGET_SAMPLE_RATE
        );

        const wavBlob = encodePcm16Wav(
            resampled,
            TARGET_SAMPLE_RATE
        );

        await transcribeWavBlob(
            wavBlob
        );
    }


    async function startRecording() {
        if (
            recording
            || transcribing
        ) {
            return;
        }

        const AudioContextImpl = (
            audioContextClass()
        );

        if (!AudioContextImpl) {
            await useAudioFileFallback();
            return;
        }

        if (
            !navigator.mediaDevices
            || !navigator.mediaDevices.getUserMedia
            || !window.isSecureContext
        ) {
            showSpeechNotice(
                (
                    "Live microphone access needs a secure browser context. "
                    + "Opening the audio recording/file fallback instead."
                )
            );

            await useAudioFileFallback();
            return;
        }

        try {
            stream = await navigator.mediaDevices
            .getUserMedia({
                audio: {
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });

            audioContext = (
                new AudioContextImpl()
            );

            if (
                audioContext.state
                === "suspended"
            ) {
                await audioContext.resume();
            }

            sampleRate = (
                audioContext.sampleRate
            );

            chunks = [];

            mediaSource = (
                audioContext
                .createMediaStreamSource(
                    stream
                )
            );

            processor = (
                audioContext
                .createScriptProcessor(
                    4096,
                    1,
                    1
                )
            );

            processor.onaudioprocess = (
                (event) => {
                    if (!recording) {
                        return;
                    }

                    const channel = (
                        event.inputBuffer
                        .getChannelData(0)
                    );

                    chunks.push(
                        new Float32Array(
                            channel
                        )
                    );

                    let sumSquares = 0;
                    let sampleCount = 0;

                    for (
                        let index = 0;
                        index < channel.length;
                        index += 8
                    ) {
                        const value = channel[index];
                        sumSquares += (
                            value * value
                        );
                        sampleCount += 1;
                    }

                    const rms = Math.sqrt(
                        sumSquares
                        / Math.max(
                            1,
                            sampleCount
                        )
                    );

                    setRecordingLevel(
                        Math.min(
                            1,
                            rms * 7.5
                        )
                    );
                }
            );

            mediaSource.connect(
                processor
            );

            // ScriptProcessor callbacks only fire while connected to an
            // output. No output samples are written, so the microphone is
            // not played back through the speakers.
            processor.connect(
                audioContext.destination
            );

            recording = true;
            setRecordingButton();
            setRecordingLevel(0.05);

            showSpeechNotice(
                "Listening locally... the mic button moves with your voice. Click it to stop.",
                0
            );

            recordingTimeout = (
                window.setTimeout(
                    () => {
                        stopRecording();
                    },
                    MAX_RECORD_SECONDS * 1000
                )
            );

        } catch (error) {
            clearRecordingResources();

            if (audioContext) {
                try {
                    await audioContext.close();
                } catch (_) {
                    // Ignore close failures.
                }

                audioContext = null;
            }

            setIdleButton();

            if (
                error.name
                === "NotAllowedError"
            ) {
                showSpeechNotice(
                    (
                        "Microphone permission was not granted. "
                        + "You can allow it in the browser site settings."
                    ),
                    6500
                );

            } else {
                showSpeechNotice(
                    (
                        "Could not start microphone: "
                        + error.message
                    ),
                    6500
                );
            }
        }
    }


    function createFallbackAudioInput() {
        const fallbackInput = (
            document.createElement(
                "input"
            )
        );

        fallbackInput.type = "file";
        fallbackInput.accept = "audio/*";
        fallbackInput.setAttribute(
            "capture",
            "user"
        );
        fallbackInput.hidden = true;

        document.body.appendChild(
            fallbackInput
        );

        return fallbackInput;
    }


    async function audioFileToWav(file) {
        const AudioContextImpl = (
            audioContextClass()
        );

        if (!AudioContextImpl) {
            throw new Error(
                "This browser cannot decode audio files."
            );
        }

        const context = (
            new AudioContextImpl()
        );

        try {
            const fileBuffer = (
                await file.arrayBuffer()
            );

            const decoded = (
                await context.decodeAudioData(
                    fileBuffer.slice(0)
                )
            );

            const mono = mixToMono(
                decoded
            );

            const resampled = resampleLinear(
                mono,
                decoded.sampleRate,
                TARGET_SAMPLE_RATE
            );

            return encodePcm16Wav(
                resampled,
                TARGET_SAMPLE_RATE
            );

        } finally {
            try {
                await context.close();
            } catch (_) {
                // Ignore close failures.
            }
        }
    }


    async function useAudioFileFallback() {
        if (transcribing) {
            return;
        }

        const fallbackInput = (
            createFallbackAudioInput()
        );

        fallbackInput.addEventListener(
            "change",
            async () => {
                const file = (
                    fallbackInput.files?.[0]
                    || null
                );

                fallbackInput.remove();

                if (!file) {
                    setIdleButton();
                    return;
                }

                setTranscribingButton();

                try {
                    showSpeechNotice(
                        "Preparing audio locally...",
                        0
                    );

                    const wavBlob = (
                        await audioFileToWav(
                            file
                        )
                    );

                    await transcribeWavBlob(
                        wavBlob
                    );

                } catch (error) {
                    transcribing = false;
                    setIdleButton();

                    showSpeechNotice(
                        (
                            "Could not prepare audio: "
                            + error.message
                        ),
                        6500
                    );
                }
            },
            {
                once: true,
            }
        );

        fallbackInput.click();
    }


    // =====================================================
    // TTS
    // =====================================================

    function ensureTtsAudioContext() {
        const AudioContextImpl = (
            audioContextClass()
        );

        if (!AudioContextImpl) {
            return null;
        }

        if (
            !ttsAudioContext
            || ttsAudioContext.state === "closed"
        ) {
            ttsAudioContext = (
                new AudioContextImpl()
            );
        }

        return ttsAudioContext;
    }


    async function unlockTtsPlayback() {
        const context = (
            ensureTtsAudioContext()
        );

        if (!context) {
            return false;
        }

        try {
            if (context.state === "suspended") {
                await context.resume();
            }

            return (
                context.state === "running"
            );

        } catch (_) {
            return false;
        }
    }


    function resetSpeechButton(button) {
        if (!button) {
            return;
        }

        button.disabled = false;
        button.textContent = "🔊";
        button.classList.remove(
            "loading",
            "playing"
        );
        button.setAttribute(
            "aria-label",
            "Read response aloud"
        );
        button.title = (
            "Read aloud locally"
        );
    }


    function stopCurrentSpeech() {
        if (!currentSpeech) {
            return;
        }

        const speech = currentSpeech;
        currentSpeech = null;

        try {
            if (speech.source) {
                speech.source.onended = null;
                speech.source.stop(0);
                speech.source.disconnect();
            }
        } catch (_) {
            // Already stopped or disconnected.
        }

        resetSpeechButton(
            speech.button
        );
    }


    function extractSpeakableText(article) {
        const content = article.querySelector(
            ".assistant-content"
        );

        if (!content) {
            return "";
        }

        const clone = content.cloneNode(
            true
        );

        clone.querySelectorAll(
            "pre, .code-toolbar, .code-copy-button"
        ).forEach(
            (element) => element.remove()
        );

        const headings = Array.from(
            clone.querySelectorAll(
                "h1, h2, h3, h4, h5, h6"
            )
        );

        const sourcesHeading = headings.find(
            (heading) => (
                heading.textContent
                ?.trim()
                .toLowerCase()
                === "sources"
            )
        );

        if (sourcesHeading) {
            let node = sourcesHeading;

            while (node) {
                const next = node.nextSibling;
                node.remove();
                node = next;
            }
        }

        return String(
            clone.textContent
            || ""
        )
        .replace(/\s+/g, " ")
        .trim();
    }


    async function speechErrorFromResponse(
        response
    ) {
        try {
            const data = await response.json();

            return (
                data.error
                || `Speech generation failed (${response.status})`
            );

        } catch (_) {
            return (
                `Speech generation failed (${response.status})`
            );
        }
    }


    async function speakArticle(
        article,
        button,
        auto = false
    ) {
        if (
            currentSpeech
            && currentSpeech.article === article
        ) {
            stopCurrentSpeech();
            return;
        }

        const text = extractSpeakableText(
            article
        );

        if (!text) {
            if (!auto) {
                showSpeechNotice(
                    "There is no response text to read aloud."
                );
            }
            return;
        }

        stopCurrentSpeech();

        // Safari/WebKit may lose the user-activation permission while local
        // synthesis is running. Unlock Web Audio immediately from the click.
        if (!auto) {
            await unlockTtsPlayback();
        }

        button.disabled = true;
        button.textContent = "…";
        button.classList.add(
            "loading"
        );
        button.title = (
            "Generating speech locally"
        );

        if (!auto) {
            showSpeechNotice(
                firstSynthesis
                    ? (
                        "Generating speech locally... First use may take longer "
                        + "while the Kokoro voice model downloads."
                    )
                    : "Generating speech locally...",
                0
            );
        }

        try {
            const response = await fetch(
                "/api/speech/synthesize",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json",
                        "X-CSRF-Token":
                            csrfToken,
                    },
                    body: JSON.stringify({
                        text,
                    }),
                }
            );

            if (response.status === 401) {
                window.location.href = "/login";
                return;
            }

            if (!response.ok) {
                throw new Error(
                    await speechErrorFromResponse(
                        response
                    )
                );
            }

            const truncated = (
                response.headers.get(
                    "X-Speech-Truncated"
                )
                === "1"
            );

            const audioBytes = (
                await response.arrayBuffer()
            );

            const context = (
                ensureTtsAudioContext()
            );

            if (!context) {
                throw new Error(
                    "This browser does not support local audio playback."
                );
            }

            if (context.state !== "running") {
                const unlocked = (
                    await unlockTtsPlayback()
                );

                if (!unlocked) {
                    resetSpeechButton(
                        button
                    );

                    if (auto) {
                        showSpeechNotice(
                            "Auto speech is ready, but the browser blocked autoplay. Click the speaker button to play it.",
                            5500
                        );
                        return;
                    }

                    throw new Error(
                        "Browser audio is blocked. Click the speaker button again to allow playback."
                    );
                }
            }

            let audioBuffer = null;

            try {
                audioBuffer = (
                    await context.decodeAudioData(
                        audioBytes.slice(0)
                    )
                );

            } catch (_) {
                throw new Error(
                    "The browser could not decode the generated speech audio."
                );
            }

            const source = (
                context.createBufferSource()
            );

            source.buffer = audioBuffer;
            source.connect(
                context.destination
            );

            currentSpeech = {
                article,
                button,
                source,
                context,
            };

            button.disabled = false;
            button.textContent = "■";
            button.classList.remove(
                "loading"
            );
            button.classList.add(
                "playing"
            );
            button.setAttribute(
                "aria-label",
                "Stop reading response"
            );
            button.title = (
                "Stop reading aloud"
            );

            source.onended = () => {
                if (
                    currentSpeech
                    && currentSpeech.source === source
                ) {
                    stopCurrentSpeech();
                }
            };

            source.start(0);

            firstSynthesis = false;

            if (!auto) {
                showSpeechNotice(
                    truncated
                        ? "Reading locally. Long response was shortened for speech."
                        : "Reading response aloud locally."
                );
            }

        } catch (error) {
            resetSpeechButton(
                button
            );

            if (!auto) {
                showSpeechNotice(
                    (
                        "Text-to-speech failed: "
                        + error.message
                    ),
                    7000
                );
            }
        }
    }


    function maybeAutoSpeak(article) {
        if (
            !ttsEnabled
            || article.dataset.ttsAutoAttempted
            || Date.now() < suppressAutoSpeakUntil
        ) {
            return;
        }

        const activity = article.querySelector(
            ".activity-row"
        );

        const content = article.querySelector(
            ".assistant-content"
        );

        if (
            !activity
            || !activity.hidden
            || !content
            || !content.textContent.trim()
        ) {
            return;
        }

        article.dataset.ttsAutoAttempted = "1";

        const button = article.querySelector(
            ".assistant-speech-button"
        );

        if (button) {
            window.setTimeout(
                () => {
                    speakArticle(
                        article,
                        button,
                        true
                    );
                },
                120
            );
        }
    }


    function enhanceAssistantMessage(article) {
        if (
            !article
            || article.dataset.speechEnhanced
        ) {
            return;
        }

        const meta = article.querySelector(
            ".assistant-meta"
        );

        const content = article.querySelector(
            ".assistant-content"
        );

        const activity = article.querySelector(
            ".activity-row"
        );

        if (
            !meta
            || !content
            || !activity
        ) {
            return;
        }

        article.dataset.speechEnhanced = "1";

        const button = document.createElement(
            "button"
        );

        button.type = "button";
        button.className = (
            "assistant-speech-button"
        );

        resetSpeechButton(
            button
        );

        button.addEventListener(
            "click",
            () => {
                speakArticle(
                    article,
                    button,
                    false
                );
            }
        );

        meta.appendChild(
            button
        );

        const completionObserver = (
            new MutationObserver(
                () => {
                    maybeAutoSpeak(
                        article
                    );
                }
            )
        );

        completionObserver.observe(
            activity,
            {
                attributes: true,
                attributeFilter: [
                    "hidden",
                    "style",
                    "class",
                ],
            }
        );

        maybeAutoSpeak(
            article
        );
    }


    function enhanceExistingAssistantMessages() {
        messages.querySelectorAll(
            ".assistant-message"
        ).forEach(
            enhanceAssistantMessage
        );
    }


    async function loadSpeechPreferences() {
        try {
            const response = await fetch(
                "/api/settings",
                {
                    headers: {
                        "Accept":
                            "application/json",
                    },
                }
            );

            if (!response.ok) {
                return;
            }

            const data = await response.json();

            ttsEnabled = Boolean(
                data.tts_enabled
            );

        } catch (_) {
            // Manual speaker controls remain available even if settings
            // could not be loaded.
        }
    }


    // =====================================================
    // EVENTS / OBSERVERS
    // =====================================================

    microphoneButton.addEventListener(
        "click",
        async () => {
            if (transcribing) {
                return;
            }

            if (recording) {
                await stopRecording();
                return;
            }

            await startRecording();
        }
    );


    if (conversationList) {
        conversationList.addEventListener(
            "click",
            () => {
                // Loading history creates assistant DOM nodes too. Avoid
                // treating those as brand-new replies for auto TTS.
                suppressAutoSpeakUntil = (
                    Date.now() + 5000
                );

                stopCurrentSpeech();
            },
            true
        );
    }


    if (composerForm) {
        composerForm.addEventListener(
            "submit",
            () => {
                // Submitting is a user gesture, so unlock Web Audio here.
                // This lets optional automatic TTS play after the reply.
                unlockTtsPlayback();
                stopCurrentSpeech();
            },
            true
        );
    }


    const messageObserver = new MutationObserver(
        (mutations) => {
            for (const mutation of mutations) {
                for (const node of mutation.addedNodes) {
                    if (!(node instanceof Element)) {
                        continue;
                    }

                    if (
                        node.matches(
                            ".assistant-message"
                        )
                    ) {
                        enhanceAssistantMessage(
                            node
                        );
                    }

                    node.querySelectorAll?.(
                        ".assistant-message"
                    ).forEach(
                        enhanceAssistantMessage
                    );
                }
            }
        }
    );

    messageObserver.observe(
        messages,
        {
            childList: true,
            subtree: true,
        }
    );


    window.addEventListener(
        "beforeunload",
        () => {
            stopCurrentSpeech();

            if (
                ttsAudioContext
                && ttsAudioContext.state !== "closed"
            ) {
                try {
                    ttsAudioContext.close();
                } catch (_) {
                    // Ignore shutdown errors.
                }
            }

            if (recording) {
                recording = false;
                clearRecordingResources();
            }
        }
    );


    // =====================================================
    // START
    // =====================================================

    ensureSpeechStylesheet();
    setIdleButton();
    enhanceExistingAssistantMessages();
    loadSpeechPreferences();
})();
