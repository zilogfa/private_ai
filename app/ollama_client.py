import json

import requests

from app.config import (
    EMBEDDING_MODEL,
    OLLAMA_CHAT_URL,
    OLLAMA_EMBED_URL,
    OLLAMA_OLD_EMBED_URL,
)


# =========================================================
# EXCEPTIONS
# =========================================================

class OllamaError(Exception):
    """Base exception for Ollama communication errors."""


class OllamaConnectionError(OllamaError):
    """Raised when Ollama cannot be reached."""


class OllamaRequestError(OllamaError):
    """Raised when Ollama returns an HTTP/request error."""


class OllamaResponseError(OllamaError):
    """Raised when Ollama returns invalid response data."""


# =========================================================
# NON-STREAMING CHAT
# =========================================================

def chat_once(
    model,
    messages,
    response_format=None,
    options=None,
    timeout=300,
):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }

    if response_format is not None:
        payload["format"] = response_format

    if options is not None:
        payload["options"] = options

    try:
        response = requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            timeout=timeout,
        )

        response.raise_for_status()

    except requests.exceptions.ConnectionError as error:
        raise OllamaConnectionError(
            "Could not connect to Ollama."
        ) from error

    except requests.exceptions.RequestException as error:
        raise OllamaRequestError(
            str(error)
        ) from error

    try:
        return response.json()

    except ValueError as error:
        raise OllamaResponseError(
            "Ollama returned invalid JSON."
        ) from error


# =========================================================
# STREAMING CHAT
# =========================================================

def chat_stream(
    model,
    messages,
    options=None,
    timeout=600,
):
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }

    if options is not None:
        payload["options"] = options

    try:
        with requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            stream=True,
            timeout=timeout,
        ) as response:

            response.raise_for_status()

            for line in response.iter_lines():

                if not line:
                    continue

                try:
                    data = json.loads(line)

                except json.JSONDecodeError as error:
                    raise OllamaResponseError(
                        "Invalid JSON received "
                        "from Ollama stream."
                    ) from error

                yield data

    except requests.exceptions.ConnectionError as error:
        raise OllamaConnectionError(
            "Could not connect to Ollama."
        ) from error

    except requests.exceptions.RequestException as error:
        raise OllamaRequestError(
            str(error)
        ) from error


# =========================================================
# EMBEDDINGS
# =========================================================

def get_embedding(
    text,
    show_error=True,
):
    text = text.strip()

    if not text:
        return None

    # -----------------------------------------------------
    # Current Ollama embedding endpoint
    # -----------------------------------------------------

    try:
        payload = {
            "model": EMBEDDING_MODEL,
            "input": text,
        }

        response = requests.post(
            OLLAMA_EMBED_URL,
            json=payload,
            timeout=300,
        )

        response.raise_for_status()

        data = response.json()

        embeddings = data.get(
            "embeddings"
        )

        if (
            embeddings
            and isinstance(
                embeddings,
                list,
            )
        ):
            return embeddings[0]

    except Exception:
        pass

    # -----------------------------------------------------
    # Older Ollama embedding endpoint fallback
    # -----------------------------------------------------

    try:
        payload = {
            "model": EMBEDDING_MODEL,
            "prompt": text,
        }

        response = requests.post(
            OLLAMA_OLD_EMBED_URL,
            json=payload,
            timeout=300,
        )

        response.raise_for_status()

        data = response.json()

        embedding = data.get(
            "embedding"
        )

        if embedding:
            return embedding

    except Exception as error:

        if show_error:
            print(
                "\nEmbedding error:"
                f" {error}\n"
            )

    return None