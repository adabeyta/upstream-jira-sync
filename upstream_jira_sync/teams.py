"""Pure helpers over the config-driven team taxonomy (R7)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from upstream_jira_sync.config import TeamSpec


def teams_to_labels(teams: Sequence[TeamSpec], names: Iterable[str]) -> list[str]:
    """Sorted Jira labels for the given canonical team names; unknown names dropped."""
    by_name = {t.name: t.label for t in teams}
    return sorted(by_name[n] for n in names if n in by_name)


def primary_team_id(teams: Sequence[TeamSpec], names: list[str]) -> str | None:
    """Team id for the primary (first) classified team, or None when unset/unknown."""
    if not names:
        return None
    spec = next((t for t in teams if t.name == names[0]), None)
    if spec is None:
        return None
    return spec.team_id or None


def canonical_team_names(teams: Sequence[TeamSpec]) -> frozenset[str]:
    return frozenset(t.name for t in teams)


def render_team_prompt_section(teams: Sequence[TeamSpec]) -> str:
    """Deterministic bullet list injected into the team_classification prompt
    as {teams_section}; names only — scope guidance belongs in a skills_dir override."""
    return "\n".join(f"- {t.name}" for t in teams)
