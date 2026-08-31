from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: str
    description: str
    requires_network: bool = False
    sends_query_off_device: bool = False


_TOOL_REGISTRY = {
    "web.search": ToolSpec("web.search", "web_search.use", "Discover public web pages through the configured search provider.", True, True),
    "web.fetch": ToolSpec("web.fetch", "web_search.use", "Fetch and extract readable text from a public URL.", True, False),
    "speech.stt": ToolSpec("speech.stt", "speech.use", "Transcribe microphone audio into text using the local speech-to-text provider."),
    "speech.tts": ToolSpec("speech.tts", "speech.use", "Generate spoken audio from assistant text using the local text-to-speech provider."),
    "document.index": ToolSpec("document.index", "chat.use", "Index readable uploaded document text into the user's local persistent RAG store."),
    "document.search": ToolSpec("document.search", "chat.use", "Semantically retrieve relevant passages from the user's locally indexed documents."),
    "automation.schedule": ToolSpec("automation.schedule", "automation.use", "Create, edit, pause, resume, and schedule persistent personal automation tasks."),
    "automation.execute": ToolSpec("automation.execute", "automation.use", "Execute a scheduled reminder, local AI task, or conditional check."),
    "notification.in_app": ToolSpec("notification.in_app", "automation.use", "Deliver an automation result to the local in-app notification inbox."),
    "agent.run": ToolSpec("agent.run", "agent.use", "Run a persistent iterative local agent with pause/resume state, sources, evidence, and a private workspace."),
    "agent.workspace.write": ToolSpec("agent.workspace.write", "agent.use", "Create or revise text, data, HTML, or source-code files inside one agent run's local workspace."),
    "agent.workspace.list": ToolSpec("agent.workspace.list", "agent.use", "List logical files in one agent run's private workspace."),
    "agent.workspace.read": ToolSpec("agent.workspace.read", "agent.use", "Read one text/source file from one agent run's private workspace."),
    "agent.sandbox.python": ToolSpec("agent.sandbox.python", "agent.code.execute", "Execute one Python workspace file inside a resource-limited, network-disabled Docker sandbox with a read-only workspace mount."),
    "agent.input.request": ToolSpec("agent.input.request", "agent.use", "Pause an agent run at a real decision point and resume the same run after user input."),
}


def get_tool_spec(name):
    return _TOOL_REGISTRY.get(str(name or "").strip())


def list_tool_specs():
    return list(_TOOL_REGISTRY.values())
