from flask import (
    Blueprint,
    jsonify,
    request,
)

from app.auth import (
    permission_required,
)

from app.config import (
    STT_MAX_UPLOAD_BYTES,
)

from app.services.speech import (
    SpeechError,
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
