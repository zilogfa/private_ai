from pathlib import Path


# =========================================================
# PROJECT PATHS
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
GENERATED_DIR = PROJECT_ROOT / "generated"


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