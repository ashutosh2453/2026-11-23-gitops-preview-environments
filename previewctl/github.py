"""Fetch open pull request numbers from the GitHub REST API (stdlib only)."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

Opener = Callable[..., Any]


def open_pull_requests(repo: str, token: str | None = None, *, opener: Opener = urllib.request.urlopen) -> set[int]:
    if not _REPO_RE.match(repo):
        raise ValueError(f"repository must look like owner/name, got {repo!r}")
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    url: str | None = f"{API}/repos/{repo}/pulls?state=open&per_page=100"
    numbers: set[int] = set()
    pages = 0
    while url and pages < 50:
        if not url.startswith(API + "/"):
            raise ValueError(f"refusing to follow pagination link outside the GitHub API: {url!r}")
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})  # noqa: S310
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with opener(req, timeout=30) as resp:
            numbers.update(int(pr["number"]) for pr in json.loads(resp.read().decode("utf-8")))
            match = _NEXT_RE.search(resp.headers.get("Link", "") or "")
        url = match.group(1) if match else None
        pages += 1
    return numbers
