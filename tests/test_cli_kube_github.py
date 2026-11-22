import io
import json
import subprocess

import pytest
import yaml

from previewctl import ANN_EXPIRES, LABEL_MANAGED_BY, LABEL_PR, cli
from previewctl.github import open_pull_requests
from previewctl.kube import Kubectl, KubectlError

SHA = "abcdef1234567"


class FakeRunner:
    """Records kubectl invocations and replays canned output."""

    def __init__(self, outputs=None, rc=0):
        self.calls = []
        self.outputs = outputs or {}
        self.rc = rc

    def __call__(self, cmd, input=None, **kwargs):
        self.calls.append((cmd, input))
        key = next((k for k in self.outputs if k in " ".join(cmd)), None)
        return subprocess.CompletedProcess(cmd, self.rc, self.outputs.get(key, ""), "boom" if self.rc else "")


def run_cli(argv, monkeypatch=None, runner=None):
    if runner is not None:
        monkeypatch.setattr(cli, "Kubectl", lambda context=None: Kubectl(context, runner=runner))
    buf = io.StringIO()
    code = cli.main(argv, out=buf)
    return code, buf.getvalue()


def test_kubectl_command_lines():
    r = FakeRunner()
    k = Kubectl("k3d-preview", runner=r)
    k.apply("x: 1", dry_run=True)
    k.rollout_status("pr-1", "web", 30)
    k.delete_namespace("pr-1")
    cmds = [c for c, _ in r.calls]
    assert cmds[0] == ["kubectl", "--context", "k3d-preview", "apply", "-f", "-", "--dry-run=server"]
    assert r.calls[0][1] == "x: 1"
    assert cmds[1][3:] == ["-n", "pr-1", "rollout", "status", "deployment/web", "--timeout=30s"]
    assert cmds[2][-4:] == ["namespace", "pr-1", "--ignore-not-found", "--wait=false"]


def test_kubectl_errors_surface_stderr():
    with pytest.raises(KubectlError, match="boom"):
        Kubectl(runner=FakeRunner(rc=1)).apply("x: 1")


def test_render_stdout_and_gitops_dir(tmp_path):
    code, text = run_cli(["render", "--pr", "5", "--sha", SHA, "--branch", "feat/x"])
    assert code == 0
    assert [d["kind"] for d in yaml.safe_load_all(text)][0] == "Namespace"

    code, msg = run_cli(["render", "--pr", "5", "--sha", SHA, "--branch", "feat/x", "--out", str(tmp_path)])
    path = tmp_path / "pr-5-feat-x" / "manifests.yaml"
    assert code == 0 and path.exists() and b"\r\n" not in path.read_bytes()


def test_render_rejects_dangerous_extra_manifest(tmp_path, capsys):
    extra = tmp_path / "extra.yaml"
    extra.write_text(
        "kind: ClusterRoleBinding\nmetadata: {name: pwn}\n---\nkind: ConfigMap\nmetadata: {name: ok}\n",
        encoding="utf-8",
    )
    code, _ = run_cli(["render", "--pr", "5", "--sha", SHA, "--extra", str(extra)])
    assert code == 2
    assert "allowed-kinds" in capsys.readouterr().err


def test_extra_manifest_defaults_to_preview_namespace(tmp_path):
    extra = tmp_path / "cm.yaml"
    extra.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings}\ndata: {a: b}\n", encoding="utf-8")
    code, text = run_cli(["render", "--pr", "5", "--sha", SHA, "--extra", str(extra)])
    cm = [d for d in yaml.safe_load_all(text) if d["kind"] == "ConfigMap"][0]
    assert code == 0 and cm["metadata"]["namespace"] == "pr-5"


def test_bad_config_and_sha_exit_1(tmp_path, capsys):
    cfg = tmp_path / "preview.yaml"
    cfg.write_text("ttl_hour: 1\n", encoding="utf-8")
    assert run_cli(["render", "--pr", "1", "--sha", SHA, "--config", str(cfg)])[0] == 1
    assert run_cli(["render", "--pr", "1", "--sha", "nothex!"])[0] == 1
    assert "error:" in capsys.readouterr().err


