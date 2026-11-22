"""previewctl command line.

previewctl render   --pr 42 --sha <sha> --branch feat/x [--out environments]
previewctl deploy   --pr 42 --sha <sha> --branch feat/x [--context k3d-preview]
previewctl teardown --pr 42 --branch feat/x
previewctl gc       [--repo owner/name | --open-prs 1,2] [--apply] [--out environments]
previewctl comment  --pr 42 --sha <sha> --branch feat/x --status ready
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import ANN_EXPIRES, __version__
from .config import ConfigError, PreviewConfig, load
from .gc import plan
from .github import open_pull_requests
from .kube import Kubectl, KubectlError
from .naming import env_name, host_for
from .policy import check, check_extra
from .render import PreviewTarget, render, to_yaml

MANIFEST_FILE = "manifests.yaml"


def _target_args(p: argparse.ArgumentParser, *, sha: bool = True) -> None:
    p.add_argument("--pr", type=int, required=True, help="pull request number")
    p.add_argument("--branch", default="", help="head branch name (only used for a readable namespace)")
    if sha:
        p.add_argument("--sha", required=True, help="commit SHA; also the image tag")
    p.add_argument("--config", help="preview.yaml (defaults are used when omitted)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="previewctl", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"previewctl {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("render", help="render manifests and run policy checks")
    _target_args(p)
    p.add_argument("--extra", nargs="*", default=[], help="extra manifest files, policy-checked like the rest")
    p.add_argument("--out", help="GitOps directory: writes <out>/<namespace>/manifests.yaml")

    p = sub.add_parser("deploy", help="render, policy-check, apply and wait for rollout")
    _target_args(p)
    p.add_argument("--extra", nargs="*", default=[])
    p.add_argument("--context", help="kubeconfig context")
    p.add_argument("--timeout", type=int, default=180, help="rollout timeout in seconds")
    p.add_argument("--dry-run", action="store_true", help="server-side dry run, no changes")

    p = sub.add_parser("teardown", help="delete a pull request's preview namespace")
    _target_args(p, sha=False)
    p.add_argument("--context")
    p.add_argument("--wait", action="store_true")

    p = sub.add_parser("gc", help="delete previews of closed PRs and expired previews")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--repo", help="owner/name: read open PRs from GitHub (GITHUB_TOKEN if set)")
    src.add_argument("--open-prs", help="comma-separated open PR numbers (skips GitHub)")
    p.add_argument("--context")
    p.add_argument("--apply", action="store_true", help="actually delete; default is a dry run")
    p.add_argument("--out", help="GitOps directory to prune alongside the cluster")
    p.add_argument("--json", action="store_true", help="machine-readable plan")

    p = sub.add_parser("comment", help="Markdown body for the pull request status comment")
    _target_args(p)
    p.add_argument("--status", choices=["ready", "verified", "failed", "deleted"], required=True)
    p.add_argument("--details", default="", help="extra Markdown, e.g. smoke-test output")
    return ap


def _render_checked(args: argparse.Namespace, cfg: PreviewConfig) -> tuple[PreviewTarget, list[dict], str]:
    target = PreviewTarget(pr=args.pr, sha=args.sha, branch=args.branch)
    manifests = render(cfg, target)
    extras: list[dict] = []
    for extra in args.extra:
        docs = [d for d in yaml.safe_load_all(Path(extra).read_text(encoding="utf-8")) if d]
        for d in docs:
            if not isinstance(d, dict):
                raise ValueError(f"{extra}: every document must be a mapping")
            d.setdefault("metadata", {}).setdefault("namespace", target.namespace)
        extras += docs
    manifests += extras
    violations = check(manifests, target.namespace, cfg.domain) + check_extra(extras)
    if violations:
        raise PolicyFailure(violations)
    return target, manifests, to_yaml(manifests)


class PolicyFailure(Exception):
    def __init__(self, violations):
        super().__init__(f"{len(violations)} policy violation(s)")
        self.violations = violations


def write_env(out: Path, namespace: str, text: str) -> Path:
    path = out / namespace / MANIFEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # bytes: LF on every OS
    return path


def prune_env(out: Path, namespace: str) -> bool:
    target = (out / namespace).resolve()
    if target.parent != out.resolve() or not (target / MANIFEST_FILE).exists():
        return False  # only ever remove a directory previewctl wrote
    shutil.rmtree(target)
    return True


def cmd_render(args, cfg, out) -> int:
    target, _, text = _render_checked(args, cfg)
    if args.out:
        path = write_env(Path(args.out), target.namespace, text)
        print(f"wrote {path.as_posix()}", file=out)
    else:
        out.write(text)
    return 0


def cmd_deploy(args, cfg, out) -> int:
    target, _, text = _render_checked(args, cfg)
    kube = Kubectl(args.context)
    out.write(kube.apply(text, dry_run=args.dry_run))
    if not args.dry_run:
        out.write(kube.rollout_status(target.namespace, cfg.app, args.timeout))
    print(f"preview: http://{host_for(target.namespace, cfg.domain)}/", file=out)
    return 0


def cmd_teardown(args, cfg, out) -> int:
    ns = env_name(args.pr, args.branch)
    out.write(Kubectl(args.context).delete_namespace(ns, wait=args.wait))
    return 0


def cmd_gc(args, cfg, out, *, now: datetime | None = None) -> int:
    open_prs: set[int] | None = None
    if args.open_prs is not None:
        open_prs = {int(x) for x in args.open_prs.split(",") if x.strip()}
    elif args.repo:
        open_prs = open_pull_requests(args.repo)
    kube = Kubectl(args.context)
    decisions = plan(kube.list_preview_namespaces(), open_prs, now or datetime.now(timezone.utc))
    if args.json:
        rows = [d.__dict__ for d in decisions]
        print(json.dumps({"apply": args.apply, "decisions": rows}, indent=2), file=out)
    else:
        mode = "deleting" if args.apply else "dry run"
        print(f"{len(decisions)} preview namespace(s), {mode}:", file=out)
        for d in decisions:
            print(f"  {'DELETE' if d.delete else 'keep  '}  {d.namespace:<45} {d.reason}", file=out)
    if args.apply:
        for d in decisions:
            if d.delete:
                kube.delete_namespace(d.namespace)
                if args.out:
                    prune_env(Path(args.out), d.namespace)
    return 0


def cmd_comment(args, cfg, out) -> int:
    target = PreviewTarget(pr=args.pr, sha=args.sha, branch=args.branch)
    ns = target.namespace
    expires = render(cfg, target)[0]["metadata"]["annotations"][ANN_EXPIRES]
    icon = {"ready": "✅", "verified": "✅", "failed": "❌", "deleted": "🧹"}[args.status]
    host = host_for(ns, cfg.domain)
    lines = [
        "<!-- previewctl -->",
        f"### {icon} Preview environment: {args.status}",
        "",
        "| | |",
        "|---|---|",
        f"| Namespace | `{ns}` |",
    ]
    if args.status == "ready":
        # Only a persistent cluster has a URL reviewers can open.
        lines += [f"| URL | http://{host}/ |", f"| Expires | {expires} (refreshed on every push) |"]
    else:
        lines += [f"| Host | `{host}` |"]
    lines += [f"| Commit | `{target.sha[:12]}` |"]
    if args.status == "verified":
        lines += [
            "",
            "Deployed and smoke-tested through the Ingress on an ephemeral k3d cluster in CI. "
            "The cluster is deleted when the job ends, so there is no link to open.",
        ]
    if args.details:
        lines += ["", args.details]
    print("\n".join(lines), file=out)
    return 0


COMMANDS = {
    "render": cmd_render,
    "deploy": cmd_deploy,
    "teardown": cmd_teardown,
    "gc": cmd_gc,
    "comment": cmd_comment,
}


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        cfg = load(getattr(args, "config", None))
        return COMMANDS[args.command](args, cfg, out)
    except PolicyFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        for v in exc.violations:
            print(f"  - {v}", file=sys.stderr)
        return 2
    except (ConfigError, ValueError, KubectlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
