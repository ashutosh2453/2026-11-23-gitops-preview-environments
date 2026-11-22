"""Preview configuration (``preview.yaml``), validated up front.

Bad config should fail before anything touches a cluster, with a message that names
the field. Unknown keys are rejected because a typo like ``ttl_hour`` would otherwise
silently fall back to the default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .naming import is_dns_label

_CPU_RE = re.compile(r"^\d+(\.\d+)?m?$")
_MEM_RE = re.compile(r"^\d+(Ki|Mi|Gi)$")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_IMAGE_RE = re.compile(r"^[a-z0-9]+([._/:-][a-z0-9]+)*$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PreviewConfig:
    app: str = "web"
    image: str = "preview-app"
    domain: str = "preview.localtest.me"
    port: int = 8080
    replicas: int = 1
    ttl_hours: int = 72
    cpu_request: str = "50m"
    cpu_limit: str = "250m"
    memory_request: str = "64Mi"
    memory_limit: str = "128Mi"
    quota_cpu: str = "1"
    quota_memory: str = "512Mi"
    quota_pods: int = 6
    ingress_class: str = "traefik"
    ingress_namespace: str = "kube-system"
    health_path: str = "/healthz"
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_dns_label(self.app):
            raise ConfigError(f"app: {self.app!r} is not a DNS-1123 label")
        if not _IMAGE_RE.match(self.image) or ":" in self.image.rsplit("/", 1)[-1]:
            raise ConfigError(f"image: {self.image!r} must be a repository without a tag (the commit SHA is the tag)")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"port: {self.port} is out of range")
        if not (1 <= self.replicas <= 3):
            raise ConfigError("replicas: previews run 1-3 replicas")
        if not (1 <= self.ttl_hours <= 24 * 30):
            raise ConfigError("ttl_hours: must be between 1 and 720")
        for name in ("cpu_request", "cpu_limit", "quota_cpu"):
            if not _CPU_RE.match(getattr(self, name)):
                raise ConfigError(f"{name}: {getattr(self, name)!r} is not a CPU quantity like 100m or 1")
        for name in ("memory_request", "memory_limit", "quota_memory"):
            if not _MEM_RE.match(getattr(self, name)):
                raise ConfigError(f"{name}: {getattr(self, name)!r} is not a memory quantity like 128Mi")
        if cpu_millis(self.cpu_request) > cpu_millis(self.cpu_limit):
            raise ConfigError("cpu_request must not exceed cpu_limit")
        if mem_bytes(self.memory_request) > mem_bytes(self.memory_limit):
            raise ConfigError("memory_request must not exceed memory_limit")
        if cpu_millis(self.cpu_limit) * self.replicas > cpu_millis(self.quota_cpu):
            raise ConfigError("quota_cpu is too small for replicas x cpu_limit")
        if mem_bytes(self.memory_limit) * self.replicas > mem_bytes(self.quota_memory):
            raise ConfigError("quota_memory is too small for replicas x memory_limit")
        if not self.health_path.startswith("/"):
            raise ConfigError("health_path must start with '/'")
        for key in self.env:
            if not _ENV_KEY_RE.match(key):
                raise ConfigError(f"env: {key!r} is not an UPPER_SNAKE_CASE variable name")
            if key.startswith("PREVIEW_") or key == "GIT_SHA":
                raise ConfigError(f"env: {key!r} is reserved for previewctl")
            if any(word in key for word in ("SECRET", "PASSWORD", "TOKEN", "API_KEY")):
                raise ConfigError(f"env: {key!r} looks like a secret; previews must not carry secrets in plain config")


def cpu_millis(q: str) -> int:
    return int(float(q[:-1])) if q.endswith("m") else int(float(q) * 1000)


def mem_bytes(q: str) -> int:
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}
    return int(q[:-2]) * units[q[-2:]]


def from_dict(data: dict[str, Any] | None) -> PreviewConfig:
    data = dict(data or {})
    known = {f.name for f in fields(PreviewConfig)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ConfigError("env: must be a mapping")
    data["env"] = {str(k): str(v) for k, v in env.items()}
    try:
        return PreviewConfig(**data)
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc


def load(path: str | Path | None) -> PreviewConfig:
    if path is None:
        return PreviewConfig()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return from_dict(raw)
