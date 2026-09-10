"""OpenTelemetry file-exporter configuration for trace-context prose tests
(DRIVERS-3454). Requires MongoDB 9.0+. See README.md in this directory.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PLATFORM = sys.platform.lower()

# samplingFactor 1.0 samples every span (production default is ~0.000045,
# which would make span assertions flake). Per the server IDL
# (src/mongo/otel/traces/trace_sampling_parameters.idl), every sampling
# strategy -- including defaultSampling -- also carries its own
# tokenBucketRateLimit (default refillRate 1/s, maxTokens 10) that throttles
# spans independently of samplingFactor, so it must be raised here too or
# internally-initiated spans still get capped at a 10-burst.
OTEL_SAMPLING_JSON = (
    '{"defaultSampling":{"samplingFactor":1.0,'
    '"tokenBucketRateLimit":{"refillRate":1000.0,"maxTokens":1000}}}'
)
# Externally-propagated contexts (driver traceparents) bypass the probability
# sampler entirely and go through the separate openTelemetryExternalTracing
# setParameter -- NOT nested under openTelemetryTracingSampling -- which
# needs the same raise.
OTEL_EXTERNAL_TRACING_JSON = (
    '{"tokenBucketRateLimit":{"refillRate":1000.0,"maxTokens":1000}}'
)
OTEL_DIR_NAME = "otel"


def normalize_path(path: Path | str) -> str:
    if PLATFORM != "win32":
        return str(path)
    path = Path(path).as_posix()
    return re.sub("/cygdrive/(.*?)(/)", r"\1://", path, count=1)


def handle_otel_config(data, otel_root):
    """Configure every mongod/mongos to export OTel spans as NDJSON files.

    Each member gets its own trace directory (keyed by port) under otel_root
    so files from different members never interleave.
    """
    members = []

    def traverse(root):
        if isinstance(root, list):
            [traverse(i) for i in root]
            return
        if "ipv6" in root:
            members.append(root)
            return
        for value in root.values():
            if isinstance(value, (dict, list)):
                traverse(value)

    traverse(data)

    for member in members:
        if "port" not in member:
            raise ValueError(
                "--otel requires an explicit port for every cluster member "
                "so each gets its own trace directory"
            )
        member_dir = Path(otel_root) / str(member["port"])
        os.makedirs(member_dir, exist_ok=True)
        set_param = member.setdefault("setParameter", {})
        # Pre-existing OTel settings (e.g. opentelemetryHttpEndpoint, or a
        # tracing compression the file exporter rejects) can conflict with
        # the parameters injected below and would only fail at server
        # startup, after the download. The tracing feature flags are also
        # rejected: featureFlagTracing=false would start fine yet silently
        # export no spans (the server requires it alongside
        # featureFlagOtelTraceSampling), and a pre-set
        # featureFlagOtelTraceSampling would be silently overwritten below.
        # Fail fast instead.
        conflicts = [
            k
            for k in set_param
            if k.lower().startswith("opentelemetry")
            or k in ("featureFlagTracing", "featureFlagOtelTraceSampling")
        ]
        if conflicts:
            raise ValueError(
                f"--otel conflicts with OpenTelemetry setParameters already "
                f"present in the orchestration config: {conflicts}"
            )
        set_param["opentelemetryTraceDirectory"] = normalize_path(member_dir)
        set_param["featureFlagOtelTraceSampling"] = "true"
        set_param["openTelemetryTracingSampling"] = OTEL_SAMPLING_JSON
        set_param["openTelemetryExternalTracing"] = OTEL_EXTERNAL_TRACING_JSON
        # The file exporter buffers 256 export batches (flushed every 30s) by
        # default, on top of the 1s batch processor; tests emit few spans, so
        # flush every batch to disk like the server's own file-export tests.
        set_param["openTelemetryTracingFileFlushCount"] = 1


def validate_otel_opts(opts):
    """Fail fast on option combinations incompatible with --otel.

    The OTel file exporter requires MongoDB 9.0+ and a cluster that shares
    the host filesystem with the test process (the only way to read spans).
    """
    if not getattr(opts, "otel", False):
        return
    # Optional "v" prefix: mongodl aliases like v8.0-perf resolve to servers
    # below 9.0 and must be caught here rather than failing at startup.
    match = re.match(r"^v?(\d+)(?:\.(\d+))?", opts.version)
    if match:
        if (int(match.group(1)), int(match.group(2) or 0)) < (9, 0):
            raise ValueError(
                f"--otel requires MongoDB 9.0+ (OTel setParameters do not "
                f"exist on {opts.version})"
            )
    # Non-numeric aliases are default-closed: only master nightlies are
    # guaranteed to be 9.0+. Aliases like "rapid", "latest-release", and
    # "latest-stable" resolve to the newest *published* release, which is
    # still 8.x while 9.0 is unpublished (see UNPUBLISHED_VERSIONS in
    # drivers_orchestration.py). Add an alias here once every version it can
    # resolve to is 9.0+.
    elif opts.version not in ("latest", "latest-build"):
        raise ValueError(
            f"--otel requires MongoDB 9.0+, which cannot be guaranteed for "
            f"version {opts.version!r}; use 'latest' or an explicit 9.x+ "
            f"version"
        )
    if os.environ.get("DOCKER_RUNNING"):
        raise ValueError(
            "--otel is not supported with DOCKER_RUNNING: the container "
            "filesystem is not readable by the host test process"
        )
    if opts.local_atlas:
        raise ValueError("--otel is not supported with --local-atlas")
