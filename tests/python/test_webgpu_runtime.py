from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "webgpu" / "build_runtime.py"
SPEC = importlib.util.spec_from_file_location("webgpu_build_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
build_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_runtime)


def locked() -> dict[str, object]:
    return json.loads((ROOT / "tools" / "webgpu" / "runtime-lock.json").read_text("utf-8"))


class WebGpuRuntimeContractTest(unittest.TestCase):
    def test_committed_lock_is_valid(self) -> None:
        build_runtime.validate_lock(locked())

    def test_lock_rejects_a_non_vulkan_backend(self) -> None:
        lock = copy.deepcopy(locked())
        lock["topology"]["graphicsBackend"] = "D3D12"
        with self.assertRaisesRegex(build_runtime.ContractError, "must use Vulkan"):
            build_runtime.validate_lock(lock)

    def test_lock_rejects_frozen_contract_mutations(self) -> None:
        mutations = [
            (
                "Dawn archive SHA-1",
                lambda lock: lock["sources"]["dawn"].__setitem__(
                    "archiveSha1", "0" * 40
                ),
                "sources.dawn",
            ),
            (
                "ONNX Runtime URL",
                lambda lock: lock["sources"]["onnxruntime"].__setitem__(
                    "url", "https://example.invalid/onnxruntime.git"
                ),
                "sources.onnxruntime",
            ),
            (
                "upstream arguments",
                lambda lock: lock["build"]["upstreamArguments"].append(
                    "--disable_webgpu"
                ),
                "build.upstreamArguments",
            ),
            (
                "dependency allowlist",
                lambda lock: lock["artifacts"]["allowedDynamicDependencies"].append(
                    "libcuda.so.1"
                ),
                "artifacts.allowedDynamicDependencies",
            ),
            (
                "qualification status",
                lambda lock: lock["qualification"].__setitem__(
                    "status", "qualified"
                ),
                "qualification",
            ),
            (
                "production hash status",
                lambda lock: lock["artifacts"].__setitem__(
                    "productionHashStatus", "qualified"
                ),
                "artifacts.productionHashStatus",
            ),
            (
                "production hash claim",
                lambda lock: lock["artifacts"].__setitem__(
                    "productionSha256", "0" * 64
                ),
                "artifacts.productionSha256",
            ),
            (
                "missing production hash",
                lambda lock: lock["artifacts"].pop("productionSha256"),
                "artifacts.productionSha256",
            ),
            (
                "missing plugin marker",
                lambda lock: lock["topology"].pop("plugin"),
                "topology.plugin",
            ),
            (
                "schema version boolean",
                lambda lock: lock.__setitem__("schemaVersion", True),
                "schemaVersion",
            ),
            (
                "provider gate integer",
                lambda lock: lock["qualification"].__setitem__(
                    "providerGatePassed", 0
                ),
                "qualification",
            ),
            (
                "evidence hash authority integer",
                lambda lock: lock["qualification"].__setitem__(
                    "evidenceArtifactHashIsProductionLock", 0
                ),
                "qualification",
            ),
        ]
        for name, mutate, error in mutations:
            with self.subTest(name=name):
                lock = copy.deepcopy(locked())
                mutate(lock)
                with self.assertRaisesRegex(build_runtime.ContractError, error):
                    build_runtime.validate_lock(lock)

    def test_host_validation_rejects_non_linux(self) -> None:
        with self.assertRaisesRegex(build_runtime.ContractError, "requires Linux"):
            build_runtime.validate_host(
                locked(), system="Darwin", machine="x86_64", libc="glibc 2.39"
            )

    def test_source_validation_rejects_exact_commit_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / ".git").mkdir()
            outputs = iter(["true", "0" * 40])
            with mock.patch.object(build_runtime, "git_output", side_effect=outputs):
                with self.assertRaisesRegex(
                    build_runtime.ContractError, "ONNX Runtime commit mismatch"
                ):
                    build_runtime.validate_source(source, locked())

    def test_source_validation_rejects_dirty_checkout(self) -> None:
        lock = locked()
        expected = lock["sources"]["onnxruntime"]["commit"]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / ".git").mkdir()
            (source / "VERSION_NUMBER").write_text("1.23.0\n", "utf-8")
            (source / "cmake").mkdir()
            dawn = lock["sources"]["dawn"]
            (source / "cmake" / "deps.txt").write_text(
                f"dawn;{dawn['url']};{dawn['archiveSha1']}\n", "utf-8"
            )
            outputs = iter(["true", expected, " M tracked-file"])
            with mock.patch.object(build_runtime, "git_output", side_effect=outputs):
                with self.assertRaisesRegex(build_runtime.ContractError, "must be clean"):
                    build_runtime.validate_source(source, lock)

    def test_source_validation_wraps_missing_contract_files(self) -> None:
        lock = locked()
        expected = lock["sources"]["onnxruntime"]["commit"]
        cases = [
            ("version", None, "cannot read ONNX Runtime version file"),
            ("dependencies", "1.23.0\n", "cannot read ONNX Runtime dependency lock"),
        ]
        for name, version, error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                if version is not None:
                    (source / "VERSION_NUMBER").write_text(version, "utf-8")
                outputs = iter(["true", expected, ""])
                with mock.patch.object(build_runtime, "git_output", side_effect=outputs):
                    with self.assertRaisesRegex(build_runtime.ContractError, error):
                        build_runtime.validate_source(source, lock)

    def test_artifact_validation_accepts_locked_dynamic_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libonnxruntime.so.1.23.0"
            library.write_bytes(b"runtime")
            dynamic = """
 0x000000000000000e (SONAME) Library soname: [libonnxruntime.so.1]
 0x000000000000001d (RUNPATH) Library runpath: [$ORIGIN]
 0x0000000000000001 (NEEDED) Shared library: [libstdc++.so.6]
 0x0000000000000001 (NEEDED) Shared library: [libm.so.6]
 0x0000000000000001 (NEEDED) Shared library: [libgcc_s.so.1]
 0x0000000000000001 (NEEDED) Shared library: [libc.so.6]
 0x0000000000000001 (NEEDED) Shared library: [ld-linux-x86-64.so.2]
"""
            details = build_runtime.validate_artifact(
                library, locked(), dynamic_section=dynamic
            )
            self.assertEqual(details["bytes"], 7)
            self.assertEqual(details["soname"], "libonnxruntime.so.1")

    def test_artifact_validation_rejects_invalid_search_paths(self) -> None:
        invalid_sections = [
            (
                "RPATH and RUNPATH coexist",
                "(RPATH) [$ORIGIN]\n (RUNPATH) [$ORIGIN]",
                "must not contain DT_RPATH",
            ),
            (
                "multiple RUNPATH tags",
                "(RUNPATH) [$ORIGIN]\n (RUNPATH) [$ORIGIN]",
                "exactly one DT_RUNPATH",
            ),
            (
                "multiple RUNPATH elements",
                "(RUNPATH) [$ORIGIN:/tmp]",
                "exactly one DT_RUNPATH",
            ),
            (
                "empty RUNPATH element",
                "(RUNPATH) [$ORIGIN:]",
                "exactly one DT_RUNPATH",
            ),
            (
                "empty RUNPATH",
                "(RUNPATH) []",
                "exactly one DT_RUNPATH",
            ),
        ]
        for name, path_tags, error in invalid_sections:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                library = Path(directory) / "libonnxruntime.so.1.23.0"
                library.write_bytes(b"runtime")
                dynamic = (
                    "(SONAME) [libonnxruntime.so.1]\n"
                    f" {path_tags}\n"
                    " (NEEDED) [libc.so.6]\n"
                )
                with self.assertRaisesRegex(build_runtime.ContractError, error):
                    build_runtime.validate_artifact(
                        library, locked(), dynamic_section=dynamic
                    )

    def test_artifact_validation_rejects_unlocked_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libonnxruntime.so.1.23.0"
            library.write_bytes(b"runtime")
            dynamic = """
 (SONAME) [libonnxruntime.so.1]
 (RUNPATH) [$ORIGIN]
 (NEEDED) [libcuda.so.1]
"""
            with self.assertRaisesRegex(
                build_runtime.ContractError, "unexpected dynamic dependencies"
            ):
                build_runtime.validate_artifact(
                    library, locked(), dynamic_section=dynamic
                )

    def test_stage_is_atomic_and_creates_relative_symlinks(self) -> None:
        lock = locked()
        expected = lock["sources"]["onnxruntime"]["commit"]
        artifact_details = {
            "bytes": 7,
            "sha256": "00" * 32,
            "soname": "libonnxruntime.so.1",
            "runpath": ["$ORIGIN"],
            "dynamicDependencies": ["libc.so.6"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            library = root / "built.so"
            library.write_bytes(b"runtime")
            output = root / "output"
            with mock.patch.object(
                build_runtime, "validate_artifact", return_value=artifact_details
            ), mock.patch.object(build_runtime, "git_output", return_value=expected):
                manifest_path = build_runtime.stage_artifact(
                    library, output, source, ["python3", "build.py"], lock
                )
            self.assertEqual(manifest_path, output / "artifact-manifest.json")
            self.assertEqual(
                (output / "libonnxruntime.so").readlink(), Path("libonnxruntime.so.1")
            )
            self.assertEqual(
                (output / "libonnxruntime.so.1").readlink(),
                Path("libonnxruntime.so.1.23.0"),
            )
            manifest = json.loads(manifest_path.read_text("utf-8"))
            self.assertEqual(
                manifest["qualification"]["status"], "proof-of-concept-only"
            )
            self.assertEqual(
                manifest["qualification"]["evidenceId"],
                lock["qualification"]["evidenceId"],
            )
            self.assertFalse(manifest["qualification"]["providerGatePassed"])
            self.assertEqual(
                manifest["qualification"]["productionHashStatus"], "pending"
            )
            self.assertFalse(
                manifest["qualification"]["productionArtifactQualified"]
            )
            self.assertEqual(manifest["sources"]["onnxruntimeCommit"], expected)

    def test_stage_failure_does_not_publish_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "built.so"
            library.write_bytes(b"runtime")
            output = root / "output"
            with mock.patch.object(
                build_runtime,
                "validate_artifact",
                side_effect=build_runtime.ContractError("bad artifact"),
            ):
                with self.assertRaisesRegex(build_runtime.ContractError, "bad artifact"):
                    build_runtime.stage_artifact(
                        library, output, root, ["build"], locked()
                    )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".output.*")), [])


if __name__ == "__main__":
    unittest.main()
