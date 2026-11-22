"""Thin kubectl driver.

Shelling out to kubectl (instead of a client library) keeps the tool dependency-free
and means it uses exactly the user's kubeconfig, context and auth plugins. The runner
is injectable so the command lines can be tested without a cluster.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from . import LABEL_MANAGED_BY, MANAGED_BY

Runner = Callable[..., subprocess.CompletedProcess]


class KubectlError(RuntimeError):
    pass


class Kubectl:
    def __init__(self, context: str | None = None, *, binary: str = "kubectl", runner: Runner = subprocess.run):
        self.context = context
        self.binary = binary
        self.runner = runner

    def _cmd(self, args: Sequence[str]) -> list[str]:
        cmd = [self.binary]
        if self.context:
            cmd += ["--context", self.context]
        return cmd + list(args)

    def run(self, args: Sequence[str], *, stdin: str | None = None, check: bool = True) -> str:
        cmd = self._cmd(args)
        proc = self.runner(cmd, input=stdin, capture_output=True, text=True, check=False)
        if check and proc.returncode != 0:
            raise KubectlError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def apply(self, manifest_yaml: str, *, dry_run: bool = False) -> str:
        args = ["apply", "-f", "-"]
        if dry_run:
            args.append("--dry-run=server")
        return self.run(args, stdin=manifest_yaml)

    def rollout_status(self, namespace: str, deployment: str, timeout_s: int = 120) -> str:
        return self.run(["-n", namespace, "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout_s}s"])

    def list_preview_namespaces(self) -> list[dict[str, Any]]:
        out = self.run(["get", "namespaces", "-l", f"{LABEL_MANAGED_BY}={MANAGED_BY}", "-o", "json"])
        return json.loads(out or "{}").get("items", [])

    def delete_namespace(self, name: str, *, wait: bool = False) -> str:
        return self.run(["delete", "namespace", name, "--ignore-not-found", f"--wait={'true' if wait else 'false'}"])
