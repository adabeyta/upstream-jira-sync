from __future__ import annotations

import logging

from upstream_jira_sync.config import AppConfig
from upstream_jira_sync.jira import JiraClient
from upstream_jira_sync.models import JiraTicket
from upstream_jira_sync.skill_loader import is_bot_actor
from upstream_jira_sync.state import SyncState

log = logging.getLogger(__name__)


class ManualOverrideGate:
    """Per-run cache of 'last non-bot author per (ticket, field)' for gating Jira writes."""

    def __init__(
        self,
        jira: JiraClient,
        bot_email: str,
        bot_account_id: str = "",
        aliases: tuple[str, ...] = (),
    ) -> None:
        self._jira = jira
        self._bot_email = bot_email
        self._bot_account_id = bot_account_id
        self._aliases = aliases
        self._cache: dict[tuple[str, str], str] = {}
        self._prefetched: set[str] = set()
        self._unreliable: set[str] = set()

    def prefetch(self, ticket_keys: list[str], field_ids: set[str]) -> None:
        """Bulk-load changelogs and record the last non-bot editor per (ticket, field)."""
        keys = [k for k in ticket_keys if k and k not in self._prefetched]
        if not keys or not field_ids:
            self._prefetched.update(keys)
            return

        jql = f"issuekey in ({','.join(keys)})"
        try:
            resp = self._jira._request(
                "GET",
                f"{self._jira._base}/rest/api/3/search/jql",
                params={
                    "jql": jql,
                    "fields": "summary",
                    "expand": "changelog",
                    "maxResults": len(keys),
                },
            ).json()
        except Exception as exc:
            log.warning(
                "  Override-gate prefetch failed (%s); fail-open for %s", exc, keys
            )
            self._unreliable.update(keys)
            self._prefetched.update(keys)
            return

        for issue in resp.get("issues", []):
            key = issue.get("key", "")
            if not key:
                continue
            changelog = issue.get("changelog", {}) or {}
            total = changelog.get("total", 0)
            max_res = changelog.get("maxResults", 0)
            if total and max_res and total > max_res:
                self._unreliable.add(key)

            for hist in changelog.get("histories", []):
                author = hist.get("author") or {}
                actor_is_bot = is_bot_actor(
                    author, self._bot_email, self._bot_account_id, self._aliases
                )
                author_email = (author.get("emailAddress") or "").strip()
                for item in hist.get("items", []):
                    normalized = self._match_field(item, field_ids)
                    if not normalized:
                        continue
                    cache_key = (key, normalized)
                    if actor_is_bot or not author:
                        # Bot or system event after a human edit clears protection.
                        self._cache.pop(cache_key, None)
                    elif author_email:
                        self._cache[cache_key] = author_email
                    else:
                        self._cache.pop(cache_key, None)

        self._prefetched.update(keys)

    @staticmethod
    def _match_field(item: dict, field_ids: set[str]) -> str | None:
        field_id = item.get("fieldId") or ""
        field_name = item.get("field") or ""
        if field_id and field_id in field_ids:
            return field_id
        if field_name and field_name in field_ids:
            return field_name
        return None

    def is_human_owned(self, ticket_key: str, field_id: str) -> bool:
        """True iff a non-bot last-modified field_id on ticket_key. Fail-open on missing data."""
        if ticket_key in self._unreliable:
            log.warning(
                "  Changelog unreliable for %s; fail-open on %s gate",
                ticket_key,
                field_id,
            )
            return False
        if ticket_key not in self._prefetched:
            log.warning(
                "  Override gate consulted without prefetch for %s; fail-open",
                ticket_key,
            )
            return False
        return (ticket_key, field_id) in self._cache

    def is_unreliable(self, ticket_key: str) -> bool:
        """True when this ticket's changelog was truncated or never prefetched."""
        return ticket_key in self._unreliable or ticket_key not in self._prefetched


def story_points_blocked(
    gate: ManualOverrideGate | None,
    config: AppConfig,
    ticket: JiraTicket,
) -> bool:
    """Return True iff a human last-modified the story-points field and mode is auto."""
    if gate is None:
        return False
    field_id = config.story_points_field
    if field_id not in config.manual_override_fields:
        return False
    if not gate.is_human_owned(ticket.key, field_id):
        return False
    if config.manual_override_mode == "auto":
        log.info(
            "  Skipping story-point write on %s: manual override on %s",
            ticket.key,
            field_id,
        )
        return True
    log.info(
        "  [MANUAL-OVERRIDE-SHADOW] Would skip story-point write on %s (manual: %s)",
        ticket.key,
        field_id,
    )
    return False


def status_blocked(
    gate: ManualOverrideGate | None,
    config: AppConfig,
    state: SyncState,
    ticket: JiraTicket,
    current_intent: str,
) -> bool:
    """Return True iff human owns status AND bot intent matches prior snapshot AND mode=auto.

    First encounter (no snapshot) writes through to establish a baseline.
    A changed bot intent overrides the human edit (threshold crossing wins).
    """
    if gate is None:
        return False
    if "status" not in config.manual_override_fields:
        return False
    snapshot = state.get_pr_state_snapshot(ticket.key)
    if snapshot is None:
        return False
    if not gate.is_human_owned(ticket.key, "status"):
        return False
    if snapshot.get("resolved_status") != current_intent:
        return False
    if config.manual_override_mode == "auto":
        log.info(
            "  Skipping status transition on %s -> %s: manual override",
            ticket.key,
            current_intent,
        )
        return True
    log.info(
        "  [MANUAL-OVERRIDE-SHADOW] Would skip status transition on %s -> %s (manual edit)",
        ticket.key,
        current_intent,
    )
    return False
