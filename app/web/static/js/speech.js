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

    if (
        !originalButton
        || !input
        || !notice
    ) {
        return;
    }

    // chat.js v1.4 owns a placeholder mic click handler. Replace the
    // button node so v1.5 can attach the real speech handler without
    // changing the large shared chat.js file.
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


    function setIdleButton() {
        microphoneButton.textContent = "◉";
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
        microphoneButton.textContent = "■";
        microphoneButton.disabled = false;
        microphoneButton.setAttribute(
            "aria-label",
            "Stop voice recording"
        );
        microphoneButton.title = (
            "Stop voice recording"
        );
    }


    function setTranscribingButton() {
        microphoneButton.textContent = "…";
        microphoneButton.disabled = true;
        microphoneButton.setAttribute(
            "aria-label",
            "Transcribing voice"
        );
        microphoneButton.title = (
            "Transcribing locally"
        );
    }


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
                }
            );

            mediaSource.connect(
                processor
            );

            // ScriptProcessor callbacks only fire while connected to an
            // output. We write no output samples, so this connection stays
            // silent and does not play the microphone back to the user.
            processor.connect(
                audioContext.destination
            );

            recording = true;
            setRecordingButton();

            showSpeechNotice(
                "Listening... tap the square microphone button to stop.",
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


    window.addEventListener(
        "beforeunload",
        () => {
            if (recording) {
                recording = false;
                clearRecordingResources();
            }
        }
    );


    setIdleButton();
})();
