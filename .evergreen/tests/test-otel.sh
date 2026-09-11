#!/usr/bin/env bash

# Test the OTel span-export orchestration configuration (DRIVERS-3454).
set -eu

SCRIPT_DIR=$(dirname ${BASH_SOURCE[0]})
. $SCRIPT_DIR/../handle-paths.sh
. $SCRIPT_DIR/../ensure-uv.sh

pushd $SCRIPT_DIR/.. > /dev/null

ensure_uv || exit 1

# Unit tests for the injection and gating helpers.
pushd orchestration > /dev/null
uv run python -m unittest test_drivers_orchestration -v
popd > /dev/null

bash install-cli.sh "$(pwd)/orchestration"

# Fail-fast checks: incompatible combinations must error before any download,
# with the expected message -- a bare non-zero exit could also be an
# unrelated crash (e.g. an ImportError) masquerading as a working gate.
assert_gate_rejects() {
  local expected="$1"
  shift
  local output
  if output=$("$@" 2>&1); then
    echo "ERROR: '$*' should have failed"
    exit 1
  fi
  if ! echo "$output" | grep -q "$expected"; then
    echo "ERROR: '$*' failed for the wrong reason (expected '$expected'):"
    echo "$output"
    exit 1
  fi
}

assert_gate_rejects "requires MongoDB 9.0" \
  env OTEL=1 ./orchestration/drivers-orchestration run --version 8.0
assert_gate_rejects "local-atlas" \
  env OTEL=1 ./orchestration/drivers-orchestration run --version latest --local-atlas
assert_gate_rejects "DOCKER_RUNNING" \
  env OTEL=1 DOCKER_RUNNING=true ./orchestration/drivers-orchestration run --version latest

# --existing-binaries-dir bypasses version selection, so the gate probes the
# actual mongod binary: a real 8.0 binary must be rejected before any
# download or deployment.
EXISTING_BIN_DIR=mongodl_otel_test
rm -rf ${EXISTING_BIN_DIR}
uv run python mongodl.py --edition enterprise --version 8.0 --component archive --out ${EXISTING_BIN_DIR} --strip-path-components 2 --cache-dir "${DRIVERS_TOOLS}/.local/cache" --retries 5
assert_gate_rejects "existing-binaries-dir contains" \
  env OTEL=1 ./orchestration/drivers-orchestration run --existing-binaries-dir=${EXISTING_BIN_DIR}
rm -rf ${EXISTING_BIN_DIR}

# Live run: the OTel parameters must be applied and the trace dir exported.
OTEL=1 ./orchestration/drivers-orchestration run --version latest

grep -q '^OTEL_TRACE_DIR=' mo-expansion.sh
# shellcheck disable=SC1091
. ./mo-expansion.sh
test -d "${OTEL_TRACE_DIR}/27017"

$MONGODB_BINARIES/mongosh "mongodb://localhost:27017/?directConnection=true" --eval '
  const p = db.adminCommand({
    getParameter: 1,
    opentelemetryTraceDirectory: 1,
    openTelemetryExternalTracing: 1,
    openTelemetryTracingSampling: 1,
    openTelemetryTracingFileFlushCount: 1,
  });
  if (!p.opentelemetryTraceDirectory.endsWith("27017") ||
      p.openTelemetryExternalTracing.tokenBucketRateLimit.maxTokens !== 1000 ||
      p.openTelemetryTracingSampling.defaultSampling.samplingFactor !== 1.0 ||
      p.openTelemetryTracingFileFlushCount !== 1) {
    throw new Error("unexpected OTel parameters: " + JSON.stringify(p));
  }
  print("OTEL_PARAMS_OK");
' | grep -q OTEL_PARAMS_OK

./orchestration/drivers-orchestration stop

# Positive probe path: a 9.0+ --existing-binaries-dir (copied from the latest
# binaries the previous run downloaded) passes the gate and the cluster comes
# up with the OTel parameters applied.
EXISTING_BIN_LATEST=otel_existing_bin_test
rm -rf ${EXISTING_BIN_LATEST}
# The previous run already downloaded the latest archive into this cache
# dir, so this is a re-extract, not a second download.
uv run python mongodl.py --edition enterprise --version latest --component archive --out ${EXISTING_BIN_LATEST} --strip-path-components 2 --cache-dir "${DRIVERS_TOOLS}/.local/cache" --retries 5
# --version 8.0 is deliberate: with --existing-binaries-dir the probed
# binary is authoritative and a stale requested version must not veto a
# compatible 9.0+ build (--skip-crypt-shared avoids downloading the only
# 8.0 artifact the version would otherwise select).
OTEL=1 ./orchestration/drivers-orchestration run --existing-binaries-dir=${EXISTING_BIN_LATEST} --version 8.0 --skip-crypt-shared
$MONGODB_BINARIES/mongosh "mongodb://localhost:27017/?directConnection=true" --eval '
  const p = db.adminCommand({getParameter: 1, opentelemetryTraceDirectory: 1});
  if (!p.opentelemetryTraceDirectory.endsWith("27017")) {
    throw new Error("unexpected OTel parameters via existing binaries: " + JSON.stringify(p));
  }
  print("OTEL_EXISTING_BIN_PARAMS_OK");
