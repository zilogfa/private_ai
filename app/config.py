import os

from pathlib import Path


# =========================================================
# PROJECT PATHS
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
GENERATED_DIR = PROJECT_ROOT / "generated"

DB_FILE = DATA_DIR / "private_ai.db"

FLASK_SECRET_KEY_FILE = (
    DATA_DIR / ".flask_secret_key"
)


# =========================================================
# TEXT MODELS
# =========================================================

FAST_MODEL = "qwen3:4b"

DEFAULT_MODEL = "qwen3:8b"

DEEP_MODEL = "deepseek-r1:14b"

ROUTER_MODEL = FAST_MODEL

MEMORY_MODEL = FAST_MODEL

SESSION_SUMMARY_MODEL = DEFAULT_MODEL

EMBEDDING_MODEL = "nomic-embed-text"


# =========================================================
# MODEL ROUTING
# =========================================================

DEFAULT_MODEL_MODE = "auto"

VALID_MODEL_MODES = (
    "auto",
    "fast",
    "default",
    "deep",
)

SHOW_ROUTER_ACTIVITY = True


# =========================================================
# OLLAMA
# =========================================================

OLLAMA_BASE_URL = "http://localhost:11434"

OLLAMA_CHAT_URL = (
    f"{OLLAMA_BASE_URL}/api/chat"
)

OLLAMA_EMBED_URL = (
    f"{OLLAMA_BASE_URL}/api/embed"
)

OLLAMA_OLD_EMBED_URL = (
    f"{OLLAMA_BASE_URL}/api/embeddings"
)


# =========================================================
# MEMORY
# =========================================================

AUTO_MEMORY = True

SHOW_MEMORY_ACTIVITY = True

MEMORY_RETRIEVAL_LIMIT = 8

MEMORY_MANAGER_LIMIT = 15

AUTO_LIFECYCLE_MIN_CONFIDENCE = 0.90


# =========================================================
# SESSION SUMMARIZATION
# =========================================================

SUMMARY_TRIGGER_MESSAGES = 18

RECENT_MESSAGE_LIMIT = 12

SUMMARY_BATCH_LIMIT = 24

MAX_SUMMARY_PASSES = 5

SHOW_SUMMARY_ACTIVITY = True


# =========================================================
# WEB TOOLS
# =========================================================

WEB_SEARCH_PROVIDER = os.environ.get(
    "PRIVATE_AI_WEB_SEARCH_PROVIDER",
    "searxng",
).strip().lower()

SEARXNG_BASE_URL = os.environ.get(
    "PRIVATE_AI_SEARXNG_URL",
    "http://127.0.0.1:8888",
).rstrip("/")

WEB_QUERY_MODEL = FAST_MODEL

WEB_SEARCH_RESULT_LIMIT = 6

WEB_FETCH_RESULT_LIMIT = 3

WEB_SEARCH_TIMEOUT_SECONDS = 15

WEB_FETCH_TIMEOUT_SECONDS = 10

WEB_FETCH_MAX_BYTES = (
    2
    * 1024
    * 1024
)

WEB_FETCH_MAX_CHARS_PER_SOURCE = 5000

WEB_TEXT_CONTEXT_BUDGET = 14000

WEB_VISION_CONTEXT_BUDGET = 6000

WEB_CONTEXT_SIZE = 8192

WEB_SAFE_SEARCH = 1

SHOW_WEB_ACTIVITY = True

# Web access policy. Explicit /web and /fetch commands remain
# available even when automatic web access is off.
VALID_WEB_MODES = (
    "off",
    "auto",
    "always",
)

DEFAULT_WEB_MODE = os.environ.get(
    "PRIVATE_AI_DEFAULT_WEB_MODE",
    "off",
).strip().lower()

if DEFAULT_WEB_MODE not in VALID_WEB_MODES:
    DEFAULT_WEB_MODE = "off"


# =========================================================
# SPEECH
# =========================================================

# -----------------------------
# Speech-to-text
# -----------------------------

STT_PROVIDER = os.environ.get(
    "PRIVATE_AI_STT_PROVIDER",
    "mlx_whisper",
).strip().lower()

# Multilingual Whisper Small is a practical default for a 16 GB
# Apple-silicon machine. It is downloaded on first use and then cached
# locally by Hugging Face / MLX Whisper.
STT_MODEL = os.environ.get(
    "PRIVATE_AI_STT_MODEL",
    "mlx-community/whisper-small-mlx",
).strip()

STT_SAMPLE_RATE = 16000

STT_MAX_SECONDS = int(
    os.environ.get(
        "PRIVATE_AI_STT_MAX_SECONDS",
        "90",
    )
)

STT_MAX_UPLOAD_BYTES = (
    8
    * 1024
    * 1024
)

# Leave empty for Whisper language auto-detection. This is useful for
# multilingual use and can be overridden later by a speech setting.
STT_LANGUAGE = (
    os.environ.get(
        "PRIVATE_AI_STT_LANGUAGE",
        "",
    ).strip().lower()
    or None
)


# -----------------------------
# Text-to-speech
# -----------------------------

TTS_PROVIDER = os.environ.get(
    "PRIVATE_AI_TTS_PROVIDER",
    "kokoro_mlx",
).strip().lower()

# Kokoro 82M is intentionally small and fast enough to coexist with the
# local chat stack. Model files are downloaded once and then cached locally.
TTS_MODEL = os.environ.get(
    "PRIVATE_AI_TTS_MODEL",
    "mlx-community/Kokoro-82M-bf16",
).strip()

TTS_DEFAULT_VOICE = os.environ.get(
    "PRIVATE_AI_TTS_VOICE",
    "af_heart",
).strip()

TTS_SPEED = float(
    os.environ.get(
        "PRIVATE_AI_TTS_SPEED",
        "1.0",
    )
)

TTS_SAMPLE_RATE = int(
    os.environ.get(
        "PRIVATE_AI_TTS_SAMPLE_RATE",
        "24000",
    )
)

if TTS_SAMPLE_RATE not in {
    24000,
    48000,
}:
    TTS_SAMPLE_RATE = 24000

TTS_MAX_CHARS = int(
    os.environ.get(
        "PRIVATE_AI_TTS_MAX_CHARS",
        "3200",
    )
)

SHOW_SPEECH_ACTIVITY = True


# =========================================================
# WEB / FLASK
# =========================================================

WEB_HOST = os.environ.get(
    "PRIVATE_AI_HOST",
    "0.0.0.0",
)

WEB_PORT = int(
    os.environ.get(
        "PRIVATE_AI_PORT",
        "5050",
    )
)

WEB_DEBUG = (
    os.environ.get(
        "PRIVATE_AI_DEBUG",
        "0",
    )
    == "1"
)

SESSION_COOKIE_NAME = (
    "private_ai_session"
)

SESSION_LIFETIME_DAYS = 30

MAX_UPLOAD_BYTES = (
    25
    * 1024
    * 1024
)
