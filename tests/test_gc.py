from datetime import datetime, timezone

from previewctl import ANN_EXPIRES, LABEL_MANAGED_BY, LABEL_PINNED, LABEL_PR
from previewctl.gc import plan

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
FUTURE = "2026-11-25T00:00:00Z"
PAST = "2026-11-22T00:00:00Z"


def ns(name, pr=None, expires=FUTURE, managed=True, pinned=False, created="2026-11-20T00:00:00Z", phase="Active"):
    labels = {}
    if managed:
        labels[LABEL_MANAGED_BY] = "previewctl"
    if pr is not None:
        labels[LABEL_PR] = str(pr)
    if pinned:
        labels[LABEL_PINNED] = "true"
    annotations = {ANN_EXPIRES: expires} if expires is not None else {}
    return {
        "metadata": {"name": name, "labels": labels, "annotations": annotations, "creationTimestamp": created},
        "status": {"phase": phase},
    }


def reasons(decisions):
    return {d.namespace: (d.reason, d.delete) for d in decisions}


def test_unmanaged_and_terminating_namespaces_are_never_touched():
    out = plan(
        [ns("kube-system", managed=False, expires=PAST), ns("pr-1", 1, PAST, phase="Terminating")],
        set(),
        NOW,
    )
    assert out == []


def test_closed_expired_active_pinned():
    out = reasons(
        plan(
            [
                ns("pr-1-a", 1),
                ns("pr-2-b", 2),
                ns("pr-3-c", 3, PAST),
                ns("pr-4-d", 4, PAST, pinned=True),
            ],
            {1, 3, 4},
            NOW,
        )
    )
    assert out == {
        "pr-1-a": ("active", False),
        "pr-2-b": ("pr-closed", True),
        "pr-3-c": ("expired", True),
        "pr-4-d": ("pinned", False),
    }


def test_without_github_only_ttl_applies():
    out = reasons(plan([ns("pr-1", 1), ns("pr-2", 2, PAST)], None, NOW))
    assert out == {"pr-1": ("active", False), "pr-2": ("expired", True)}


def test_expiry_boundary_is_inclusive():
    out = reasons(plan([ns("pr-1", 1, "2026-11-23T12:00:00Z")], None, NOW))
    assert out["pr-1"] == ("expired", True)


def test_bad_metadata_is_reported_not_deleted():
    out = reasons(
        plan(
            [ns("pr-x", None), ns("pr-5", 5, None), ns("pr-6", 6, "next tuesday"), ns("pr-7", "-1")],
            {5, 6},
            NOW,
        )
    )
    assert out == {
        "pr-x": ("missing-pr-label", False),
        "pr-5": ("missing-expiry", False),
        "pr-6": ("missing-expiry", False),
        "pr-7": ("missing-pr-label", False),
    }


def test_renamed_branch_keeps_only_newest_namespace():
    out = reasons(
        plan(
            [
                ns("pr-9-old-name", 9, created="2026-11-20T00:00:00Z"),
                ns("pr-9-new-name", 9, created="2026-11-22T00:00:00Z"),
            ],
            {9},
            NOW,
        )
    )
    assert out == {"pr-9-old-name": ("superseded", True), "pr-9-new-name": ("active", False)}


def test_closed_wins_over_superseded():
    out = reasons(plan([ns("pr-9-a", 9, created="1"), ns("pr-9-b", 9, created="2")], set(), NOW))
    assert out == {"pr-9-a": ("pr-closed", True), "pr-9-b": ("pr-closed", True)}
