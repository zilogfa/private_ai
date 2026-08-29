from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: str
    description: str
    requires_network: bool = False
    sends_query_off_device: bool = False


_TOOL_REGISTRY = {
    "web.search": ToolSpec(
        name="web.search",
        capability="web_search.use",
        description=(
            "Discover public web pages through the configured "
            "search provider."
        ),
        requires_network=True,
        sends_query_off_device=True,
    ),
    "web.fetch": ToolSpec(
        name="web.fetch",
        capability="web_search.use",
        description=(
            "Fetch and extract readable text from a public URL."
        ),
        requires_network=True,
        sends_query_off_device=False,
    ),
    "speech.stt": ToolSpec(
        name="speech.stt",
        capability="speech.use",
        description=(
            "Transcribe microphone audio into text using the local "
            "speech-to-text provider."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "speech.tts": ToolSpec(
        name="speech.tts",
        capability="speech.use",
        description=(
            "Generate spoken audio from assistant text using the local "
            "text-to-speech provider."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "image.generate": ToolSpec(
        name="image.generate",
        capability="image_generation.use",
        description=(
            "Generate an image from a text prompt using the local "
            "Apple-silicon image generation provider."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
}


def get_tool_spec(name):
    return _TOOL_REGISTRY.get(
        str(name or "").strip()
    )


def list_tool_specs():
    return list(
        _TOOL_REGISTRY.values()
    )
