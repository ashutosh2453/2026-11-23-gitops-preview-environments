# GitOps Preview Environments

[![CI](https://github.com/ashutosh2453/2026-11-23-gitops-preview-environments/actions/workflows/ci.yml/badge.svg)](https://github.com/ashutosh2453/2026-11-23-gitops-preview-environments/actions/workflows/ci.yml)

`previewctl` gives every pull request its own isolated environment on Kubernetes. It renders the manifests deterministically, checks them against a policy gate before anything reaches the cluster, deploys them with a least-privilege identity provisioned by Terraform, and garbage-collects previews when the PR closes or goes quiet.

All of this is tested on a real cluster: CI creates a k3d (k3s) cluster with Traefik and k3s's network policy controller, and checks every claim below.

## How it works

```
pull request ──► previewctl render ──► policy gate ──► kubectl apply (as SA) ──► rollout ──► smoke test
                      │                    │
                      ▼                    └─ rejects cluster-scoped kinds, other namespaces,
          environments/<ns>/manifests.yaml    privileged pods, unpinned images, extra guardrails…
              (GitOps directory)

previewctl gc ──► list managed namespaces ──► plan (closed / superseded / expired / keep) ──► delete
```

Each PR gets a namespace such as `pr-42-feature-login`, which contains:

| Object | Purpose |
|---|---|
| Namespace | Pod Security `restricted` enforced. Labelled `managed-by=previewctl`, with the PR number, and annotated with the SHA, branch and expiry. |
| ResourceQuota | CPU, memory and pod budget. No LoadBalancers, NodePorts or PVCs. |
| LimitRange | Default requests and limits for any container. |
| NetworkPolicy ×2 | Default-deny ingress. Only the ingress controller's namespace and the same namespace may connect. |
| Deployment | Runs as non-root uid 10001 with a read-only root filesystem, drops ALL capabilities, uses seccomp `RuntimeDefault`, and has no service-account token. |
| Service + Ingress | `ClusterIP` behind the host `<namespace>.<domain>`. |

Every push re-renders with a fresh expiry, so active PRs never expire and abandoned ones are collected after `ttl_hours`.

## Verified in CI on a real cluster

`scripts/e2e.sh` runs on every push and pull request:

1. **Least privilege.** Terraform creates the `previewctl` service account and ClusterRole, and the previews are deployed *as that account*. It is confirmed unable to create ClusterRoleBindings, read `kube-system` secrets or exec into `kube-system` pods.
2. **Routing.** Two previews built from different commits run side by side, and `/version` through Traefik proves each host reaches its own build.
3. **Isolation.** A probe pod in the same namespace connects. A probe pod in the other preview is refused (`curl: (7) Failed to connect`).
4. **Policy gate.** A `ClusterRoleBinding` to `cluster-admin`, smuggled in as an extra manifest, is rejected with exit code 2, and the namespace is never created.
5. **Garbage collection.** Closing PR 102 deletes its preview and keeps PR 101. Expiring PR 101 then deletes it. `kube-system` and `preview-system` are untouched.

Other checks:
- kubeconform validates the rendered manifests against the Kubernetes 1.31 schemas.
- `terraform fmt` and `terraform validate`.
- ruff, and 66 unit tests on Python 3.10, 3.12 and 3.13.

One thing the e2e run surfaced: k3s's network policy controller programs a new pod's IP a moment *after* the pod starts. The first same-namespace connection is refused and the retry succeeds. The probe now retries, and the log shows the refusal followed by success.

## Quick start

```bash
pip install -e ".[dev]"
previewctl render --pr 42 --branch feature/login --sha "$(git rev-parse HEAD)"          # print YAML
previewctl render --pr 42 --branch feature/login --sha "$(git rev-parse HEAD)" --out environments
previewctl deploy --pr 42 --branch feature/login --sha "$(git rev-parse HEAD)" --context k3d-preview
previewctl gc --repo owner/name            # dry run: what would be deleted and why
previewctl gc --repo owner/name --apply    # delete; add --out environments to prune the GitOps dir
previewctl teardown --pr 42 --branch feature/login
```

To run the full end-to-end test locally, you need Docker, k3d, kubectl, Terraform and jq:

```bash
bash scripts/e2e.sh
```

Configuration lives in `preview.yaml` (see [`examples/preview.yaml`](examples/preview.yaml)):
- Unknown keys are rejected.
- Requests and limits are cross-checked against the quota.
- Variables that look like secrets (`*PASSWORD*`, `*TOKEN*`, …) are refused.

## Garbage collection rules

| Situation | Decision |
|---|---|
| Not labelled `managed-by=previewctl`, or already terminating | ignored |
| `previewctl/pinned=true` | keep (pinned) |
| PR label or expiry missing or unreadable | keep and report; never delete on bad metadata |
| PR closed (GitHub API, or `--open-prs`) | delete |
| Several namespaces for one PR (branch renamed) | delete all but the newest |
| Expiry passed | delete |

`gc` is a dry run unless you pass `--apply`. If the open-PR list can't be fetched completely, it fails rather than treating the missing PRs as closed.

## Pull request previews

`.github/workflows/preview.yml` builds each PR, deploys it to a k3d cluster inside the runner, smoke-tests it through the Ingress and keeps one sticky status comment up to date:
- The job that runs PR code has a read-only token, which is not saved in git config.
- A separate job with `pull-requests: write` posts the comment without checking out PR code.

Because that cluster is destroyed with the runner, the comment says **verified** and gives no link. For browsable previews, run the deploy step against a persistent cluster (self-hosted runner, or a kubeconfig secret) and schedule `previewctl gc --repo … --apply`.

## Layout

```
previewctl/   naming, config, render, policy, gc, kube (kubectl driver), github, cli
app/          stdlib demo service + non-root Dockerfile
infra/        Terraform: preview-system namespace, service account, ClusterRole + binding
scripts/      e2e.sh — the live-cluster test above
tests/        66 unit tests (no cluster needed)
```

## Limits

- Kubernetes RBAC can't restrict namespace *creation* by label. The namespace-scoped guardrails are enforced by the policy gate and by GC's `managed-by` filter, not by RBAC alone.
- Egress is left open, so previews can reach the internet and cluster DNS. Only ingress is isolated.
- No secrets support by design. Previews run unreviewed code, so secrets should come from a dedicated, low-privilege source if they're needed at all.

## License

MIT