def test_deploy_applies_then_waits(monkeypatch):
    r = FakeRunner({"apply": "applied\n", "rollout": "rolled out\n"})
    code, out = run_cli(["deploy", "--pr", "3", "--sha", SHA, "--context", "k3d-x"], monkeypatch, r)
    assert code == 0
    assert r.calls[0][0][3:5] == ["apply", "-f"] and "kind: Namespace" in r.calls[0][1]
    assert "rollout" in r.calls[1][0]
    assert "preview: http://pr-3.preview.localtest.me/" in out


def test_teardown(monkeypatch):
    r = FakeRunner()
    assert run_cli(["teardown", "--pr", "3", "--branch", "x"], monkeypatch, r)[0] == 0
    assert r.calls[0][0][1:4] == ["delete", "namespace", "pr-3-x"]


def _ns_list():
    def item(name, pr, expires):
        return {
            "metadata": {
                "name": name,
                "labels": {LABEL_MANAGED_BY: "previewctl", LABEL_PR: str(pr)},
                "annotations": {ANN_EXPIRES: expires},
            }
        }

    return json.dumps({"items": [item("pr-1", 1, "2999-01-01T00:00:00Z"), item("pr-2", 2, "2999-01-01T00:00:00Z")]})


def test_gc_dry_run_deletes_nothing(monkeypatch):
    r = FakeRunner({"get namespaces": _ns_list()})
    code, out = run_cli(["gc", "--open-prs", "1", "--json"], monkeypatch, r)
    data = json.loads(out)
    assert code == 0 and data["apply"] is False
    assert {d["namespace"]: d["delete"] for d in data["decisions"]} == {"pr-1": False, "pr-2": True}
    assert not any("delete" in c for c, _ in r.calls)


def test_gc_apply_deletes_and_prunes_gitops_dir(monkeypatch, tmp_path):
    for name in ("pr-1", "pr-2"):
        cli.write_env(tmp_path, name, "x: 1\n")
    (tmp_path / "not-ours").mkdir()
    r = FakeRunner({"get namespaces": _ns_list()})
    code, out = run_cli(["gc", "--open-prs", "1", "--apply", "--out", str(tmp_path)], monkeypatch, r)
    assert code == 0 and "DELETE  pr-2" in out
    deletes = [c for c, _ in r.calls if "delete" in c]
    assert deletes == [["kubectl", "delete", "namespace", "pr-2", "--ignore-not-found", "--wait=false"]]
    assert (tmp_path / "pr-1").exists() and not (tmp_path / "pr-2").exists()


def test_prune_refuses_directories_it_did_not_write(tmp_path):
    (tmp_path / "pr-1").mkdir()
    assert cli.prune_env(tmp_path, "pr-1") is False
    assert cli.prune_env(tmp_path, "../outside") is False
    assert (tmp_path / "pr-1").exists()


def test_comment_body():
    code, out = run_cli(["comment", "--pr", "8", "--sha", SHA, "--branch", "b", "--status", "ready"])
    assert code == 0
    assert out.startswith("<!-- previewctl -->")
    assert "http://pr-8-b.preview.localtest.me/" in out and "`abcdef123456`" in out


class FakeResponse:
    def __init__(self, body, link=""):
        self.body = json.dumps(body).encode()
        self.headers = {"Link": link}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_open_pull_requests_paginates_and_sends_token():
    pages = [
        FakeResponse([{"number": 1}, {"number": 2}], '<https://api.github.com/x?page=2>; rel="next"'),
        FakeResponse([{"number": 7}]),
    ]
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return pages[len(seen) - 1]

    assert open_pull_requests("o/r", "t0k", opener=opener) == {1, 2, 7}
    assert seen[0].get_header("Authorization") == "Bearer t0k"
    assert seen[1].full_url == "https://api.github.com/x?page=2"


def test_open_pull_requests_rejects_bad_repo_and_foreign_links():
    with pytest.raises(ValueError):
        open_pull_requests("not a repo", "")

    def opener(req, timeout):
        return FakeResponse([], '<https://evil.example/steal>; rel="next"')

    with pytest.raises(ValueError, match="outside the GitHub API"):
        open_pull_requests("o/r", "t", opener=opener)


def test_gc_uses_github_when_repo_given(monkeypatch):
    monkeypatch.setattr(cli, "open_pull_requests", lambda repo: {2})
    r = FakeRunner({"get namespaces": _ns_list()})
    code, out = run_cli(["gc", "--repo", "o/r"], monkeypatch, r)
    assert code == 0 and "DELETE  pr-1" in out and "keep    pr-2" in out
