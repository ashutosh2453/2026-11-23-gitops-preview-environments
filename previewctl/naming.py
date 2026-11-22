"""DNS-1123 safe names for preview namespaces and hosts.

Kubernetes namespace names must be RFC 1123 labels: lower-case alphanumerics and
'-', starting and ending with an alphanumeric, at most 63 characters. Branch names
are arbitrary ("feature/Add_Login!!"), so they are slugged and truncated, and a short
hash of the original branch keeps two long branches from colliding after truncation.
"""

from __future__ import annotations

import hashlib
import re

MAX_LABEL = 63
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def env_name(pr: int, branch: str = "") -> str:
    """Namespace for a pull request, e.g. ``pr-42-add-login``.

    The PR number always survives truncation, so the name is unique per PR even when
    the branch slug is cut. Long slugs get a 6-character hash suffix of the full
    branch name so the name stays stable and recognisable.
    """
    if not isinstance(pr, int) or isinstance(pr, bool) or pr < 0:
        raise ValueError(f"pull request number must be a non-negative integer, got {pr!r}")
    prefix = f"pr-{pr}"
    slug = slugify(branch)
    if not slug:
        return prefix
    name = f"{prefix}-{slug}"
    if len(name) <= MAX_LABEL:
        return name
    digest = hashlib.sha256(branch.encode()).hexdigest()[:6]
    room = MAX_LABEL - len(prefix) - len(digest) - 2
    cut = slug[:room].rstrip("-")
    return f"{prefix}-{cut}-{digest}" if cut else f"{prefix}-{digest}"


def is_dns_label(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)) and len(name) <= MAX_LABEL


def host_for(env: str, domain: str) -> str:
    domain = domain.strip(".").lower()
    if not domain or not all(is_dns_label(part) for part in domain.split(".")):
        raise ValueError(f"invalid preview domain: {domain!r}")
    return f"{env}.{domain}"


def validate_sha(sha: str) -> str:
    sha = sha.strip().lower()
    if not _SHA_RE.match(sha):
        raise ValueError(f"commit SHA must be 7-40 hex characters, got {sha!r}")
    return sha
