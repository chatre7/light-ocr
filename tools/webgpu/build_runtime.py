#!/usr/bin/env python3
"""Build and validate the locked Linux x64 ONNX Runtime WebGPU runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = Path(__file__).with_name("runtime-lock.json")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SONAME_RE = re.compile(r"\(SONAME\).*\[([^]]+)]")
NEEDED_RE = re.compile(r"\(NEEDED\).*\[([^]]+)]")
RUNPATH_RE = re.compile(r"\(RUNPATH\).*\[([^]]*)]")
RPATH_RE = re.compile(r"\(RPATH\).*\[([^]]*)]")

EXPECTED_CONTRACT_ID = "linux-x64-gnu-webgpu-ort-1.23.0-monolithic-v1"
EXPECTED_ORT = {
    "url": "https://github.com/microsoft/onnxruntime.git",
    "version": "1.23.0",
    "tag": "v1.23.0",
    "commit": "be835efc56aca19b8e810538ec93c8e150e0fc61",
}
EXPECTED_DAWN = {
    "url": "https://github.com/google/dawn/archive/9733be39e18186961d503e064874afe3e9ceb8d1.zip",
    "revision": "9733be39e18186961d503e064874afe3e9ceb8d1",
    "archiveSha1": "2a4017c32892b90d072a9102eba90ae691fae36d",
    "authority": "onnxruntime/cmake/deps.txt",
}
EXPECTED_UPSTREAM_ARGUMENTS = [
    "--update",
    "--build",
    "--config",
    "Release",
    "--build_shared_lib",
    "--use_webgpu",
    "--skip_tests",
    "--cmake_generator",
    "Ninja",
]
EXPECTED_DYNAMIC_DEPENDENCIES = [
    "libstdc++.so.6",
    "libm.so.6",
    "libgcc_s.so.1",
    "libc.so.6",
    "ld-linux-x86-64.so.2",
]
EXPECTED_QUALIFICATION = {
    "status": "proof-of-concept-only",
    "evidenceId": "native-webgpu-ort-1.23.0-20260716",
    "evidenceArtifactSha256": "fb96eb8a8fb22adc058bad8e6d1379c2bcc4ff643ddc019e4c71adc25e2ff831",
    "evidenceArtifactHashIsProductionLock": False,
    "providerGatePassed": False,
    "knownLimitations": [
        "The evidence is Linux x64 inference-only on one NVIDIA Vulkan device, not an end-to-end or cross-vendor Provider Gate.",
        "Recognition partitions Slice.2, Concat.2, and Gather to CPU; cpuPartition=forbid fails.",
        "The production artifact SHA-256 is intentionally unset until a release builder produces and qualifies an artifact.",
    ],
}


class ContractError(RuntimeError):
    """The runtime lock, source, host, or artifact violates the contract."""


def run(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exception:
        command = " ".join(arguments)
        detail = (exception.stderr or exception.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ContractError(f"command failed ({command}){suffix}") from exception


def load_lock(path: Path = DEFAULT_LOCK) -> dict[str, object]:
    try:
        lock = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exception:
        raise ContractError(f"cannot read runtime lock {path}: {exception}") from exception
    if not isinstance(lock, dict):
        raise ContractError("runtime lock root must be an object")
    validate_lock(lock)
    return lock


def require_mapping(parent: dict[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"runtime lock field {key!r} must be an object")
    return value


def require_string(parent: dict[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"runtime lock field {key!r} must be a non-empty string")
    return value


def values_match_exactly(value: object, expected: object) -> bool:
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        if value.keys() != expected.keys():
            return False
        return all(values_match_exactly(value[key], item) for key, item in expected.items())
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            values_match_exactly(actual, item)
            for actual, item in zip(value, expected)
        )
    return value == expected


def require_exact(value: object, expected: object, field: str) -> None:
    if not values_match_exactly(value, expected):
        raise ContractError(
            f"runtime lock field {field} must be exactly {expected!r}, got {value!r}"
        )


def validate_lock(lock: dict[str, object]) -> None:
    require_exact(lock.get("schemaVersion"), 1, "schemaVersion")
    require_exact(lock.get("contractId"), EXPECTED_CONTRACT_ID, "contractId")

    scope = require_mapping(lock, "scope")
    expected_scope = {
        "operatingSystem": "linux",
        "architecture": "x86_64",
        "libc": "glibc",
    }
    if scope != expected_scope:
        raise ContractError(f"runtime scope must be exactly {expected_scope}")

    topology = require_mapping(lock, "topology")
    if topology.get("kind") != "monolithic":
        raise ContractError("WGPU-001 requires a monolithic runtime")
    if "plugin" not in topology:
        raise ContractError("runtime lock field topology.plugin must be explicitly null")
    require_exact(topology["plugin"], None, "topology.plugin")
    if topology.get("providers") != ["cpu", "webgpu"]:
        raise ContractError("monolithic runtime must contain CPU and WebGPU providers")
    if topology.get("graphicsBackend") != "Vulkan":
        raise ContractError("Linux WebGPU runtime must use Vulkan")
    require_exact(
        topology.get("webgpuImplementation"),
        "Dawn Native",
        "topology.webgpuImplementation",
    )

    sources = require_mapping(lock, "sources")
    ort = require_mapping(sources, "onnxruntime")
    dawn = require_mapping(sources, "dawn")
    ort_commit = require_string(ort, "commit")
    dawn_revision = require_string(dawn, "revision")
    dawn_sha1 = require_string(dawn, "archiveSha1")
    if not HEX40.fullmatch(ort_commit) or not HEX40.fullmatch(dawn_revision):
        raise ContractError("source commits must be lowercase 40-character SHA-1 values")
    if not HEX40.fullmatch(dawn_sha1):
        raise ContractError("Dawn archiveSha1 must be a lowercase SHA-1")
    require_exact(ort, EXPECTED_ORT, "sources.onnxruntime")
    require_exact(dawn, EXPECTED_DAWN, "sources.dawn")

    build = require_mapping(lock, "build")
    if build.get("configuration") != "Release" or build.get("generator") != "Ninja":
        raise ContractError("runtime must use the Release configuration and Ninja")
    upstream_arguments = build.get("upstreamArguments")
    require_exact(
        upstream_arguments,
        EXPECTED_UPSTREAM_ARGUMENTS,
        "build.upstreamArguments",
    )

    definitions = require_mapping(build, "cmakeDefinitions")
    required_definitions = {
        "onnxruntime_BUILD_UNIT_TESTS": "OFF",
        "onnxruntime_BUILD_NODEJS": "OFF",
        "onnxruntime_BUILD_DAWN_MONOLITHIC_LIBRARY": "OFF",
        "onnxruntime_USE_EXTERNAL_DAWN": "OFF",
        "DAWN_BUILD_TESTS": "OFF",
        "DAWN_BUILD_SAMPLES": "OFF",
        "DAWN_BUILD_NODE_BINDINGS": "OFF",
        "DAWN_ENABLE_VULKAN": "ON",
        "DAWN_ENABLE_D3D11": "OFF",
        "DAWN_ENABLE_D3D12": "OFF",
        "DAWN_ENABLE_METAL": "OFF",
        "DAWN_ENABLE_DESKTOP_GL": "OFF",
        "DAWN_ENABLE_OPENGLES": "OFF",
        "DAWN_USE_X11": "OFF",
        "DAWN_USE_WAYLAND": "OFF",
    }
    if definitions != required_definitions:
        raise ContractError("cmakeDefinitions do not match the Vulkan-only product contract")

    artifacts = require_mapping(lock, "artifacts")
    if artifacts.get("versionedLibrary") != "libonnxruntime.so.1.23.0":
        raise ContractError("unexpected versioned runtime library name")
    if artifacts.get("soname") != "libonnxruntime.so.1":
        raise ContractError("unexpected runtime SONAME")
    if artifacts.get("runpath") != "$ORIGIN":
        raise ContractError("runtime RUNPATH must be $ORIGIN")
    expected_links = {
        "libonnxruntime.so": "libonnxruntime.so.1",
        "libonnxruntime.so.1": "libonnxruntime.so.1.23.0",
    }
    if artifacts.get("symlinks") != expected_links:
        raise ContractError("runtime symlink contract is invalid")
    require_exact(
        artifacts.get("allowedDynamicDependencies"),
        EXPECTED_DYNAMIC_DEPENDENCIES,
        "artifacts.allowedDynamicDependencies",
    )
    if "productionSha256" not in artifacts:
        raise ContractError(
            "runtime lock field artifacts.productionSha256 must be explicitly null"
        )
    require_exact(
        artifacts["productionSha256"],
        None,
        "artifacts.productionSha256",
    )
    require_exact(
        artifacts.get("productionHashStatus"),
        "pending",
        "artifacts.productionHashStatus",
    )

    qualification = require_mapping(lock, "qualification")
    require_exact(qualification, EXPECTED_QUALIFICATION, "qualification")


def validate_host(
    lock: dict[str, object],
    *,
    system: str | None = None,
    machine: str | None = None,
    libc: str | None = None,
) -> None:
    system = system or platform.system()
    machine = machine or platform.machine()
    if libc is None:
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
        except (AttributeError, OSError, ValueError):
            libc = ""
    if system != "Linux":
        raise ContractError(f"runtime build requires Linux, got {system}")
    if machine.lower() not in {"x86_64", "amd64"}:
        raise ContractError(f"runtime build requires x86_64, got {machine}")
    if not libc.lower().startswith("glibc "):
        raise ContractError(f"runtime build requires glibc, got {libc or 'unknown libc'}")
    scope = require_mapping(lock, "scope")
    if scope != {
        "operatingSystem": "linux",
        "architecture": "x86_64",
        "libc": "glibc",
    }:
        raise ContractError("host validation received an incompatible runtime lock")


def require_tools(names: Sequence[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise ContractError(f"required build tools are missing: {', '.join(missing)}")


def git_output(source: Path, *arguments: str) -> str:
    return run(["git", *arguments], cwd=source, capture_output=True).stdout.strip()


def validate_source(source: Path, lock: dict[str, object]) -> None:
    try:
        inside_work_tree = git_output(source, "rev-parse", "--is-inside-work-tree")
    except ContractError as exception:
        raise ContractError(f"ONNX Runtime source is not a Git checkout: {source}") from exception
    if inside_work_tree != "true":
        raise ContractError(f"ONNX Runtime source is not a Git checkout: {source}")
    expected = require_string(
        require_mapping(require_mapping(lock, "sources"), "onnxruntime"), "commit"
    )
    actual = git_output(source, "rev-parse", "HEAD")
    if actual != expected:
        raise ContractError(f"ONNX Runtime commit mismatch: expected {expected}, got {actual}")
    dirty = git_output(source, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise ContractError("ONNX Runtime source checkout must be clean")
    version_path = source / "VERSION_NUMBER"
    try:
        version = version_path.read_text("utf-8").strip()
    except OSError as exception:
        raise ContractError(
            f"cannot read ONNX Runtime version file {version_path}: {exception}"
        ) from exception
    if version != "1.23.0":
        raise ContractError(f"ONNX Runtime VERSION_NUMBER mismatch: {version}")

    dawn = require_mapping(require_mapping(lock, "sources"), "dawn")
    expected_line = (
        "dawn;"
        + require_string(dawn, "url")
        + ";"
        + require_string(dawn, "archiveSha1")
    )
    dependencies_path = source / "cmake" / "deps.txt"
    try:
        dependencies = dependencies_path.read_text("utf-8").splitlines()
    except OSError as exception:
        raise ContractError(
            f"cannot read ONNX Runtime dependency lock {dependencies_path}: {exception}"
        ) from exception
    if expected_line not in dependencies:
        raise ContractError("ONNX Runtime cmake/deps.txt does not match the locked Dawn archive")


def prepare_source(work_dir: Path, lock: dict[str, object], source_dir: Path | None) -> Path:
    if source_dir is not None:
        source = source_dir.resolve()
        validate_source(source, lock)
        return source

    source = work_dir / "onnxruntime"
    if not source.exists():
        ort = require_mapping(require_mapping(lock, "sources"), "onnxruntime")
        run(
            [
                "git",
                "clone",
                "--branch",
                require_string(ort, "tag"),
                "--depth",
                "1",
                "--no-checkout",
                require_string(ort, "url"),
                str(source),
            ]
        )
        run(["git", "checkout", "--detach", require_string(ort, "commit")], cwd=source)
    validate_source(source, lock)
    return source


def build_arguments(
    source: Path, work_dir: Path, lock: dict[str, object], jobs: int
) -> list[str]:
    build = require_mapping(lock, "build")
    upstream = build.get("upstreamArguments")
    assert isinstance(upstream, list)
    definitions = require_mapping(build, "cmakeDefinitions")
    return [
        sys.executable,
        str(source / "tools" / "ci_build" / "build.py"),
        *upstream,
        "--build_dir",
        str(work_dir / "build"),
        "--parallel",
        str(jobs),
        "--cmake_extra_defines",
        *(f"{key}={value}" for key, value in sorted(definitions.items())),
    ]


def parse_dynamic_section(
    text: str,
) -> tuple[str | None, set[str], list[str], list[str]]:
    soname: str | None = None
    needed: set[str] = set()
    runpaths: list[str] = []
    rpaths: list[str] = []
    for line in text.splitlines():
        if match := SONAME_RE.search(line):
            soname = match.group(1)
        if match := NEEDED_RE.search(line):
            needed.add(match.group(1))
        if match := RUNPATH_RE.search(line):
            runpaths.append(match.group(1))
        if match := RPATH_RE.search(line):
            rpaths.append(match.group(1))
    return soname, needed, runpaths, rpaths


def validate_artifact(
    library: Path,
    lock: dict[str, object],
    *,
    dynamic_section: str | None = None,
) -> dict[str, object]:
    if not library.is_file() or library.is_symlink():
        raise ContractError(f"versioned runtime artifact is missing or not a regular file: {library}")
    artifacts = require_mapping(lock, "artifacts")
    if dynamic_section is None:
        dynamic_section = run(
            ["readelf", "-d", str(library)], capture_output=True
        ).stdout
    soname, needed, runpaths, rpaths = parse_dynamic_section(dynamic_section)
    expected_soname = require_string(artifacts, "soname")
    if soname != expected_soname:
        raise ContractError(f"runtime SONAME mismatch: expected {expected_soname}, got {soname}")
    if rpaths:
        raise ContractError(f"runtime must not contain DT_RPATH, got {rpaths!r}")
    expected_runpath = require_string(artifacts, "runpath")
    if len(runpaths) != 1 or runpaths[0] != expected_runpath:
        raise ContractError(
            "runtime must contain exactly one DT_RUNPATH entry with value "
            f"{expected_runpath!r}, got {runpaths!r}"
        )
    allowlist_value = artifacts.get("allowedDynamicDependencies")
    assert isinstance(allowlist_value, list)
    allowlist = set(allowlist_value)
    unexpected = needed - allowlist
    if unexpected:
        raise ContractError(f"runtime has unexpected dynamic dependencies: {sorted(unexpected)}")
    if not needed:
        raise ContractError("runtime dynamic dependency list is empty")
    data = library.read_bytes()
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "soname": soname,
        "runpath": runpaths,
        "dynamicDependencies": sorted(needed),
    }


def stage_artifact(
    built_library: Path,
    output_dir: Path,
    source: Path,
    arguments: Sequence[str],
    lock: dict[str, object],
) -> Path:
    if output_dir.exists():
        raise ContractError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        artifacts = require_mapping(lock, "artifacts")
        versioned_name = require_string(artifacts, "versionedLibrary")
        staged_library = stage / versioned_name
        shutil.copy2(built_library, staged_library)
        artifact_details = validate_artifact(staged_library, lock)

        links = artifacts.get("symlinks")
        assert isinstance(links, dict)
        for name, target in links.items():
            assert isinstance(name, str) and isinstance(target, str)
            if Path(name).name != name or Path(target).name != target:
                raise ContractError("artifact symlinks must use relative leaf names")
            (stage / name).symlink_to(target)

        ort = require_mapping(require_mapping(lock, "sources"), "onnxruntime")
        dawn = require_mapping(require_mapping(lock, "sources"), "dawn")
        qualification = require_mapping(lock, "qualification")
        manifest = {
            "schemaVersion": 1,
            "contractId": lock["contractId"],
            "artifact": {"filename": versioned_name, **artifact_details},
            "symlinks": links,
            "sources": {
                "onnxruntimeCommit": git_output(source, "rev-parse", "HEAD"),
                "dawnRevision": dawn["revision"],
                "dawnArchiveSha1": dawn["archiveSha1"],
            },
            "build": {
                "arguments": list(arguments),
                "configuration": require_mapping(lock, "build")["configuration"],
            },
            "qualification": {
                **qualification,
                "productionHashStatus": artifacts["productionHashStatus"],
                "productionArtifactQualified": False,
            },
        }
        manifest_path = stage / "artifact-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
        os.replace(stage, output_dir)
        return output_dir / "artifact-manifest.json"
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".cache" / "webgpu-runtime")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "webgpu-runtime")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument(
        "--validate-lock",
        action="store_true",
        help="validate the contract only; do not inspect the host, fetch, or build",
    )
    arguments = parser.parse_args()

    lock = load_lock(arguments.lock)
    if arguments.validate_lock:
        print(arguments.lock.resolve())
        return 0
    if arguments.jobs < 1:
        raise ContractError("--jobs must be at least 1")

    validate_host(lock)
    require_tools(["git", "cmake", "ninja", "readelf"])
    work_dir = arguments.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    source = prepare_source(work_dir, lock, arguments.source_dir)
    command = build_arguments(source, work_dir, lock, arguments.jobs)
    run(command, cwd=source)

    artifacts = require_mapping(lock, "artifacts")
    built_library = (
        work_dir
        / "build"
        / require_string(require_mapping(lock, "build"), "configuration")
        / require_string(artifacts, "versionedLibrary")
    )
    manifest = stage_artifact(
        built_library,
        arguments.output_dir.resolve(),
        source,
        command,
        lock,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exception:
        print(f"error: {exception}", file=sys.stderr)
        raise SystemExit(1)