' | grep -q OTEL_EXISTING_BIN_PARAMS_OK
./orchestration/drivers-orchestration stop
rm -rf ${EXISTING_BIN_LATEST}

# Same flow through the preferred mongodb-runner entry point (run-mongodb.sh):
# the runner translates procParams.setParameter into --setParameter args, so
# the injected OTel parameters must be applied there too.
OTEL=1 MONGODB_VERSION=latest bash ./run-mongodb.sh start
# run() silently falls back to mongo-orchestration when mongodb-runner is
# unsupported on the host, which would make the assertions below meaningless
# for this leg. Only the runner path writes out.log as JSON-serialized
# cluster info; mongo-orchestration writes plain daemon log text.
if ! uv run python -c "import json; json.load(open('orchestration/out.log'))" 2>/dev/null; then
  echo "ERROR: mongodb-runner path fell back to mongo-orchestration"
  exit 1
fi
# shellcheck disable=SC1091
. ./mo-expansion.sh
test -n "${OTEL_TRACE_DIR}"
test -d "${OTEL_TRACE_DIR}/27017"
$MONGODB_BINARIES/mongosh "mongodb://localhost:27017/?directConnection=true" --eval '
  const p = db.adminCommand({
    getParameter: 1,
    opentelemetryTraceDirectory: 1,
    openTelemetryExternalTracing: 1,
    openTelemetryTracingFileFlushCount: 1,
  });
  if (!p.opentelemetryTraceDirectory.endsWith("27017") ||
      p.openTelemetryExternalTracing.tokenBucketRateLimit.maxTokens !== 1000 ||
      p.openTelemetryTracingFileFlushCount !== 1) {
    throw new Error("unexpected OTel parameters via mongodb-runner: " + JSON.stringify(p));
  }
  print("OTEL_RUNNER_PARAMS_OK");
' | grep -q OTEL_RUNNER_PARAMS_OK
bash ./run-mongodb.sh stop

# Sharded topology: mongos must accept the injected setParameters too
# (router entries hold proc params directly, without a procParams wrapper).
OTEL=1 ./orchestration/drivers-orchestration run --version latest --topology sharded_cluster
# shellcheck disable=SC1091
. ./mo-expansion.sh
# Per-port directories for a shard member (27217) and the mongos (27017).
test -d "${OTEL_TRACE_DIR}/27217"
test -d "${OTEL_TRACE_DIR}/27017"
# getParameter against the mongos itself: the router's own parameters.
$MONGODB_BINARIES/mongosh "mongodb://localhost:27017" --eval '
  const p = db.adminCommand({
    getParameter: 1,
    opentelemetryTraceDirectory: 1,
    openTelemetryExternalTracing: 1,
  });
  if (!p.opentelemetryTraceDirectory.endsWith("27017") ||
      p.openTelemetryExternalTracing.tokenBucketRateLimit.maxTokens !== 1000) {
    throw new Error("unexpected OTel parameters on mongos: " + JSON.stringify(p));
  }
  print("OTEL_MONGOS_PARAMS_OK");
' | grep -q OTEL_MONGOS_PARAMS_OK
./orchestration/drivers-orchestration stop

# Opt-in regression: without OTEL, no trace dir and no expansion entry.
./orchestration/drivers-orchestration run --version latest
if ! grep -q '^OTEL_TRACE_DIR=""$' mo-expansion.sh; then
  echo "ERROR: OTEL_TRACE_DIR should be exported as empty without OTEL=1 (to clear stale values)"
  exit 1
fi
if [ -d "${DRIVERS_TOOLS}/otel" ]; then
  echo "ERROR: otel directory created without OTEL=1"
  exit 1
fi
./orchestration/drivers-orchestration stop

popd > /dev/null
# Overwrite the placeholder FAIL result seeded by setup.sh with a PASS entry.
make -C ${DRIVERS_TOOLS} test
echo "OTel orchestration test... done."
