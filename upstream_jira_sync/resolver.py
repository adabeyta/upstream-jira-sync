from __future__ import annotations

from upstream_jira_sync.models import (
    CanonicalStatus,
    PRLifecycleState,
    PullRequest,
    ReviewDecision,
)


def derive_lifecycle_state(
    pr: PullRequest,
    review_decision: str | None,
    changes_requested_count: int,
) -> PRLifecycleState:
    if pr.effectively_merged:
        return PRLifecycleState.MERGED
    if pr.state == "closed":
        return PRLifecycleState.CLOSED_UNMERGED
    if (
        review_decision == ReviewDecision.CHANGES_REQUESTED.value
        or review_decision == ReviewDecision.CHANGES_REQUESTED
        or changes_requested_count > 0
    ):
        return PRLifecycleState.CHANGES_REQUESTED
    return PRLifecycleState.OPEN


class StatusResolver:
    """
    Maps a GitHub PR's current state + review decision -> target canonical status.

      PR opened / approved / minor feedback  -> REVIEW
      PR with significant rework requested   -> IN_PROGRESS
      PR merged                              -> DONE
      PR closed but not merged (cancelled)   -> DONE
    """

    def __init__(self, significant_comments_threshold: int) -> None:
        self._threshold = significant_comments_threshold

    def resolve(
        self,
        pr: PullRequest,
        review_decision: ReviewDecision,
        changes_requested_count: int,
    ) -> CanonicalStatus:
        """Return the target CanonicalStatus for this PR (every PR state maps to one)."""
        if pr.effectively_merged:
            return CanonicalStatus.DONE

        if pr.state == "open":
            if review_decision == ReviewDecision.CHANGES_REQUESTED:
                if changes_requested_count >= self._threshold:
                    return CanonicalStatus.IN_PROGRESS
            return CanonicalStatus.REVIEW

        # PR was closed without merging (cancelled) — close the ticket. The
        # orchestrator debounces this one run to ride out close/reopen churn.
        return CanonicalStatus.DONE
