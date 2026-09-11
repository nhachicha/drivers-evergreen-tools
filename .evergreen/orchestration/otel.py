"""OpenTelemetry file-exporter configuration for trace-context prose tests
(DRIVERS-3454). Requires MongoDB 9.0+. See README.md in this directory.
"""

from __future__ import annotations

import os
import re
import subprocess
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
    binaries_dir = getattr(opts, "existing_binaries_dir", None)
    if binaries_dir:
        # --existing-binaries-dir bypasses version selection entirely (the
        # requested version is not what runs), so the probed binary is the
        # single source of truth and the version-string gate below does not
        # apply. This is the primary path for OTel-enabled custom builds
        # (see requires_otel_build in README.md).
        _check_existing_binaries_version(binaries_dir)
    else:
        below_90 = _numeric_below_90(opts.version)
        if below_90:
            # Optional "v" prefix: mongodl aliases like v8.0-perf resolve to
            # servers below 9.0 and must be caught here rather than failing
            # at startup.
            raise ValueError(
                f"--otel requires MongoDB 9.0+ (OTel setParameters do not "
                f"exist on {opts.version})"
            )
        if below_90 is None and opts.version not in ("latest", "latest-build"):
            # Non-numeric aliases ("rapid", "latest-release",
            # "latest-stable", ...) are resolved through mongodl's release
            # catalog -- the same mechanism the download uses -- and the
            # resolved version is gated, so an alias starts passing
            # automatically once it resolves to 9.0+. Master nightlies
            # (latest/latest-build) do not resolve via the catalog and are
            # always 9.0+.
            resolved = _resolve_published_version(
                opts.version, getattr(opts, "arch", None)
            )
            if _numeric_below_90(resolved) is not False:
                raise ValueError(
                    f"--otel requires MongoDB 9.0+, but version "
                    f"{opts.version!r} resolves to {resolved}"
                )
    if os.environ.get("DOCKER_RUNNING"):
        raise ValueError(
            "--otel is not supported with DOCKER_RUNNING: the container "
            "filesystem is not readable by the host test process"
        )
    if opts.local_atlas:
        raise ValueError("--otel is not supported with --local-atlas")


def _numeric_below_90(version):
    """True/False when version parses as [v]major[.minor]; None otherwise."""
    match = re.match(r"^v?(\d+)(?:\.(\d+))?", version)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2) or 0)) < (9, 0)


def _resolve_published_version(version, arch=None):
    """Resolve a version alias to a concrete version via mongodl's catalog.

    Filters by the same target/arch/edition/component the subsequent
    download uses, so the gate judges the artifact that will actually be
    downloaded (releases can be published for platforms at different times).
    Raises ValueError when the alias cannot be resolved (unknown alias, no
    catalog entry, or the release list is unreachable) so the gate stays
    fail-fast rather than deferring to a server startup failure.
    """
    evg_dir = Path(__file__).absolute().parent.parent
    sys.path.insert(0, str(evg_dir))
    try:
        # Deferred: mongodl lives in .evergreen, only on sys.path here.
        from mongodl import Cache, infer_arch, infer_target

        cache = Cache.open_in(evg_dir.parent / ".local" / "cache")
        cache.refresh_full_json()
        component = next(
            iter(
                cache.db.iter_available(
                    version=version,
                    target=infer_target(version),
                    arch=arch or infer_arch(),
                    edition="enterprise",
                    component="archive",
                )
            ),
            None,
        )
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            f"--otel could not resolve version {version!r} from the release "
            f"list: {e}"
        ) from e
    finally:
        sys.path.remove(str(evg_dir))
    if component is None:
        raise ValueError(
            f"--otel could not resolve version {version!r}: no published "
            f"release matches it for this platform"
        )
    return component.version


def _check_existing_binaries_version(binaries_dir):
    """Raise ValueError unless the mongod in binaries_dir reports 9.0+."""
    ext = ".exe" if PLATFORM == "win32" else ""
    mongod = Path(binaries_dir) / f"mongod{ext}"
    try:
        output = subprocess.check_output(
            [str(mongod), "--version"], encoding="utf-8", stderr=subprocess.STDOUT
        )
    except (OSError, subprocess.CalledProcessError) as e:
        raise ValueError(
            f"--otel could not determine the server version from "
            f"{mongod} --version: {e}"
        ) from e
    match = re.search(r"db version v(\d+)\.(\d+)", output)
    if match is None:
        raise ValueError(
            f"--otel could not parse the server version from "
            f"{mongod} --version output: {output.splitlines()[:1]}"
        )
    if (int(match.group(1)), int(match.group(2))) < (9, 0):
        raise ValueError(
            f"--otel requires MongoDB 9.0+, but --existing-binaries-dir "
            f"contains {match.group(0)}"
        )
