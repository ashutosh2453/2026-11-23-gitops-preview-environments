"""Render the Kubernetes manifests for one pull request's preview environment.

Rendering is a pure function of (config, PR, branch, SHA, time): the same inputs give
byte-identical YAML, so the GitOps directory only changes when something real changes.
Every push re-renders with a fresh expiry, so an active PR's preview never expires
while an abandoned one is collected after ``ttl_hours``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml

from . import ANN_BRANCH, ANN_EXPIRES, ANN_SHA, LABEL_MANAGED_BY, LABEL_PR, MANAGED_BY
from .config import PreviewConfig
from .naming import env_name, host_for, validate_sha

Manifest = dict[str, Any]


@dataclass(frozen=True)
class PreviewTarget:
    pr: int
    sha: str
    branch: str = ""

    @property
    def namespace(self) -> str:
        return env_name(self.pr, self.branch)


def format_time(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def render(cfg: PreviewConfig, target: PreviewTarget, now: datetime | None = None) -> list[Manifest]:
    sha = validate_sha(target.sha)
    now = now or datetime.now(timezone.utc)
    ns = target.namespace
    host = host_for(ns, cfg.domain)
    app = cfg.app
    common = {LABEL_MANAGED_BY: MANAGED_BY, LABEL_PR: str(target.pr)}
    selector = {"app.kubernetes.io/name": app}
    pod_labels = {**selector, **common}

    def meta(name: str, **extra: Any) -> dict[str, Any]:
        return {"name": name, "namespace": ns, "labels": dict(common), **extra}

    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": ns,
            "labels": {
                **common,
                # Enforce the "restricted" Pod Security Standard inside every preview.
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
            "annotations": {
                ANN_EXPIRES: format_time(now + timedelta(hours=cfg.ttl_hours)),
                ANN_SHA: sha,
                ANN_BRANCH: target.branch,
            },
        },
    }
    quota = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": meta("preview-quota"),
        "spec": {
            "hard": {
                "limits.cpu": cfg.quota_cpu,
                "limits.memory": cfg.quota_memory,
                "requests.cpu": cfg.quota_cpu,
                "requests.memory": cfg.quota_memory,
                "pods": str(cfg.quota_pods),
                "services.loadbalancers": "0",
                "services.nodeports": "0",
                "persistentvolumeclaims": "0",
            }
        },
    }
    limits = {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": meta("preview-defaults"),
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {"cpu": cfg.cpu_limit, "memory": cfg.memory_limit},
                    "defaultRequest": {"cpu": cfg.cpu_request, "memory": cfg.memory_request},
                }
            ]
        },
    }
    deny_all = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": meta("default-deny-ingress"),
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
    }
    allow = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": meta("allow-ingress-controller-and-same-namespace"),
        "spec": {
            "podSelector": {"matchLabels": selector},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": cfg.ingress_namespace}}},
                        {"podSelector": {}},
                    ],
                    "ports": [{"protocol": "TCP", "port": cfg.port}],
                }
            ],
        },
    }
    env = [
        {"name": "PREVIEW_PR", "value": str(target.pr)},
        {"name": "PREVIEW_NAMESPACE", "value": ns},
        {"name": "PREVIEW_HOST", "value": host},
        {"name": "GIT_SHA", "value": sha},
        {"name": "PORT", "value": str(cfg.port)},
    ] + [{"name": k, "value": v} for k, v in sorted(cfg.env.items())]
    probe = {"httpGet": {"path": cfg.health_path, "port": "http"}, "periodSeconds": 5}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta(app, annotations={ANN_SHA: sha}),
        "spec": {
            "replicas": cfg.replicas,
            "revisionHistoryLimit": 2,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": pod_labels, "annotations": {ANN_SHA: sha}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": app,
                            "image": f"{cfg.image}:{sha}",
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"name": "http", "containerPort": cfg.port}],
                            "env": env,
                            "readinessProbe": {**probe, "initialDelaySeconds": 1},
                            "livenessProbe": {**probe, "initialDelaySeconds": 10},
                            "resources": {
                                "requests": {"cpu": cfg.cpu_request, "memory": cfg.memory_request},
                                "limits": {"cpu": cfg.cpu_limit, "memory": cfg.memory_limit},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": meta(app),
        "spec": {
            "type": "ClusterIP",
            "selector": selector,
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    }
    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": meta(app),
        "spec": {
            "ingressClassName": cfg.ingress_class,
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": app, "port": {"name": "http"}}},
                            }
                        ]
                    },
                }
            ],
        },
    }
    return [namespace, quota, limits, deny_all, allow, deployment, service, ingress]


def to_yaml(manifests: list[Manifest]) -> str:
    return yaml.safe_dump_all(manifests, sort_keys=True, default_flow_style=False, explicit_start=True)
