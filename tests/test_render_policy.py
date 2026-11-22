import copy
from datetime import datetime, timezone

import yaml

from previewctl import ANN_EXPIRES, ANN_SHA, LABEL_MANAGED_BY, LABEL_PR
from previewctl.config import PreviewConfig
from previewctl.policy import check
from previewctl.render import PreviewTarget, parse_time, render, to_yaml

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"
CFG = PreviewConfig()
TARGET = PreviewTarget(pr=42, sha=SHA, branch="feature/login")
NS = "pr-42-feature-login"


def by_kind(manifests, kind, name=None):
    return [m for m in manifests if m["kind"] == kind and (name is None or m["metadata"]["name"] == name)]


def test_render_is_deterministic_and_round_trips():
    a = to_yaml(render(CFG, TARGET, NOW))
    b = to_yaml(render(CFG, TARGET, NOW))
    assert a == b
    docs = list(yaml.safe_load_all(a))
    assert [d["kind"] for d in docs] == [
        "Namespace",
        "ResourceQuota",
        "LimitRange",
        "NetworkPolicy",
        "NetworkPolicy",
        "Deployment",
        "Service",
        "Ingress",
    ]


def test_everything_lives_in_the_preview_namespace_and_is_labelled():
    ms = render(CFG, TARGET, NOW)
    ns = by_kind(ms, "Namespace")[0]
    assert ns["metadata"]["name"] == NS
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    for m in ms:
        assert m["metadata"]["labels"][LABEL_MANAGED_BY] == "previewctl"
        assert m["metadata"]["labels"][LABEL_PR] == "42"
        if m["kind"] != "Namespace":
            assert m["metadata"]["namespace"] == NS


def test_expiry_sha_image_and_host():
    ms = render(CFG, TARGET, NOW)
    ann = by_kind(ms, "Namespace")[0]["metadata"]["annotations"]
    assert parse_time(ann[ANN_EXPIRES]) == datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    assert ann[ANN_SHA] == SHA
    dep = by_kind(ms, "Deployment")[0]
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == f"preview-app:{SHA}"
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["GIT_SHA"] == SHA and env["PREVIEW_PR"] == "42"
    assert env["PREVIEW_HOST"] == f"{NS}.preview.localtest.me"
    ing = by_kind(ms, "Ingress")[0]
    assert ing["spec"]["rules"][0]["host"] == f"{NS}.preview.localtest.me"


def test_extra_env_is_sorted_after_builtins():
    cfg = PreviewConfig(env={"Z_FLAG": "1", "A_FLAG": "2"})
    container = by_kind(render(cfg, TARGET, NOW), "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    names = [e["name"] for e in container["env"]]
    assert names[-2:] == ["A_FLAG", "Z_FLAG"]


def test_network_policy_only_admits_ingress_controller_and_same_namespace():
    allow = by_kind(render(CFG, TARGET, NOW), "NetworkPolicy", "allow-ingress-controller-and-same-namespace")[0]
    sources = allow["spec"]["ingress"][0]["from"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}} in sources
    assert {"podSelector": {}} in sources
    assert len(sources) == 2


def test_rendered_manifests_pass_policy():
    assert check(render(CFG, TARGET, NOW), NS, CFG.domain) == []


def _rules(manifests):
    return {v.rule for v in check(manifests, NS, CFG.domain)}


def test_policy_catches_escapes():
    base = render(CFG, TARGET, NOW)

    ms = copy.deepcopy(base)
    pod = by_kind(ms, "Deployment")[0]["spec"]["template"]["spec"]
    c = pod["containers"][0]
    c["image"] = "preview-app:latest"
    c["securityContext"] = {"privileged": True}
    del c["resources"]["limits"]
    pod["hostNetwork"] = True
    pod["volumes"] = [{"name": "root", "hostPath": {"path": "/"}}]
    pod["automountServiceAccountToken"] = True
    del pod["securityContext"]
    assert {
        "pinned-image",
        "no-privileged",
        "resource-limits",
        "no-host-namespaces",
        "no-host-path",
        "no-sa-token",
        "no-privilege-escalation",
        "non-root",
        "drop-capabilities",
    } <= _rules(ms)


def test_policy_blocks_cross_namespace_and_cluster_scope():
    ms = copy.deepcopy(render(CFG, TARGET, NOW))
    ms.append({"kind": "ClusterRoleBinding", "metadata": {"name": "pwn"}})
    ms.append({"kind": "ConfigMap", "metadata": {"name": "x", "namespace": "kube-system"}})
    ms.append({"kind": "Namespace", "metadata": {"name": "other"}})
    svc = by_kind(ms, "Service")[0]
    svc["spec"]["type"] = "LoadBalancer"
    by_kind(ms, "Ingress")[0]["spec"]["rules"][0]["host"] = "evil.example.com"
    assert {"allowed-kinds", "single-namespace", "cluster-ip-only", "preview-domain"} <= _rules(ms)


def test_policy_requires_quota_and_network_policy():
    ms = [m for m in render(CFG, TARGET, NOW) if m["kind"] not in {"ResourceQuota", "NetworkPolicy"}]
    assert {"quota-required", "network-policy-required"} <= _rules(ms)


def test_digest_pinned_images_are_accepted():
    ms = copy.deepcopy(render(CFG, TARGET, NOW))
    c = by_kind(ms, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    c["image"] = "registry.local:5000/app@sha256:" + "a" * 64
    assert "pinned-image" not in _rules(ms)
    c["image"] = "registry.local:5000/app"  # port colon is not a tag
    assert "pinned-image" in _rules(ms)


def test_extra_manifests_cannot_replace_generated_guardrails():
    from previewctl.policy import check_extra

    extras = [
        {"kind": "Namespace", "metadata": {"name": NS, "labels": {}}},
        {"kind": "ResourceQuota", "metadata": {"name": "preview-quota", "namespace": NS}},
        {"kind": "LimitRange", "metadata": {"name": "preview-defaults", "namespace": NS}},
        {
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-all", "namespace": NS},
            "spec": {"podSelector": {}, "ingress": [{}]},
        },
        {"kind": "ConfigMap", "metadata": {"name": "fine", "namespace": NS}},
    ]
    found = check_extra(extras)
    assert [v.kind for v in found] == ["Namespace", "ResourceQuota", "LimitRange", "NetworkPolicy"]
    assert {v.rule for v in found} == {"reserved-kind"}


def test_ingress_default_backend_and_hostless_rules_are_rejected():
    ms = copy.deepcopy(render(CFG, TARGET, NOW))
    ing = by_kind(ms, "Ingress")[0]
    ing["spec"] = {"defaultBackend": {"service": {"name": "web", "port": {"number": 80}}}}
    assert {"no-default-backend", "preview-domain"} <= _rules(ms)
    ing["spec"] = {"rules": [{"http": {"paths": []}}]}
    assert "preview-domain" in _rules(ms)
