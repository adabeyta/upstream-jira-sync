"""StatusResolver, lifecycle derivation, and PR merge/cancel semantics."""

import pytest
from conftest import make_pr
from upstream_jira_sync.models import CanonicalStatus, PRLifecycleState, ReviewDecision
from upstream_jira_sync.resolver import StatusResolver, derive_lifecycle_state


class TestStatusResolver:
    def setup_method(self):
        self.resolver = StatusResolver(significant_comments_threshold=3)

    def test_merged_pr_returns_done(self):
        pr = make_pr(state="closed", merged=True)
        assert self.resolver.resolve(pr, ReviewDecision.NONE, 0) == CanonicalStatus.DONE

    def test_open_pr_returns_review(self):
        pr = make_pr(state="open")
        assert (
            self.resolver.resolve(pr, ReviewDecision.NONE, 0) == CanonicalStatus.REVIEW
        )

    def test_minor_changes_stays_in_review(self):
        pr = make_pr(state="open")
        assert (
            self.resolver.resolve(pr, ReviewDecision.CHANGES_REQUESTED, 2)
            == CanonicalStatus.REVIEW
        )

    def test_significant_changes_returns_in_progress(self):
        pr = make_pr(state="open")
        assert (
            self.resolver.resolve(pr, ReviewDecision.CHANGES_REQUESTED, 3)
            == CanonicalStatus.IN_PROGRESS
        )

    def test_closed_not_merged_returns_done(self):
        pr = make_pr(state="closed", merged=False)
        assert self.resolver.resolve(pr, ReviewDecision.NONE, 0) == CanonicalStatus.DONE

    def test_merge_label_pr_returns_done(self):
        # A merge bot closes without merged=true and applies a configured label.
        pr = make_pr(
            state="closed", merged=False, labels=("Merged",), merge_labels=("Merged",)
        )
        assert self.resolver.resolve(pr, ReviewDecision.NONE, 0) == CanonicalStatus.DONE


class TestMergeLabelsOptIn:
    def test_merge_labels_default_off(self):
        # Without configured merge_labels a labeled close stays a cancel (R6).
        pr = make_pr(state="closed", merged=False, labels=("Merged",))
        assert pr.effectively_merged is False
        assert pr.is_cancelled is True

    def test_merge_labels_enable_effective_merge(self):
        pr = make_pr(
            state="closed", merged=False, labels=("Merged",), merge_labels=("Merged",)
        )
        assert pr.effectively_merged is True
        assert pr.is_cancelled is False

    def test_merge_label_match_is_case_insensitive(self):
        pr = make_pr(
            state="closed", merged=False, labels=("merged",), merge_labels=("Merged",)
        )
        assert pr.effectively_merged is True

    def test_status_icon_reflects_effective_merge(self):
        assert (
            make_pr(
                state="closed",
                merged=False,
                labels=("Merged",),
                merge_labels=("Merged",),
            ).status_icon
            == "[merged]"
        )
        assert make_pr(state="closed", merged=False).status_icon == "[closed]"
        assert make_pr(state="open").status_icon == "[open]"


class TestDeriveLifecycleState:
    @pytest.mark.parametrize(
        "merged,state,review_decision,changes_requested_count,expected",
        [
            (True, "closed", None, 0, "MERGED"),
            (False, "closed", None, 0, "CLOSED_UNMERGED"),
            (False, "open", "CHANGES_REQUESTED", 0, "CHANGES_REQUESTED"),
            (False, "open", None, 2, "CHANGES_REQUESTED"),
            (False, "open", None, 0, "OPEN"),
        ],
    )
    def test_covers_all_branches(
        self, merged, state, review_decision, changes_requested_count, expected
    ):
        pr = make_pr(state=state, merged=merged)
        assert (
            derive_lifecycle_state(pr, review_decision, changes_requested_count)
            == PRLifecycleState[expected]
        )
