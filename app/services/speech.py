import io
import threading
import wave

import numpy as np

import app.config as config


class SpeechError(Exception):
    pass


class SpeechUnavailableError(SpeechError):
    pass


_STT_LOCK = threading.Lock()


# =========================================================
# WAV DECODING
# =========================================================


def _resample_linear(
    samples,
    source_rate,
    target_rate,
):
    if source_rate == target_rate:
        return samples.astype(
            np.float32,
            copy=False,
        )

    if not len(samples):
        return np.empty(
            0,
            dtype=np.float32,
        )

    target_length = max(
        1,
        int(
            round(
                len(samples)
                * target_rate
                / source_rate
            )
        ),
    )

    source_positions = np.linspace(
        0.0,
        1.0,
        num=len(samples),
        endpoint=False,
        dtype=np.float64,
    )

    target_positions = np.linspace(
        0.0,
        1.0,
        num=target_length,
        endpoint=False,
        dtype=np.float64,
    )

    resampled = np.interp(
        target_positions,
        source_positions,
        samples,
    )

    return resampled.astype(
        np.float32,
        copy=False,
    )


def decode_pcm16_wav(
    wav_bytes,
):
    if not wav_bytes:
        raise SpeechError(
            "The audio recording is empty."
        )

    if (
        len(wav_bytes)
        > config.STT_MAX_UPLOAD_BYTES
    ):
        raise SpeechError(
            "The voice recording is too large."
        )

    try:
        with wave.open(
            io.BytesIO(wav_bytes),
            "rb",
        ) as wav_file:
            channels = (
                wav_file.getnchannels()
            )
            sample_width = (
                wav_file.getsampwidth()
            )
            sample_rate = (
                wav_file.getframerate()
            )
            frame_count = (
                wav_file.getnframes()
            )
            compression = (
                wav_file.getcomptype()
            )

            if compression != "NONE":
                raise SpeechError(
                    "Compressed WAV audio is not supported."
                )

            if channels not in {1, 2}:
                raise SpeechError(
                    "Voice input must be mono or stereo."
                )

            if sample_width != 2:
                raise SpeechError(
                    "Voice input must use 16-bit PCM WAV audio."
                )

            if sample_rate <= 0:
                raise SpeechError(
                    "The audio sample rate is invalid."
                )

            duration_seconds = (
                frame_count
                / float(sample_rate)
            )

            if (
                duration_seconds
                > config.STT_MAX_SECONDS
                + 1.0
            ):
                raise SpeechError(
                    (
                        "Voice recordings are limited to "
                        f"{config.STT_MAX_SECONDS} seconds."
                    )
                )

            frames = wav_file.readframes(
                frame_count
            )

    except SpeechError:
        raise

    except (
        wave.Error,
        EOFError,
        ValueError,
    ) as error:
        raise SpeechError(
            "The uploaded audio is not a valid PCM WAV recording."
        ) from error

    samples = np.frombuffer(
        frames,
        dtype="<i2",
    ).astype(
        np.float32
    )

    samples /= 32768.0

    if channels == 2:
        if len(samples) % 2:
            samples = samples[:-1]

        samples = (
            samples.reshape(-1, 2)
            .mean(axis=1)
        )

    samples = np.clip(
        samples,
        -1.0,
        1.0,
    ).astype(
        np.float32,
        copy=False,
    )

    samples = _resample_linear(
        samples,
        source_rate=sample_rate,
        target_rate=config.STT_SAMPLE_RATE,
    )

    if len(samples) < int(
        config.STT_SAMPLE_RATE
        * 0.15
    ):
        raise SpeechError(
            "The voice recording is too short to transcribe."
        )

    return (
        np.ascontiguousarray(
            samples,
            dtype=np.float32,
        ),
        duration_seconds,
    )


# =========================================================
# PROVIDER
# =========================================================


def _transcribe_mlx_whisper(
    samples,
    language=None,
):
    try:
        import mlx_whisper

    except ImportError as error:
        raise SpeechUnavailableError(
            (
                "MLX Whisper is not installed. Run "
                "'python -m pip install -r requirements.txt' "
                "inside the project virtual environment."
            )
        ) from error

    options = {
        "path_or_hf_repo":
            config.STT_MODEL,
        "verbose": False,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "word_timestamps": False,
    }

    selected_language = (
        str(
            language
            or config.STT_LANGUAGE
            or ""
        )
        .strip()
        .lower()
        or None
    )

    if selected_language:
        options[
            "language"
        ] = selected_language

    try:
        with _STT_LOCK:
            result = (
                mlx_whisper.transcribe(
                    samples,
                    **options,
                )
            )

    except Exception as error:
        raise SpeechError(
            (
                "Local speech transcription failed. "
                "On first use, make sure the Mac is online so the "
                "Whisper model can download, then try again. "
                f"Details: {error}"
            )
        ) from error

    text = str(
        result.get(
            "text",
            "",
        )
        or ""
    ).strip()

    if not text:
        raise SpeechError(
            "No speech was detected in the recording."
        )

    return {
        "text": text,
        "language": (
            result.get("language")
            or selected_language
        ),
    }


def transcribe_wav_bytes(
    wav_bytes,
    language=None,
):
    samples, duration_seconds = (
        decode_pcm16_wav(
            wav_bytes
        )
    )

    provider = str(
        config.STT_PROVIDER
        or ""
    ).strip().lower()

    if provider == "mlx_whisper":
        result = _transcribe_mlx_whisper(
            samples,
            language=language,
        )

    else:
        raise SpeechUnavailableError(
            (
                "Unsupported speech-to-text provider: "
                f"{config.STT_PROVIDER}"
            )
        )

    return {
        "text": result["text"],
        "language": result.get(
            "language"
        ),
        "duration_seconds": round(
            duration_seconds,
            2,
        ),
        "provider": provider,
        "model": config.STT_MODEL,
    }
