#!/usr/bin/env python3
"""Stage, validate, and pack the light-ocr npm release set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = json.loads(
    (ROOT / "bindings" / "node" / "package.json").read_text("utf-8")
)["version"]
BUNDLE_ID = "ppocrv6-small-apple-20260715.1"
MODEL_PACKAGE = "@arcships/light-ocr-model-ppocrv6-small"
FACADE_PACKAGE = "@arcships/light-ocr"
NPM_REGISTRY = "https://registry.npmjs.org/"
REGISTRY_WAIT_SECONDS = 600
PLATFORMS: dict[str, dict[str, Any]] = {
    "macos-arm64": {
        "package": "@arcships/light-ocr-darwin-arm64",
        "os": ["darwin"],
        "cpu": ["arm64"],
        "architecture": "arm64",
        "runtime": "libonnxruntime.1.22.0.dylib",
    },
    "macos-x64": {
        "package": "@arcships/light-ocr-darwin-x64",
        "os": ["darwin"],
        "cpu": ["x64"],
        "architecture": "x86_64",
        "runtime": "libonnxruntime.1.22.0.dylib",
    },
    "linux-x64": {
        "package": "@arcships/light-ocr-linux-x64-gnu",
        "os": ["linux"],
        "cpu": ["x64"],
        "architecture": "x86_64",
        "libc": ["glibc"],
        "runtime": "libonnxruntime.so.1",
    },
    "windows-x64": {
        "package": "@arcships/light-ocr-win32-x64",
        "os": ["win32"],
        "cpu": ["x64"],
        "architecture": "x86_64",
        "runtime": "onnxruntime.dll",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def remove_and_create(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"required file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"package staging contains a symlink: {path}")


def cmake_is_multi_config(build: Path) -> bool:
    cache = build / "CMakeCache.txt"
    if not cache.is_file():
        return False
    for line in cache.read_text("utf-8", errors="replace").splitlines():
        if line.startswith("CMAKE_CONFIGURATION_TYPES:"):
            return bool(line.partition("=")[2])
    return False


def build_file(build: Path, filename: str, configuration: str) -> Path:
    if not isinstance(configuration, str) or not re.fullmatch(
        r"[A-Za-z0-9_.+-]+", configuration
    ):
        raise RuntimeError("build configuration is invalid")
    if cmake_is_multi_config(build):
        candidate = build / "bin" / configuration / filename
        if candidate.is_file():
            return candidate
        raise RuntimeError(
            f"{configuration} build output is missing: {filename}"
        )
    candidate = build / "bin" / filename
    if candidate.is_file():
        return candidate
    raise RuntimeError(f"build output is missing: {filename}")


def safe_package_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} must be a non-empty package-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError(f"{field} must be a safe package-relative path")
    return path


def file_record(path: Path, package_root: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"runtime payload must be a regular file: {path}")
    return {
        "path": path.relative_to(package_root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def validate_file_record(record: object, package_root: Path, field: str) -> Path:
    if not isinstance(record, dict):
        raise RuntimeError(f"{field} must be an object")
    relative = safe_package_path(record.get("path"), f"{field}.path")
    path = package_root.joinpath(*relative.parts)
    try:
        path.resolve().relative_to(package_root.resolve())
    except ValueError as exception:
        raise RuntimeError(f"{field}.path escapes the package") from exception
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"descriptor artifact is missing or not a regular file: {relative}")
    if type(record.get("bytes")) is not int or path.stat().st_size != record["bytes"]:
        raise RuntimeError(f"descriptor artifact byte count mismatch: {relative}")
    digest = record.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError(f"descriptor artifact hash is invalid: {relative}")
    if sha256(path) != digest:
        raise RuntimeError(f"descriptor artifact hash mismatch: {relative}")
    return path


def validate_runtime_descriptor(
    descriptor: object,
    package_root: Path,
    *,
    platform_id: str | None = None,
    require_released: bool = False,
) -> None:
    if not isinstance(descriptor, dict) or descriptor.get("schemaVersion") != "1.0":
        raise RuntimeError("runtime descriptor schemaVersion must be 1.0")
    platform = descriptor.get("platform")
    if not isinstance(platform, dict) or not isinstance(platform.get("id"), str):
        raise RuntimeError("runtime descriptor platform is invalid")
    if platform_id is not None and platform.get("id") != platform_id:
        raise RuntimeError("runtime descriptor platform identity mismatch")
    qualification_only = descriptor.get("qualificationOnly")
    released = descriptor.get("released")
    if type(qualification_only) is not bool or type(released) is not bool:
        raise RuntimeError("runtime descriptor release flags must be booleans")
    if qualification_only == released:
        raise RuntimeError(
            "runtime descriptor must be either released or qualification-only"
        )
    if require_released and (qualification_only or not released):
        raise RuntimeError("qualification-only runtime descriptor cannot enter npm release")
    policy = descriptor.get("autoPolicy")
    if not isinstance(policy, dict) or not isinstance(policy.get("id"), str):
        raise RuntimeError("runtime descriptor Auto policy is invalid")
    providers = policy.get("providers")
    if not isinstance(providers, list) or not providers or providers[-1] != "cpu":
        raise RuntimeError("runtime descriptor Auto policy must be non-empty and end in cpu")
    if qualification_only and providers != ["cpu"]:
        raise RuntimeError("qualification descriptor released Auto policy must remain cpu-only")
    provider_records = descriptor.get("providers")
    if not isinstance(provider_records, dict) or "cpu" not in provider_records:
        raise RuntimeError("runtime descriptor must declare the CPU provider")
    if (
        len(providers) != len(set(providers))
        or any(provider not in provider_records for provider in providers)
    ):
        raise RuntimeError("runtime descriptor Auto policy providers are invalid")
    runtime = descriptor.get("runtime")
    expected_runtime = {
        "cpu": ("onnxruntime-cpu", "1.22.0", "onnxruntime-c-api-22"),
        "webgpu": (
            "onnxruntime-monolithic-webgpu",
            "1.23.0",
            "onnxruntime-c-api-23",
        ),
    }
    if not isinstance(runtime, dict) or runtime.get("flavor") not in expected_runtime:
        raise RuntimeError("runtime descriptor ABI identity is invalid")
    if tuple(runtime.get(field) for field in ("kind", "version", "abi")) != expected_runtime[
        runtime["flavor"]
    ]:
        raise RuntimeError("runtime descriptor ABI identity is invalid")
    if platform_id is not None:
        expected_policy = (
            ["cpu"]
            if qualification_only
            else ["apple", "cpu"]
            if platform_id.startswith("macos-")
            else ["webgpu", "cpu"]
            if runtime["flavor"] == "webgpu"
            else ["cpu"]
        )
        expected_available = (
            {"cpu", "webgpu"}
            if runtime["flavor"] == "webgpu"
            else {"apple", "cpu"}
            if platform_id.startswith("macos-")
            else {"cpu"}
        )
        if providers != expected_policy or set(provider_records) != expected_available:
            raise RuntimeError(
                "runtime descriptor providers disagree with platform capabilities"
            )
    addon = descriptor.get("addon")
    validate_file_record(addon, package_root, "addon")
    referenced: set[str] = {addon["path"]}
    for provider_id, provider in provider_records.items():
        if (
            provider_id not in {"cpu", "apple", "webgpu"}
            or not isinstance(provider, dict)
        ):
            raise RuntimeError("runtime descriptor provider entry is invalid")
        if not isinstance(provider.get("qualificationId"), str) or not provider[
            "qualificationId"
        ]:
            raise RuntimeError(f"provider {provider_id} has no qualification identity")
        artifacts = provider.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise RuntimeError(f"provider {provider_id} has no runtime artifacts")
        for index, artifact in enumerate(artifacts):
            validate_file_record(
                artifact, package_root, f"providers.{provider_id}.artifacts[{index}]"
            )
            referenced.add(artifact["path"])
        if provider_id == "webgpu":
            compatibility = provider.get("compatibilityManifest")
            compatibility_path = validate_file_record(
                compatibility, package_root, "providers.webgpu.compatibilityManifest"
            )
            if compatibility["path"] not in {
                artifact.get("path") for artifact in artifacts if isinstance(artifact, dict)
            }:
                raise RuntimeError(
                    "WebGPU compatibility manifest must be part of provider artifacts"
                )
            runtime_artifacts = [
                artifact
                for artifact in artifacts
                if isinstance(artifact, dict)
                and artifact.get("path") != compatibility["path"]
            ]
            try:
                compatibility_data = read_json(compatibility_path)
            except (OSError, json.JSONDecodeError) as exception:
                raise RuntimeError(
                    "WebGPU compatibility manifest is unreadable"
                ) from exception
            expected_compatibility = {
                "schemaVersion": "1.0",
                "provider": "webgpu",
                "platformId": platform["id"],
                "runtimeVersion": runtime["version"],
                "runtimeAbi": runtime["abi"],
                "qualificationId": provider.get("qualificationId"),
                "qualificationOnly": qualification_only,
                "released": released,
                "runtimeArtifact": (
                    {
                        "bytes": runtime_artifacts[0].get("bytes"),
                        "sha256": runtime_artifacts[0].get("sha256"),
                    }
                    if len(runtime_artifacts) == 1
                    else None
                ),
            }
            if compatibility_data != expected_compatibility:
                raise RuntimeError(
                    "WebGPU compatibility manifest does not match the runtime descriptor"
                )
    native = package_root / "native"
    actual = {
        path.relative_to(package_root).as_posix()
        for path in native.rglob("*")
        if path.is_file()
        and path.relative_to(package_root).as_posix()
        != "native/runtime-descriptor.json"
    }
    if actual != referenced:
        raise RuntimeError(
            "runtime descriptor payload inventory mismatch: "
            f"missing={sorted(referenced - actual)}, extra={sorted(actual - referenced)}"
        )


def _webgpu_manifest(arguments: argparse.Namespace) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    manifest_path = getattr(arguments, "webgpu_artifact_manifest", None)
    compatibility_path = getattr(arguments, "webgpu_compatibility_manifest", None)
    if manifest_path is None or compatibility_path is None:
        raise RuntimeError("WebGPU staging requires artifact and compatibility manifests")
    manifest_path = Path(manifest_path).absolute()
    compatibility_path = Path(compatibility_path).absolute()
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or compatibility_path.is_symlink()
        or not compatibility_path.is_file()
    ):
        raise RuntimeError(
            "WebGPU artifact and compatibility manifests must be regular files"
        )
    manifest = read_json(manifest_path)
    compatibility = read_json(compatibility_path)
    if manifest.get("contractId") != "linux-x64-gnu-webgpu-ort-1.23.0-monolithic-v1":
        raise RuntimeError("WebGPU artifact manifest contract ID mismatch")
    if manifest.get("runtimeFlavor") != "webgpu":
        raise RuntimeError("WebGPU artifact manifest runtime flavor mismatch")

    headers = manifest.get("headers")
    expected_headers = [
        "onnxruntime_c_api.h",
        "onnxruntime_cxx_api.h",
        "onnxruntime_cxx_inline.h",
        "onnxruntime_float16.h",
        "onnxruntime_run_options_config_keys.h",
        "onnxruntime_session_options_config_keys.h",
    ]
    if (
        not isinstance(headers, dict)
        or headers.get("directory") != "include"
        or headers.get("onnxruntimeVersion") != "1.23.0"
        or headers.get("onnxruntimeCommit")
        != "be835efc56aca19b8e810538ec93c8e150e0fc61"
        or not isinstance(headers.get("files"), list)
        or len(headers["files"]) != len(expected_headers)
    ):
        raise RuntimeError("WebGPU SDK header identity does not match locked ORT 1.23")
    for expected_name, record in zip(expected_headers, headers["files"], strict=True):
        if not isinstance(record, dict) or record.get("path") != f"include/{expected_name}":
            raise RuntimeError("WebGPU SDK header inventory is unsafe or out of order")
        validate_file_record(record, manifest_path.parent, f"headers.{expected_name}")

    expected_session_options = {
        "providerName": "WebGPU",
        "providerOptions": {
            "dawnBackendType": "Vulkan",
            "preferredLayout": "NHWC",
            "enableGraphCapture": "0",
            "validationMode": "basic",
        },
        "deviceIdSupported": False,
    }
    if manifest.get("sessionOptions") != expected_session_options:
        raise RuntimeError("WebGPU SDK session options do not match the locked contract")

    artifact = manifest.get("artifact")
    if (
        not isinstance(artifact, dict)
        or artifact.get("filename") != "lib/libonnxruntime.so.1.23.0"
    ):
        raise RuntimeError("WebGPU artifact manifest has no locked runtime artifact")
    source = manifest_path.parent / artifact["filename"]
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("WebGPU artifact manifest runtime is missing")
    actual_sha = sha256(source)
    if artifact.get("bytes") != source.stat().st_size or artifact.get("sha256") != actual_sha:
        raise RuntimeError("WebGPU artifact manifest runtime identity mismatch")

    qualification = manifest.get("qualification", {})
    if not isinstance(qualification, dict):
        raise RuntimeError("WebGPU artifact qualification must be an object")
    hash_status = qualification.get("productionHashStatus")
    gate = qualification.get("providerGatePassed")
    artifact_qualified = qualification.get("productionArtifactQualified")
    production_sha = qualification.get("productionSha256")
    qualification_build = bool(getattr(arguments, "qualification_build", False))
    if qualification_build:
        if hash_status != "pending" or gate is not False or artifact_qualified is not False:
            raise RuntimeError("WebGPU qualification artifact must remain pending and unqualified")
    elif (
        hash_status != "qualified"
        or gate is not True
        or artifact_qualified is not True
        or production_sha != actual_sha
    ):
        raise RuntimeError(
            "WebGPU release staging requires a qualified production artifact and exact production hash"
        )
    qualification_id = qualification.get("evidenceId")
    expected_compatibility = {
        "schemaVersion": "1.0",
        "provider": "webgpu",
        "platformId": "linux-x64",
        "runtimeVersion": "1.23.0",
        "runtimeAbi": "onnxruntime-c-api-23",
        "qualificationId": qualification_id,
        "qualificationOnly": qualification_build,
        "released": not qualification_build,
        "runtimeArtifact": {
            "bytes": source.stat().st_size,
            "sha256": actual_sha,
        },
    }
    if (
        not isinstance(qualification_id, str)
        or not qualification_id
        or not isinstance(compatibility, dict)
        or set(compatibility) != set(expected_compatibility)
        or not isinstance(compatibility.get("runtimeArtifact"), dict)
        or set(compatibility["runtimeArtifact"]) != {"bytes", "sha256"}
        or any(
            compatibility.get(field) != value
            for field, value in expected_compatibility.items()
        )
    ):
        raise RuntimeError(
            "WebGPU compatibility manifest does not match the staged runtime contract"
        )
    return manifest, compatibility_path, compatibility


def stage_native(arguments: argparse.Namespace) -> None:
    platform = PLATFORMS[arguments.platform_id]
    build = arguments.build_dir.resolve()
    metadata = arguments.metadata_dir.resolve()
    output = arguments.output_dir.resolve()
    runtime_flavor = getattr(arguments, "runtime_flavor", "cpu")
    qualification_build = bool(getattr(arguments, "qualification_build", False))
    configuration = getattr(arguments, "configuration", "Release")
    if runtime_flavor not in {"cpu", "webgpu"}:
        raise RuntimeError("runtime flavor must be cpu or webgpu")
    if runtime_flavor == "webgpu" and arguments.platform_id != "linux-x64":
        raise RuntimeError("WebGPU runtime staging is limited to Linux x64 glibc")
    remove_and_create(output)

    native = output / "native"
    native.mkdir()
    addon = native / "light_ocr_node.node"
    copy_file(build_file(build, "light_ocr_node.node", configuration), addon)

    qualification_only = False
    released = True
    runtime_version = "1.22.0"
    runtime_kind = "onnxruntime-cpu"
    runtime_abi = "onnxruntime-c-api-22"
    provider_entries: dict[str, Any]
    if runtime_flavor == "cpu":
        runtime = native / platform["runtime"]
        copy_file(build_file(build, platform["runtime"], configuration), runtime)
        runtime_record = file_record(runtime, output)
        provider_entries = {
            "cpu": {
                "runtimeProvider": "CPUExecutionProvider",
                "qualificationId": "cpu-baseline-v1",
                "artifacts": [runtime_record],
            }
        }
        if platform["os"] == ["darwin"]:
            provider_entries["apple"] = {
                "runtimeProvider": "CoreML",
                "qualificationId": "apple-open-macos-v1",
                # Core ML is an OS framework; its provider implementation is
                # linked into the addon rather than staged as another runtime.
                "artifacts": [file_record(addon, output)],
            }
    else:
        manifest, compatibility_source, compatibility = _webgpu_manifest(arguments)
        artifact = manifest["artifact"]
        runtime = native / "libonnxruntime.so.1"
        copy_file(Path(arguments.webgpu_artifact_manifest).resolve().parent / artifact["filename"], runtime)
        compatibility_path = native / "webgpu-compatibility.json"
        copy_file(compatibility_source, compatibility_path)
        runtime_record = file_record(runtime, output)
        compatibility_record = file_record(compatibility_path, output)
        qualification = manifest.get("qualification", {})
        qualification_id = qualification.get("evidenceId", "unqualified-webgpu")
        qualification_only = qualification_build
        released = not qualification_only
        runtime_version = "1.23.0"
        runtime_kind = "onnxruntime-monolithic-webgpu"
        runtime_abi = "onnxruntime-c-api-23"
        provider_entries = {
            "webgpu": {
                "runtimeProvider": "WebGpuExecutionProvider",
                "qualificationId": qualification_id,
                "compatibilityManifest": compatibility_record,
                "artifacts": [runtime_record, compatibility_record],
            },
            "cpu": {
                "runtimeProvider": "CPUExecutionProvider",
                "qualificationId": "cpu-baseline-v1",
                "artifacts": [runtime_record],
            },
        }

    copy_file(metadata / "license-inventory.json", output / "license-inventory.json")
    copy_file(metadata / "sbom.spdx.json", output / "sbom.spdx.json")
    shutil.copytree(metadata / "licenses", output / "licenses")

    descriptor = {
        "schemaVersion": "1.0",
        "platform": {
            "id": arguments.platform_id,
            "os": platform["os"][0],
            "architecture": platform["architecture"],
            **({"libc": platform["libc"][0]} if "libc" in platform else {}),
        },
        "runtime": {
            "flavor": runtime_flavor,
            "kind": runtime_kind,
            "version": runtime_version,
            "abi": runtime_abi,
        },
        "qualificationOnly": qualification_only,
        "released": released,
        "autoPolicy": {
            "id": f"{arguments.platform_id}-v1",
            "version": 1,
            "providers": ["cpu"] if qualification_only else (
                ["webgpu", "cpu"]
                if runtime_flavor == "webgpu"
                else (["apple", "cpu"] if platform["os"] == ["darwin"] else ["cpu"])
            ),
        },
        "providers": provider_entries,
        "addon": file_record(addon, output),
    }
    write_json(native / "runtime-descriptor.json", descriptor)
    validate_runtime_descriptor(descriptor, output, platform_id=arguments.platform_id)

    records = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            records.append(file_record(path, output))
    write_json(
        output / "native-input.json",
        {
            "schemaVersion": "1.0",
            "platformId": arguments.platform_id,
            "package": platform["package"],
            "runtimeFlavor": runtime_flavor,
            "qualificationOnly": qualification_only,
            "files": records,
        },
    )
    reject_symlinks(output)
    print(json.dumps({"ok": True, "platformId": arguments.platform_id, "output": str(output)}))


def common_package(name: str, version: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "description": description,
        "license": "Apache-2.0",
        "repository": {
            "type": "git",
            "url": "git+https://github.com/arcships/light-ocr.git",
        },
        "homepage": "https://github.com/arcships/light-ocr#readme",
        "bugs": {"url": "https://github.com/arcships/light-ocr/issues"},
        "publishConfig": {"access": "public", "provenance": True},
    }


def package_readme(name: str, description: str, direct_install: bool) -> str:
    lines = [f"# {name}", "", description, ""]
    if direct_install:
        lines.extend(
            [
                "```bash",
                "npm install @arcships/light-ocr",
                "```",
                "",
                "The default PP-OCRv6 Small model is included as a required dependency; no",
                "runtime model download or postinstall compilation is used.",
            ]
        )
    else:
        lines.extend(
            [
                "This is an internal distribution package for `@arcships/light-ocr`. Install",
                "the facade package instead of depending on this package directly.",
            ]
        )
    lines.extend(
        [
            "",
            "Documentation: https://github.com/arcships/light-ocr",
            "",
            "License: Apache-2.0",
            "",
        ]
    )
    return "\n".join(lines)


def add_project_files(package: Path, readme: str) -> None:
    copy_file(ROOT / "LICENSE", package / "LICENSE")
    copy_file(ROOT / "NOTICE", package / "NOTICE")
    (package / "README.md").write_text(readme, encoding="utf-8")


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"required directory is missing: {source}")
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise RuntimeError(f"source directory contains a symlink: {path}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            copy_file(path, target)


def native_source(root: Path, platform_id: str) -> Path:
    candidates = [root / platform_id, root / f"native-{platform_id}"]
    for candidate in candidates:
        if (candidate / "native-input.json").is_file():
            return candidate
    raise RuntimeError(f"native input is missing for {platform_id}")


def artifact_hashes(package: Path, name: str, version: str) -> None:
    records = []
    for path in sorted(package.rglob("*")):
        if not path.is_file() or path.name == "artifact-hashes.json":
            continue
        records.append(
            {
                "path": path.relative_to(package).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    write_json(
        package / "artifact-hashes.json",
        {"schemaVersion": "1.0", "package": name, "version": version, "files": records},
    )


def assemble(arguments: argparse.Namespace) -> None:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", arguments.version):
        raise RuntimeError("version must be a plain stable SemVer value")
    version = arguments.version
    if tuple(int(part) for part in version.split(".")) < (0, 2, 0):
        raise RuntimeError("tiled-v1 packages require version 0.2.0 or newer")
    if version != SOURCE_VERSION:
        raise RuntimeError(
            f"release version {version} does not match source version {SOURCE_VERSION}"
        )
    output = arguments.output_dir.resolve()
    native_root = arguments.native_root.resolve()
    bundle = arguments.bundle.resolve()
    remove_and_create(output)

    manifest = read_json(bundle / "manifest.json")
    if manifest.get("bundleId") != BUNDLE_ID:
        raise RuntimeError("model bundle ID does not match the npm release contract")
    normalized_config = read_json(bundle / manifest["normalizedConfigPath"])
    tiled_contract = normalized_config.get("runtimeProfiles", {}).get("tiled", {})
    apple_provider = manifest.get("providers", {}).get("apple", {})
    validated_families = apple_provider.get("validatedDeviceFamilies", [])
    if (manifest.get("schemaVersion") != "1.1" or
            normalized_config.get("schemaVersion") != "1.2" or
            tiled_contract.get("contractVersion") != "tiled-v1" or
            apple_provider.get("schemaVersion") != "1.1" or
            apple_provider.get("devicePolicy") != "open-macos" or
            apple_provider.get("architectures") != ["arm64", "x86_64"] or
            not isinstance(validated_families, list) or
            len(validated_families) < 1 or
            len(validated_families) != len(set(validated_families)) or
            any(family not in {"Apple M1", "Apple M2", "Apple M3", "Apple M4"}
                for family in validated_families)):
        raise RuntimeError(
            "model bundle does not contain the tiled-v1 Apple release contract"
        )

    facade = output / "facade"
    facade.mkdir()
    copy_tree(ROOT / "bindings" / "node" / "js", facade / "js")
    facade_json = common_package(
        FACADE_PACKAGE,
        version,
        "Offline PP-OCRv6 OCR for Node.js, powered by an embeddable C++ core",
    )
    facade_json.update(
        {
            "keywords": ["ocr", "offline-ocr", "pp-ocrv6", "paddleocr", "node-api", "napi"],
            "type": "commonjs",
            "main": "./js/index.cjs",
            "module": "./js/index.mjs",
            "types": "./js/index.d.ts",
            "exports": {
                ".": {
                    "types": "./js/index.d.ts",
                    "import": "./js/index.mjs",
                    "require": "./js/index.cjs",
                }
            },
            "files": ["js/", "README.md", "LICENSE", "NOTICE"],
            "engines": {"node": "^22.0.0 || ^24.0.0"},
            "dependencies": {MODEL_PACKAGE: version},
            "optionalDependencies": {
                PLATFORMS[key]["package"]: version for key in sorted(PLATFORMS)
            },
        }
    )
    write_json(facade / "package.json", facade_json)
    add_project_files(
        facade,
        package_readme(
            FACADE_PACKAGE,
            "Offline OCR for Node.js applications, powered by PP-OCRv6 Small.",
            True,
        ),
    )

    model = output / "model-ppocrv6-small"
    model.mkdir()
    copy_tree(bundle, model / "bundle")
    model_json = common_package(
        MODEL_PACKAGE,
        version,
        "Pinned PP-OCRv6 Small model bundle for @arcships/light-ocr",
    )
    model_json.update(
        {
            "files": ["bundle/", "README.md", "LICENSE", "NOTICE"],
            "exports": {"./bundle/manifest.json": "./bundle/manifest.json"},
            "lightOcr": {
                "bundleId": BUNDLE_ID,
                "manifestSchemaVersion": manifest["schemaVersion"],
                "normalizedConfigSchemaVersion": normalized_config["schemaVersion"],
                "tiledContractVersion": tiled_contract["contractVersion"],
                "paddleOcrRevision": "b03f46425e8ff4442b268ce449e3eef758146cd4",
            },
        }
    )
    write_json(model / "package.json", model_json)
    add_project_files(
        model,
        package_readme(
            MODEL_PACKAGE,
            "The pinned PP-OCRv6 Small model bundle for `@arcships/light-ocr`.",
            False,
        ),
    )

    for platform_id, platform in PLATFORMS.items():
        source = native_source(native_root, platform_id)
        descriptor = read_json(source / "native-input.json")
        if descriptor.get("platformId") != platform_id:
            raise RuntimeError(f"native input identity mismatch for {platform_id}")
        runtime_descriptor_path = source / "native" / "runtime-descriptor.json"
        if not runtime_descriptor_path.is_file():
            raise RuntimeError(f"runtime descriptor is missing for {platform_id}")
        runtime_descriptor = read_json(runtime_descriptor_path)
        validate_runtime_descriptor(
            runtime_descriptor, source, platform_id=platform_id, require_released=True
        )
        if descriptor.get("qualificationOnly") is not False:
            raise RuntimeError("qualification-only native input cannot enter npm release")
        package = output / platform_id
        package.mkdir()
        copy_tree(source / "native", package / "native")
        copy_tree(source / "licenses", package / "licenses")
        copy_file(source / "license-inventory.json", package / "license-inventory.json")
        copy_file(source / "sbom.spdx.json", package / "sbom.spdx.json")
        package_json = common_package(
            platform["package"],
            version,
            f"Native {platform_id} runtime for @arcships/light-ocr",
        )
        package_json.update(
            {
                "main": "./native/light_ocr_node.node",
                "exports": {".": "./native/light_ocr_node.node"},
                "os": platform["os"],
                "cpu": platform["cpu"],
                "files": [
                    "native/",
                    "licenses/",
                    "license-inventory.json",
                    "sbom.spdx.json",
                    "artifact-hashes.json",
                    "README.md",
                    "LICENSE",
                    "NOTICE",
                ],
                "engines": {"node": "^22.0.0 || ^24.0.0"},
            }
        )
        if "libc" in platform:
            package_json["libc"] = platform["libc"]
        write_json(package / "package.json", package_json)
        add_project_files(
            package,
            package_readme(
                platform["package"],
                f"The prebuilt {platform_id} native runtime for `@arcships/light-ocr`.",
                False,
            ),
        )
        artifact_hashes(package, platform["package"], version)

    for package in output.iterdir():
        if package.is_dir():
            reject_symlinks(package)
    print(json.dumps({"ok": True, "version": version, "output": str(output)}))


def git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def package_directories(staging: Path) -> list[Path]:
    packages = sorted(path for path in staging.iterdir() if (path / "package.json").is_file())
    if len(packages) != 6:
        raise RuntimeError(f"expected six staged packages, found {len(packages)}")
    names = [read_json(path / "package.json")["name"] for path in packages]
    if len(set(names)) != 6:
        raise RuntimeError("staged package names are not unique")
    return packages


def run_npm_pack(npm: str, package: Path, destination: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [npm, "pack", "--json", "--ignore-scripts", "--pack-destination", str(destination), str(package)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    records = json.loads(completed.stdout)
    if len(records) != 1:
        raise RuntimeError(f"npm pack returned an unexpected result for {package}")
    return records[0]


def validate_tarball(package: Path, tarball: Path, npm_record: dict[str, Any]) -> None:
    expected = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file()
    }
    reported = {record["path"] for record in npm_record["files"]}
    if reported != expected:
        missing = sorted(expected - reported)
        extra = sorted(reported - expected)
        raise RuntimeError(f"npm inventory mismatch for {package.name}: missing={missing}, extra={extra}")

    archived: set[str] = set()
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "package":
                raise RuntimeError(f"unsafe npm tar entry: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"npm tar contains a link: {member.name}")
            if member.isfile():
                archived.add(PurePosixPath(*path.parts[1:]).as_posix())
    if archived != expected:
        raise RuntimeError(f"tar inventory differs from staging for {package.name}")


def pack(arguments: argparse.Namespace) -> None:
    staging = arguments.staging_dir.resolve()
    output = arguments.output_dir.resolve()
    remove_and_create(output)
    release_records = []
    with tempfile.TemporaryDirectory(prefix="light-ocr-npm-pack-") as temporary:
        first = Path(temporary) / "first"
        second = Path(temporary) / "second"
        first.mkdir()
        second.mkdir()
        for package in package_directories(staging):
            reject_symlinks(package)
            record = run_npm_pack(arguments.npm, package, first)
            repeat = run_npm_pack(arguments.npm, package, second)
            first_tarball = first / record["filename"]
            second_tarball = second / repeat["filename"]
            first_sha256 = sha256(first_tarball)
            if first_sha256 != sha256(second_tarball):
                raise RuntimeError(f"npm pack is not deterministic for {record['name']}")
            validate_tarball(package, first_tarball, record)
            destination = output / record["filename"]
            shutil.copyfile(first_tarball, destination)
            release_records.append(
                {
                    "name": record["name"],
                    "version": record["version"],
                    "filename": record["filename"],
                    "bytes": destination.stat().st_size,
                    "unpackedBytes": record["unpackedSize"],
                    "sha256": first_sha256,
                    "shasum": record["shasum"],
                    "integrity": record["integrity"],
                    "fileCount": len(record["files"]),
                }
            )
    versions = {record["version"] for record in release_records}
    if len(versions) != 1:
        raise RuntimeError("npm packages do not share one lockstep version")
    release_manifest = {
        "schemaVersion": "1.0",
        "gitRevision": git_revision(),
        "version": next(iter(versions)),
        "distTag": "next",
        "npmVersion": subprocess.run(
            [arguments.npm, "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip(),
        "packages": sorted(release_records, key=lambda record: record["name"]),
    }
    write_json(output / "release-manifest.json", release_manifest)
    print(json.dumps({"ok": True, "release": release_manifest}, separators=(",", ":")))


def npm_integrity(npm: str, specification: str) -> str | None:
    completed = subprocess.run(
        [
            npm,
            "view",
            specification,
            "dist.integrity",
            "--json",
            "--prefer-online",
            f"--registry={NPM_REGISTRY}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode == 0:
        value = json.loads(completed.stdout)
        if not isinstance(value, str) or not value.startswith("sha512-"):
            raise RuntimeError(f"registry returned invalid integrity for {specification}")
        return value
    if "E404" in completed.stderr or "404 Not Found" in completed.stderr:
        return None
    raise RuntimeError(f"npm view failed for {specification}: {completed.stderr.strip()}")


def wait_for_integrity(npm: str, specification: str, expected: str) -> None:
    attempts = REGISTRY_WAIT_SECONDS // 3
    for _ in range(attempts):
        actual = npm_integrity(npm, specification)
        if actual == expected:
            return
        if actual is not None and actual != expected:
            raise RuntimeError(f"registry integrity mismatch for {specification}")
        time.sleep(3)
    raise RuntimeError(
        f"registry did not expose {specification} within {REGISTRY_WAIT_SECONDS} seconds"
    )


def npm_dist_tag(npm: str, package: str, tag: str) -> str | None:
    completed = subprocess.run(
        [
            npm,
            "view",
            package,
            f"dist-tags.{tag}",
            "--json",
            "--prefer-online",
            f"--registry={NPM_REGISTRY}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"npm dist-tag lookup failed for {package}: {completed.stderr.strip()}"
        )
    value = json.loads(completed.stdout)
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"registry returned invalid {tag} tag for {package}")
    return value


def wait_for_dist_tag(npm: str, package: str, tag: str, version: str) -> None:
    attempts = REGISTRY_WAIT_SECONDS // 3
    for _ in range(attempts):
        if npm_dist_tag(npm, package, tag) == version:
            return
        time.sleep(3)
    raise RuntimeError(
        f"registry did not expose {package}@{version} as {tag} "
        f"within {REGISTRY_WAIT_SECONDS} seconds"
    )


def publish(arguments: argparse.Namespace) -> None:
    tarballs = arguments.tarball_dir.resolve()
    release = read_json(tarballs / "release-manifest.json")
    records = {record["name"]: record for record in release["packages"]}
    if arguments.phase == "dependencies":
        names = [MODEL_PACKAGE] + sorted(
            platform["package"] for platform in PLATFORMS.values()
        )
    else:
        names = [FACADE_PACKAGE]
    for name in names:
        record = records[name]
        specification = f"{name}@{record['version']}"
        existing = npm_integrity(arguments.npm, specification)
        if existing is not None:
            if existing != record["integrity"]:
                raise RuntimeError(f"published package integrity mismatch for {specification}")
            print(json.dumps({"package": specification, "status": "already-published"}))
            continue
        tarball = tarballs / record["filename"]
        if sha256(tarball) != record["sha256"]:
            raise RuntimeError(f"release tarball hash mismatch for {specification}")
        subprocess.run(
            [
                arguments.npm,
                "publish",
                str(tarball),
                "--access",
                "public",
                "--tag",
                arguments.tag,
                "--provenance",
            ],
            cwd=ROOT,
            check=True,
        )
        wait_for_integrity(arguments.npm, specification, record["integrity"])
        print(json.dumps({"package": specification, "status": "published"}))


def promote(arguments: argparse.Namespace) -> None:
    tarballs = arguments.tarball_dir.resolve()
    release = read_json(tarballs / "release-manifest.json")
    if (
        arguments.expected_version
        and release.get("version") != arguments.expected_version
    ):
        raise RuntimeError("release manifest version does not match promotion request")
    records = {record["name"]: record for record in release["packages"]}
    names = [MODEL_PACKAGE] + sorted(
        platform["package"] for platform in PLATFORMS.values()
    ) + [FACADE_PACKAGE]
    for name in names:
        record = records[name]
        specification = f"{record['name']}@{record['version']}"
        wait_for_integrity(arguments.npm, specification, record["integrity"])
        subprocess.run(
            [arguments.npm, "dist-tag", "add", specification, arguments.tag],
            cwd=ROOT,
            check=True,
        )
        wait_for_dist_tag(
            arguments.npm, record["name"], arguments.tag, record["version"]
        )
        print(json.dumps({"package": specification, "tag": arguments.tag}))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    native = subparsers.add_parser("stage-native")
    native.add_argument("--platform-id", choices=sorted(PLATFORMS), required=True)
    native.add_argument("--build-dir", type=Path, required=True)
    native.add_argument("--configuration", default="Release")
    native.add_argument("--metadata-dir", type=Path, required=True)
    native.add_argument("--output-dir", type=Path, required=True)
    native.add_argument("--runtime-flavor", choices=["cpu", "webgpu"], default="cpu")
    native.add_argument("--webgpu-artifact-manifest", type=Path)
    native.add_argument("--webgpu-compatibility-manifest", type=Path)
    native.add_argument("--qualification-build", action="store_true")
    native.set_defaults(handler=stage_native)

    assembly = subparsers.add_parser("assemble")
    assembly.add_argument("--version", required=True)
    assembly.add_argument("--bundle", type=Path, required=True)
    assembly.add_argument("--native-root", type=Path, required=True)
    assembly.add_argument("--output-dir", type=Path, required=True)
    assembly.set_defaults(handler=assemble)

    packing = subparsers.add_parser("pack")
    packing.add_argument("--staging-dir", type=Path, required=True)
    packing.add_argument("--output-dir", type=Path, required=True)
    packing.add_argument("--npm", default="npm")
    packing.set_defaults(handler=pack)

    publishing = subparsers.add_parser("publish")
    publishing.add_argument("--tarball-dir", type=Path, required=True)
    publishing.add_argument("--phase", choices=["dependencies", "facade"], required=True)
    publishing.add_argument("--tag", default="next")
    publishing.add_argument("--npm", default="npm")
    publishing.set_defaults(handler=publish)

    promotion = subparsers.add_parser("promote")
    promotion.add_argument("--tarball-dir", type=Path, required=True)
    promotion.add_argument("--tag", default="latest")
    promotion.add_argument("--expected-version")
    promotion.add_argument("--npm", default="npm")
    promotion.set_defaults(handler=promote)

    arguments = parser.parse_args()
    arguments.handler(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
