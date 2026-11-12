#!/usr/bin/env bash
# End-to-end demo on a local kind cluster: build, deploy, then walk one VM through its whole life.
# Needs docker, kind and kubectl on PATH. Re-runnable; KEEP_CLUSTER=0 deletes the cluster at the end.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=${CLUSTER:-vmlease}
VM=e2e-vm

step() { printf '\n==> %s\n' "$*"; }
fail() { echo "FAIL: $*"; kubectl -n vmlease-system logs deploy/vmlease-operator --tail=40 || true; exit 1; }
wait_for() {  # wait_for <description> <seconds> <command...>
  local what=$1 seconds=$2; shift 2
  for ((i = 0; i < seconds; i++)); do
    if "$@" >/dev/null 2>&1; then echo "   ok: $what (${i}s)"; return 0; fi
    sleep 1
  done
  fail "timed out waiting for: $what"
}
field() { kubectl get osvm "$1" -o jsonpath="{.status.$2}"; }
phase_is() { [ "$(field "$1" phase)" = "$2" ]; }
# Talk to fakestack's /_fake endpoints from inside its pod (plays "another admin").
fake() {
  kubectl -n vmlease-system exec deploy/fakestack -- python -c \
    "import sys, urllib.request as u; print(u.urlopen(u.Request('http://localhost:5000' + sys.argv[2], method=sys.argv[1])).read().decode())" \
    "$1" "$2"
}

step "kind cluster '$CLUSTER'"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-$CLUSTER" >/dev/null

step "build images and load them into kind"
docker build -q -t vmlease-operator:dev .
docker build -q -f fakestack/Dockerfile -t fakestack:dev .
kind load docker-image vmlease-operator:dev fakestack:dev --name "$CLUSTER"

step "deploy CRD, fake OpenStack and operator"
kubectl apply -f deploy/crd.yaml -f deploy/operator.yaml -f deploy/fakestack.yaml
# Restart to pick up the fresh :dev images. Cloud first, then the operator, so the operator
# never logs in to a fakestack pod that is about to disappear.
kubectl -n vmlease-system rollout restart deploy/fakestack
kubectl -n vmlease-system rollout status deploy/fakestack --timeout=120s
kubectl -n vmlease-system rollout restart deploy/vmlease-operator
kubectl -n vmlease-system rollout status deploy/vmlease-operator --timeout=120s
kubectl delete osvm --all --wait=true >/dev/null 2>&1 || true

step "1. create a VM with a 90 second lease"
kubectl apply -f - <<EOF
apiVersion: vmlease.io/v1alpha1
kind: OpenStackVM
metadata: {name: $VM, namespace: default}
spec: {flavor: m1.small, image: cirros, network: private, ttl: 90s}
EOF
wait_for "phase Building" 30 phase_is $VM Building
wait_for "phase Active" 60 phase_is $VM Active
kubectl get osvm -o wide
FIRST=$(field $VM serverId)

step "2. CRD validation rejects bad input"
if kubectl patch osvm $VM --type merge -p '{"spec":{"flavor":"m1.medium"}}' 2>/tmp/vmlease-err; then
  fail "flavor change should have been rejected"
fi
echo "   ok: $(grep -o 'flavor, image and network are immutable[^"]*' /tmp/vmlease-err | head -1)"
if kubectl apply -f - 2>/dev/null <<EOF
apiVersion: vmlease.io/v1alpha1
kind: OpenStackVM
metadata: {name: bad-ttl, namespace: default}
spec: {flavor: m1.small, image: cirros, ttl: forever}
EOF
then fail "ttl 'forever' should have been rejected"; fi
echo "   ok: ttl 'forever' rejected by the schema pattern"

step "3. someone deletes the VM directly in OpenStack: the operator heals it"
fake DELETE "/_fake/servers/$FIRST" >/dev/null
wait_for "new server replaces $FIRST" 30 bash -c "[ -n \"\$(kubectl get osvm $VM -o jsonpath='{.status.serverId}')\" ] && [ \"\$(kubectl get osvm $VM -o jsonpath='{.status.serverId}')\" != '$FIRST' ]"
wait_for "phase Active again" 60 phase_is $VM Active
kubectl get events --field-selector "involvedObject.uid=$(kubectl get osvm $VM -o jsonpath='{.metadata.uid}')" \
  --sort-by=.lastTimestamp -o custom-columns=TYPE:.type,REASON:.reason,MESSAGE:.message

step "4. lease runs out: VM is deleted, object stays as a record"
wait_for "phase Expired" 120 phase_is $VM Expired
kubectl get osvm $VM
[ "$(fake GET /_fake/servers)" = "[]" ] || fail "server still exists in OpenStack after expiry"
echo "   ok: no servers left in OpenStack"

step "5. kubectl delete waits for OpenStack cleanup (finalizer)"
kubectl apply -f - <<EOF
apiVersion: vmlease.io/v1alpha1
kind: OpenStackVM
metadata: {name: $VM-2, namespace: default}
spec: {flavor: m1.tiny, image: cirros, ttl: 1h}
EOF
wait_for "second VM Active" 60 phase_is $VM-2 Active
kubectl get osvm $VM-2 -o jsonpath='{.metadata.finalizers}{"\n"}'
kubectl delete osvm $VM-2 --timeout=60s
[ "$(fake GET /_fake/servers)" = "[]" ] || fail "server not deleted with its object"
echo "   ok: server deleted before the object went away"
kubectl delete osvm $VM

step "6. Prometheus metrics"
kubectl -n vmlease-system exec deploy/vmlease-operator -- python -c \
  "import urllib.request as u; print(u.urlopen('http://localhost:9100/metrics').read().decode())" \
  | grep -E '^vmlease_(events_total|reconcile_total|vms)' | grep -v ' 0.0$'

[ "${KEEP_CLUSTER:-1}" = "0" ] && kind delete cluster --name "$CLUSTER"
printf '\nE2E PASSED\n'
