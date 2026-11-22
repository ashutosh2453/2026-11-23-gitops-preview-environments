"""Decide which preview namespaces to delete.

Planning is pure (namespaces + open PRs + time in, actions out) so it can be unit
tested and printed as a dry run before anything is deleted. Safety rules:

* only namespaces labelled ``app.kubernetes.io/managed-by=previewctl`` are considered,
  so the collector can never touch ``kube-system`` or a team's namespace;
* ``previewctl/pinned=true`` keeps a preview alive (for a demo, say);
* a namespace with a missing or unreadable expiry is reported, not deleted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import ANN_EXPIRES, LABEL_MANAGED_BY, LABEL_PINNED, LABEL_PR, MANAGED_BY
from .render import parse_time


@dataclass(frozen=True)
class GcDecision:
    namespace: str
    pr: int | None
    reason: str
    delete: bool


def _meta(ns: dict[str, Any]) -> dict[str, Any]:
    return ns.get("metadata", {}) or {}


def plan(namespaces: list[dict[str, Any]], open_prs: set[int] | None, now: datetime) -> list[GcDecision]:
    """Return one decision per previewctl namespace, sorted by name.

    ``open_prs`` is None when the PR list is unknown (no GitHub access); then only the
    TTL is used, which is still safe because active PRs refresh their expiry on push.
    """
    managed = [
        ns
        for ns in namespaces
        if (_meta(ns).get("labels") or {}).get(LABEL_MANAGED_BY) == MANAGED_BY
        and (ns.get("status") or {}).get("phase") != "Terminating"
    ]

    # Several namespaces for one PR happen when the head branch is renamed; keep the newest.
    by_pr: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ns in managed:
        pr = _pr(ns)
        if pr is not None:
            by_pr[pr].append(ns)
    superseded: set[str] = set()
    for group in by_pr.values():
        if len(group) > 1:
            group.sort(key=lambda n: (_meta(n).get("creationTimestamp") or "", _meta(n).get("name", "")))
            superseded.update(_meta(n)["name"] for n in group[:-1])

    decisions = []
    for ns in managed:
        meta = _meta(ns)
        name = meta["name"]
        labels = meta.get("labels") or {}
        pr = _pr(ns)
        if labels.get(LABEL_PINNED) == "true":
            decisions.append(GcDecision(name, pr, "pinned", False))
        elif pr is None:
            decisions.append(GcDecision(name, pr, "missing-pr-label", False))
        elif (expires := _expiry(meta)) is None:
            # Metadata previewctl did not write (or someone edited): report, never delete.
            decisions.append(GcDecision(name, pr, "missing-expiry", False))
        elif open_prs is not None and pr not in open_prs:
            decisions.append(GcDecision(name, pr, "pr-closed", True))
        elif name in superseded:
            decisions.append(GcDecision(name, pr, "superseded", True))
        elif expires <= now:
            decisions.append(GcDecision(name, pr, "expired", True))
        else:
            decisions.append(GcDecision(name, pr, "active", False))
    return sorted(decisions, key=lambda d: d.namespace)


def _pr(ns: dict[str, Any]) -> int | None:
    raw = (_meta(ns).get("labels") or {}).get(LABEL_PR)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _expiry(meta: dict[str, Any]) -> datetime | None:
    raw = (meta.get("annotations") or {}).get(ANN_EXPIRES)
    if not raw:
        return None
    try:
        return parse_time(raw)
    except ValueError:
        return None
