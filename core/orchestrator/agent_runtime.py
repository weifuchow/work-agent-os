"""Shared helpers for agent runtime selection."""

SUPPORTED_AGENT_RUNTIMES = ("claude", "codex")
DEFAULT_AGENT_RUNTIME = "claude"

_ALIASES = {
    "": DEFAULT_AGENT_RUNTIME,
    "agent_sdk": "claude",
    "claude": "claude",
    "claude_code": "claude",
    "claude_sdk": "claude",
    "codex": "codex",
    "codex_agent": "codex",
    "openai": "codex",
}

_AGENT_RUN_RUNTIME_TYPES = {
    "claude": "claude_code",
    "codex": "codex",
}


def normalize_agent_runtime(
    value: str | None,
    default: str = DEFAULT_AGENT_RUNTIME,
) -> str:
    candidate = (value or default or DEFAULT_AGENT_RUNTIME).strip().lower()
    runtime = _ALIASES.get(candidate)
    if runtime:
        return runtime
    supported = ", ".join(SUPPORTED_AGENT_RUNTIMES)
    raise ValueError(f"Unsupported agent runtime: {value!r}. Expected one of: {supported}")


def get_agent_run_runtime_type(runtime: str | None) -> str:
    normalized = normalize_agent_runtime(runtime)
    return _AGENT_RUN_RUNTIME_TYPES[normalized]
