#!/usr/bin/env bash
# End-to-end test on a real k3d cluster (k3s + Traefik + its network policy controller).
#
# Proves, on a live cluster:
#   1. Terraform provisions the least-privilege previewctl identity, and that identity
#      can deploy a preview but cannot touch cluster-wide RBAC or kube-system secrets.
#   2. Two previews deploy side by side and the Ingress routes each host to its own build.
#   3. NetworkPolicy blocks traffic from another preview but allows same-namespace traffic.
#   4. The policy gate rejects a dangerous manifest before it reaches the cluster.
#   5. gc deletes the preview of a closed PR, then an expired one, and keeps the rest.
#
# Requires: docker, k3d, kubectl, terraform, jq, and previewctl installed.
set -euo pipefail

CLUSTER=${CLUSTER:-preview}
CTX="k3d-${CLUSTER}"
PORT=${PORT:-8081}
DOMAIN=preview.localtest.me
SHA1=${SHA1:-$(git rev-parse HEAD)}
SHA2=$(printf 'second-build' | sha1sum | cut -c1-40)
NS1=pr-101-e2e-one
NS2=pr-102-e2e-two
ROOT=$(cd "$(dirname "$0")/.." && pwd)

step() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "create k3d cluster"
k3d cluster create "$CLUSTER" --agents 0 -p "${PORT}:80@loadbalancer" --wait --timeout 180s
kubectl config use-context "$CTX"

step "build app image with two tags and import into the cluster"
docker build -q -t "preview-app:${SHA1}" "$ROOT/app"
docker tag "preview-app:${SHA1}" "preview-app:${SHA2}"
k3d image import -c "$CLUSTER" "preview-app:${SHA1}" "preview-app:${SHA2}"

step "terraform apply (system namespace, service account, RBAC)"
terraform -chdir="$ROOT/infra/terraform" init -input=false -no-color >/dev/null
terraform -chdir="$ROOT/infra/terraform" apply -auto-approve -input=false -no-color -var "kube_context=${CTX}"

step "kubeconfig context for the previewctl service account"
TOKEN=$(kubectl -n preview-system create token previewctl --duration=1h)
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::add-mask::${TOKEN}"
kubectl config set-credentials previewctl-sa --token="$TOKEN" >/dev/null
kubectl config set-context previewctl --cluster="$CTX" --user=previewctl-sa >/dev/null
unset TOKEN

step "least privilege: what the identity must NOT be able to do"
for check in "create clusterrolebindings" "get secrets -n kube-system" "create pods/exec -n kube-system"; do
  # shellcheck disable=SC2086
  answer=$(kubectl --context previewctl auth can-i $check || true)
  [[ "$answer" == "no" ]] || fail "service account can: $check"
  echo "can-i $check -> no"
done

step "wait for Traefik ingress controller"
for _ in $(seq 1 60); do
  kubectl -n kube-system get deploy traefik >/dev/null 2>&1 && break
  sleep 3
done
kubectl -n kube-system rollout status deploy/traefik --timeout=180s

step "deploy two previews as the service account"
previewctl deploy --context previewctl --pr 101 --branch e2e/one --sha "$SHA1"
previewctl deploy --context previewctl --pr 102 --branch e2e/two --sha "$SHA2"

step "ingress routes each host to its own build"
check_host() {
  local ns=$1 pr=$2 sha=$3 body=""
  for _ in $(seq 1 30); do
    body=$(curl -fsS -m 5 -H "Host: ${ns}.${DOMAIN}" "http://127.0.0.1:${PORT}/version" || true)
    [[ -n "$body" ]] && break
    sleep 2
  done
  echo "$ns -> $body"
  jq -e --arg pr "$pr" --arg sha "$sha" '.pr == $pr and .sha == $sha' <<<"$body" >/dev/null \
    || fail "$ns did not serve PR $pr at $sha"
}
check_host "$NS1" 101 "$SHA1"
check_host "$NS2" 102 "$SHA2"

step "network policy: other previews are blocked, same namespace is allowed"
# probe <namespace> <url> -> prints Succeeded or Failed. curl retries because the network
# policy controller programs a new pod's IP a moment after the pod starts.
probe() {
  local ns=$1 url=$2 name="probe-$RANDOM"
  local overrides
  overrides=$(jq -cn --arg url "$url" '{spec: {
    securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: "RuntimeDefault"}},
    containers: [{name: "probe", image: "curlimages/curl:8.11.1",
      command: ["curl", "-fsS", "-m", "4", "--retry", "6", "--retry-delay", "2", "--retry-all-errors", $url],
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}}]}}')
  kubectl -n "$ns" run "$name" --restart=Never --image=curlimages/curl:8.11.1 --overrides="$overrides" >/dev/null
  for _ in $(seq 1 90); do
    phase=$(kubectl -n "$ns" get pod "$name" -o jsonpath='{.status.phase}')
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    sleep 2
  done
  kubectl -n "$ns" logs "$name" 2>&1 | tail -3 | sed "s/^/    [$ns] /" >&2
  kubectl -n "$ns" delete pod "$name" --wait=false >/dev/null
  echo "$phase"
}
target="http://web.${NS1}.svc.cluster.local/healthz"
same=$(probe "$NS1" "$target")
cross=$(probe "$NS2" "$target")
echo "same-namespace: $same, cross-namespace: $cross"
[[ "$same" == "Succeeded" ]] || fail "same-namespace traffic was blocked"
[[ "$cross" == "Failed" ]] || fail "cross-namespace traffic was NOT blocked"

step "policy gate rejects a dangerous manifest before it reaches the cluster"
cat >/tmp/evil.yaml <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: pwn}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: cluster-admin}
subjects: [{kind: ServiceAccount, name: default, namespace: pr-103}]
EOF
set +e
previewctl deploy --context previewctl --pr 103 --sha "$SHA1" --extra /tmp/evil.yaml
code=$?
set -e
[[ $code -eq 2 ]] || fail "expected policy failure (exit 2), got $code"
kubectl get ns pr-103 >/dev/null 2>&1 && fail "namespace pr-103 should not exist"
echo "rejected with exit 2, nothing applied"

step "gc: PR 102 closed -> its preview is deleted, PR 101 is kept"
previewctl gc --context previewctl --open-prs 101 --apply
kubectl wait --for=delete "namespace/${NS2}" --timeout=120s
kubectl get ns "$NS1" >/dev/null || fail "$NS1 should have been kept"

step "gc: PR 101 expired -> deleted"
kubectl annotate ns "$NS1" previewctl/expires-at=2000-01-01T00:00:00Z --overwrite
previewctl gc --context previewctl --open-prs 101 --json --apply | tee /tmp/gc.json
jq -e '.decisions[] | select(.namespace == "'"$NS1"'") | .reason == "expired" and .delete' /tmp/gc.json >/dev/null
kubectl wait --for=delete "namespace/${NS1}" --timeout=120s
kubectl get ns kube-system preview-system >/dev/null || fail "gc touched a namespace it does not own"

step "all end-to-end checks passed"
