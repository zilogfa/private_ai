from flask import (
    Blueprint,
    Response,
    jsonify,
    request,
)

from app.auth import (
    get_current_user_id,
    permission_required,
)

from app.config import (
    STT_MAX_UPLOAD_BYTES,
    TTS_DEFAULT_VOICE,
)

from app.database import (
    get_user_settings,
)

from app.services.speech import (
    SpeechError,
    synthesize_text_to_wav,
    transcribe_wav_bytes,
)


speech_api_bp = Blueprint(
    "speech_api",
    __name__,
    url_prefix="/api/speech",
)


@speech_api_bp.post("/transcribe")
@permission_required("speech.use")
def transcribe_audio():
    audio_file = request.files.get(
        "audio"
    )

    if not audio_file:
        return (
            jsonify({
                "error": "audio_required"
            }),
            400,
        )

    audio_bytes = audio_file.read(
        STT_MAX_UPLOAD_BYTES
        + 1
    )

    if not audio_bytes:
        return (
            jsonify({
                "error": "empty_audio"
            }),
            400,
        )

    if (
        len(audio_bytes)
        > STT_MAX_UPLOAD_BYTES
    ):
        return (
            jsonify({
                "error": "audio_too_large"
            }),
            413,
        )

    language = (
        request.form.get(
            "language"
        )
        or None
    )

    try:
        result = transcribe_wav_bytes(
            audio_bytes,
            language=language,
        )

    except SpeechError as error:
        return (
            jsonify({
                "error": str(error)
            }),
            400,
        )

    return jsonify(result)


@speech_api_bp.post("/synthesize")
@permission_required("speech.use")
def synthesize_speech():
    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    text = str(
        payload.get(
            "text",
            "",
        )
        or ""
    ).strip()

    if not text:
        return (
            jsonify({
                "error": "text_required"
            }),
            400,
        )

    settings = (
        get_user_settings(
            get_current_user_id()
        )
        or {}
    )

    voice = (
        settings.get(
            "voice_id"
        )
        or TTS_DEFAULT_VOICE
    )

    try:
        result = synthesize_text_to_wav(
            text,
            voice=voice,
        )

    except SpeechError as error:
        return (
            jsonify({
                "error": str(error)
            }),
            400,
        )

    response = Response(
        result["wav_bytes"],
        mimetype="audio/wav",
    )

    response.headers[
        "Cache-Control"
    ] = "no-store"

    response.headers[
        "X-Speech-Voice"
    ] = result["voice"]

    response.headers[
        "X-Speech-Duration"
    ] = str(
        result["duration_seconds"]
    )

    response.headers[
        "X-Speech-Truncated"
    ] = (
        "1"
        if result["truncated"]
        else "0"
    )

    return response
