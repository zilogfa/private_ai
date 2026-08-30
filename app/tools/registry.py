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
    "document.index": ToolSpec(
        name="document.index",
        capability="chat.use",
        description=(
            "Index readable uploaded document text into the user's local "
            "persistent RAG store."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "document.search": ToolSpec(
        name="document.search",
        capability="chat.use",
        description=(
            "Semantically retrieve relevant passages from the user's locally "
            "indexed documents."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "automation.schedule": ToolSpec(
        name="automation.schedule",
        capability="automation.use",
        description=(
            "Create, edit, pause, resume, and schedule persistent personal "
            "automation tasks."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "automation.execute": ToolSpec(
        name="automation.execute",
        capability="automation.use",
        description=(
            "Execute a scheduled reminder, local AI task, or conditional check."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "notification.in_app": ToolSpec(
        name="notification.in_app",
        capability="automation.use",
        description=(
            "Deliver an automation result to the local in-app notification inbox."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "agent.run": ToolSpec(
        name="agent.run",
        capability="agent.use",
        description=(
            "Run a persistent iterative local agent with a step budget, "
            "pause/resume state, sources, evidence, and a private workspace."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "agent.workspace.write": ToolSpec(
        name="agent.workspace.write",
        capability="agent.use",
        description=(
            "Create inert text, data, HTML, or source-code artifacts inside "
            "one agent run's isolated local workspace."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
    "agent.input.request": ToolSpec(
        name="agent.input.request",
        capability="agent.use",
        description=(
            "Pause an agent run at a real decision point and resume the same "
            "run after the user provides additional input."
        ),
        requires_network=False,
        sends_query_off_device=False,
    ),
}


def get_tool_spec(name):
    return _TOOL_REGISTRY.get(str(name or "").strip())


def list_tool_specs():
    return list(_TOOL_REGISTRY.values())
