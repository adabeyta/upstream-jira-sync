from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Final, Protocol, runtime_checkable

from upstream_jira_sync.config import LLMSettings


@runtime_checkable
class LLMProvider(Protocol):
    """Single-turn completion surface consumed by every AI class (R9).

    The model is bound at provider construction from LLMSettings.model, so
    callers pass only prompt content. Implementations must return the
    stripped text of the first content block and raise on transport errors
    (AI classes catch and skip)."""

    def complete(
        self,
        system: str,
        user_message: str,
        max_tokens: int = 256,
    ) -> str: ...


_BUILTIN_PROVIDERS: Final[dict[str, str]] = {
    "vertex": "upstream_jira_sync.llm.vertex:VertexProvider",
    "anthropic": "upstream_jira_sync.llm.anthropic:AnthropicProvider",
}


def load_provider(settings: LLMSettings) -> LLMProvider:
    """Resolve settings.provider via the 'upstream_jira_sync.llm' entry-point group,
    falling back to the built-in map. Entry points load a class (or factory)
    called as factory(settings) -> LLMProvider."""
    for ep in entry_points(group="upstream_jira_sync.llm"):
        if ep.name == settings.provider:
            return ep.load()(settings)

    target = _BUILTIN_PROVIDERS.get(settings.provider)
    if target is None:
        known = sorted(
            set(_BUILTIN_PROVIDERS)
            | {ep.name for ep in entry_points(group="upstream_jira_sync.llm")}
        )
        raise ValueError(
            f"Unknown llm.provider {settings.provider!r}; available: {known}"
        )
    module_name, _, attr = target.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory(settings)


def provider_load_error(provider: str) -> str:
    """Import (without constructing) the configured provider. Empty string when
    loadable, else the error — e.g. a missing optional extra like [vertex]."""
    try:
        for ep in entry_points(group="upstream_jira_sync.llm"):
            if ep.name == provider:
                ep.load()
                return ""
        target = _BUILTIN_PROVIDERS.get(provider)
        if target is None:
            return f"unknown llm.provider {provider!r}"
        module_name, _, attr = target.partition(":")
        getattr(importlib.import_module(module_name), attr)
        return ""
    except ImportError as exc:
        return str(exc)
