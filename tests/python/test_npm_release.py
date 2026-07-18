from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform as host_platform
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import npm_release


class NpmReleaseTests(unittest.TestCase):
    def test_multi_config_build_file_is_configuration_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            (build / "bin" / "Release").mkdir(parents=True)
            (build / "bin" / "light_ocr_node.node").write_bytes(b"stale")
            expected = build / "bin" / "Release" / "light_ocr_node.node"
            expected.write_bytes(b"release")
            (build / "CMakeCache.txt").write_text(
                "CMAKE_CONFIGURATION_TYPES:STRING=Debug;Release\n", "utf-8"
            )

            self.assertEqual(
                npm_release.build_file(build, "light_ocr_node.node", "Release"),
                expected,
            )
            with self.assertRaisesRegex(RuntimeError, "Debug build output is missing"):
                npm_release.build_file(build, "light_ocr_node.node", "Debug")

    def test_rejects_a_pre_tiled_package_version(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "0.2.0 or newer"):
            npm_release.assemble(
                argparse.Namespace(
                    version="0.1.1",
                    bundle=Path("unused"),
                    native_root=Path("unused"),
                    output_dir=Path("unused"),
                )
            )

    def test_rejects_a_version_that_does_not_match_the_source(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not match source version"):
            npm_release.assemble(
                argparse.Namespace(
                    version="0.2.2",
                    bundle=Path("unused"),
                    native_root=Path("unused"),
                    output_dir=Path("unused"),
                )
            )

    @mock.patch("tools.npm_release.subprocess.run")
    def test_registry_lookup_bypasses_stale_npm_metadata(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='"sha512-release-integrity"\n', stderr=""
        )

        integrity = npm_release.npm_integrity("npm", "@arcships/light-ocr@0.1.0")

        self.assertEqual(integrity, "sha512-release-integrity")
        command = run.call_args.args[0]
        self.assertIn("--prefer-online", command)
        self.assertIn(f"--registry={npm_release.NPM_REGISTRY}", command)

    @mock.patch("tools.npm_release.time.sleep")
    @mock.patch("tools.npm_release.npm_dist_tag")
    def test_dist_tag_verification_waits_for_registry_convergence(
        self, dist_tag: mock.Mock, sleep: mock.Mock
    ) -> None:
        dist_tag.side_effect = ["0.1.0", "0.2.0"]

        npm_release.wait_for_dist_tag(
            "npm", "@arcships/light-ocr", "latest", "0.2.0"
        )

        self.assertEqual(dist_tag.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_stages_and_deterministically_packs_six_packages(self) -> None:
        npm = shutil.which("npm")
        if npm is None:
            self.skipTest("npm is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build" / "bin"
            build.mkdir(parents=True)
            (build / "light_ocr_node.node").write_bytes(b"native-addon")
            for platform in npm_release.PLATFORMS.values():
                (build / platform["runtime"]).write_bytes(platform["runtime"].encode())

            metadata = root / "metadata"
            (metadata / "licenses").mkdir(parents=True)
            (metadata / "licenses" / "dependency.txt").write_text("license\n", "utf-8")
            (metadata / "license-inventory.json").write_text(
                '{"schemaVersion":"1.0","files":[]}\n', "utf-8"
            )
            (metadata / "sbom.spdx.json").write_text(
                '{"spdxVersion":"SPDX-2.3"}\n', "utf-8"
            )

            native_root = root / "native"
            for platform_id in npm_release.PLATFORMS:
                npm_release.stage_native(
                    argparse.Namespace(
                        platform_id=platform_id,
                        build_dir=build.parent,
                        metadata_dir=metadata,
                        output_dir=native_root / platform_id,
                    )
                )
            for platform_id in ("macos-arm64", "macos-x64"):
                descriptor = json.loads(
                    (native_root / platform_id / "native" / "runtime-descriptor.json")
                    .read_text("utf-8")
                )
                self.assertEqual(
                    descriptor["autoPolicy"]["providers"], ["apple", "cpu"]
                )
                self.assertIn("apple", descriptor["providers"])

            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps({
                    "schemaVersion": "1.1",
                    "bundleId": npm_release.BUNDLE_ID,
                    "normalizedConfigPath": "normalized-config.json",
                    "providers": {
                        "apple": {
                            "schemaVersion": "1.1",
                            "devicePolicy": "open-macos",
                            "architectures": ["arm64", "x86_64"],
                            "validatedDeviceFamilies": ["Apple M4"],
                        }
                    },
                }) + "\n", "utf-8"
            )
            (bundle / "normalized-config.json").write_text(
                json.dumps({
                    "schemaVersion": "1.2",
                    "runtimeProfiles": {
                        "tiled": {"contractVersion": "tiled-v1"}
                    },
                }) + "\n", "utf-8"
            )

            staging = root / "staging"
            npm_release.assemble(
                argparse.Namespace(
                    version="0.2.1",
                    bundle=bundle,
                    native_root=native_root,
                    output_dir=staging,
                )
            )
            facade = json.loads((staging / "facade" / "package.json").read_text("utf-8"))
            self.assertEqual(facade["dependencies"][npm_release.MODEL_PACKAGE], "0.2.1")
            self.assertEqual(len(facade["optionalDependencies"]), 4)
            model = json.loads(
                (staging / "model-ppocrv6-small" / "package.json").read_text("utf-8")
            )
            self.assertEqual(model["lightOcr"]["manifestSchemaVersion"], "1.1")
            self.assertEqual(
                model["lightOcr"]["normalizedConfigSchemaVersion"], "1.2"
            )
            self.assertEqual(model["lightOcr"]["tiledContractVersion"], "tiled-v1")

            tarballs = root / "tarballs"
            npm_release.pack(
                argparse.Namespace(staging_dir=staging, output_dir=tarballs, npm=npm)
            )
            release = json.loads((tarballs / "release-manifest.json").read_text("utf-8"))
            self.assertEqual(release["version"], "0.2.1")
            self.assertEqual(len(release["packages"]), 6)
            self.assertEqual(len(list(tarballs.glob("*.tgz"))), 6)

            machine = host_platform.machine().lower()
            if host_platform.system() == "Darwin" and machine in {"arm64", "aarch64"}:
                platform_id = "macos-arm64"
            elif host_platform.system() == "Darwin" and machine in {"x86_64", "amd64"}:
                platform_id = "macos-x64"
            elif host_platform.system() == "Linux" and machine in {"x86_64", "amd64"}:
                platform_id = "linux-x64"
            elif host_platform.system() == "Windows" and machine in {"x86_64", "amd64"}:
                platform_id = "windows-x64"
            else:
                return
            filenames = {record["name"]: record["filename"] for record in release["packages"]}
            native_name = npm_release.PLATFORMS[platform_id]["package"]
            consumer = root / "consumer"
            consumer.mkdir()
            (consumer / "package.json").write_text(
                '{"name":"package-smoke","version":"1.0.0","private":true}\n', "utf-8"
            )
            subprocess.run(
                [
                    npm,
                    "install",
                    "--offline",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--package-lock=false",
                    str(tarballs / filenames[npm_release.MODEL_PACKAGE]),
                    str(tarballs / filenames[native_name]),
                    str(tarballs / filenames[npm_release.FACADE_PACKAGE]),
                ],
                cwd=consumer,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue((consumer / "node_modules/@arcships/light-ocr/package.json").is_file())

    def test_runtime_descriptor_rejects_mutated_payload_and_qualification_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "native"
            native.mkdir()
            addon = native / "light_ocr_node.node"
            runtime = native / "libonnxruntime.so.1"
            addon.write_bytes(b"addon")
            runtime.write_bytes(b"runtime")
            descriptor = {
                "schemaVersion": "1.0",
                "platform": {
                    "id": "linux-x64",
                    "os": "linux",
                    "architecture": "x86_64",
                    "libc": "glibc",
                },
                "runtime": {
                    "flavor": "cpu",
                    "kind": "onnxruntime-cpu",
                    "version": "1.22.0",
                    "abi": "onnxruntime-c-api-22",
                },
                "qualificationOnly": False,
                "released": True,
                "autoPolicy": {
                    "id": "linux-x64-v1",
                    "version": 1,
                    "providers": ["cpu"],
                },
                "providers": {
                    "cpu": {
                        "runtimeProvider": "CPUExecutionProvider",
                        "qualificationId": "cpu-baseline-v1",
                        "artifacts": [npm_release.file_record(runtime, root)],
                    }
                },
                "addon": npm_release.file_record(addon, root),
            }
            npm_release.validate_runtime_descriptor(
                descriptor, root, platform_id="linux-x64", require_released=True
            )
            runtime.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "(?:byte count|hash) mismatch"):
                npm_release.validate_runtime_descriptor(descriptor, root)
            runtime.write_bytes(b"runtime")
            descriptor["qualificationOnly"] = True
            descriptor["released"] = False
            with self.assertRaisesRegex(RuntimeError, "cannot enter npm release"):
                npm_release.validate_runtime_descriptor(
                    descriptor, root, require_released=True
                )

    def test_rejects_pending_webgpu_release_but_stages_qualification_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build" / "bin"
            build.mkdir(parents=True)
            (build / "light_ocr_node.node").write_bytes(b"addon")
            artifact_root = root / "webgpu"
            library = artifact_root / "lib" / "libonnxruntime.so.1.23.0"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"webgpu-runtime")
            header_names = [
                "onnxruntime_c_api.h",
                "onnxruntime_cxx_api.h",
                "onnxruntime_cxx_inline.h",
                "onnxruntime_float16.h",
                "onnxruntime_run_options_config_keys.h",
                "onnxruntime_session_options_config_keys.h",
            ]
            headers = artifact_root / "include"
            headers.mkdir()
            header_records = []
            for name in header_names:
                header = headers / name
                header.write_text(f"// {name}\n", "utf-8")
                header_records.append(npm_release.file_record(header, artifact_root))
            manifest = {
                "contractId": "linux-x64-gnu-webgpu-ort-1.23.0-monolithic-v1",
                "runtimeFlavor": "webgpu",
                "artifact": {
                    "filename": "lib/libonnxruntime.so.1.23.0",
                    "bytes": library.stat().st_size,
                    "sha256": npm_release.sha256(library),
                },
                "headers": {
                    "directory": "include",
                    "onnxruntimeVersion": "1.23.0",
                    "onnxruntimeCommit": "be835efc56aca19b8e810538ec93c8e150e0fc61",
                    "files": header_records,
                },
                "sessionOptions": {
                    "providerName": "WebGPU",
                    "providerOptions": {
                        "dawnBackendType": "Vulkan",
                        "preferredLayout": "NHWC",
                        "enableGraphCapture": "0",
                        "validationMode": "basic",
                    },
                    "deviceIdSupported": False,
                },
                "qualification": {
                    "evidenceId": "poc-only",
                    "providerGatePassed": False,
                    "productionHashStatus": "pending",
                    "productionSha256": None,
                    "productionArtifactQualified": False,
                },
            }
            manifest_path = artifact_root / "artifact-manifest.json"
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            compatibility = artifact_root / "compatibility.json"
            valid_compatibility = {
                "schemaVersion": "1.0",
                "provider": "webgpu",
                "platformId": "linux-x64",
                "runtimeVersion": "1.23.0",
                "runtimeAbi": "onnxruntime-c-api-23",
                "qualificationId": "poc-only",
                "qualificationOnly": True,
                "released": False,
                "runtimeArtifact": {
                    "bytes": library.stat().st_size,
                    "sha256": npm_release.sha256(library),
                },
            }
            compatibility.write_text(
                json.dumps(valid_compatibility) + "\n", "utf-8"
            )
            metadata = root / "metadata"
            (metadata / "licenses").mkdir(parents=True)
            (metadata / "licenses" / "notice.txt").write_text("notice", "utf-8")
            (metadata / "license-inventory.json").write_text("{}\n", "utf-8")
            (metadata / "sbom.spdx.json").write_text("{}\n", "utf-8")
            arguments = argparse.Namespace(
                platform_id="linux-x64",
                build_dir=build.parent,
                metadata_dir=metadata,
                output_dir=root / "output",
                runtime_flavor="webgpu",
                webgpu_artifact_manifest=manifest_path,
                webgpu_compatibility_manifest=compatibility,
                qualification_build=False,
            )
            with self.assertRaisesRegex(RuntimeError, "qualified production artifact"):
                npm_release.stage_native(arguments)
            manifest["qualification"].update(
                {
                    "providerGatePassed": True,
                    "productionHashStatus": "qualified",
                    "productionArtifactQualified": False,
                    "productionSha256": npm_release.sha256(library),
                }
            )
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            with self.assertRaisesRegex(RuntimeError, "qualified production artifact"):
                npm_release.stage_native(arguments)
            manifest["qualification"].update(
                {
                    "providerGatePassed": False,
                    "productionHashStatus": "pending",
                    "productionArtifactQualified": False,
                    "productionSha256": None,
                }
            )
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            arguments.qualification_build = True
            for field, invalid_value in (
                ("provider", "cpu"),
                ("platformId", "windows-x64"),
                ("runtimeAbi", "onnxruntime-c-api-22"),
                ("qualificationId", "other-evidence"),
                ("released", True),
            ):
                with self.subTest(compatibility_field=field):
                    mutated = dict(valid_compatibility)
                    mutated[field] = invalid_value
                    compatibility.write_text(json.dumps(mutated) + "\n", "utf-8")
                    with self.assertRaisesRegex(
                        RuntimeError, "compatibility manifest does not match"
                    ):
                        npm_release.stage_native(arguments)
            mutated = dict(valid_compatibility)
            mutated["runtimeArtifact"] = dict(valid_compatibility["runtimeArtifact"])
            mutated["runtimeArtifact"]["sha256"] = "0" * 64
            compatibility.write_text(json.dumps(mutated) + "\n", "utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "compatibility manifest does not match"
            ):
                npm_release.stage_native(arguments)
            compatibility.write_text(
                json.dumps(valid_compatibility) + "\n", "utf-8"
            )
            npm_release.stage_native(arguments)
            descriptor = json.loads(
                (arguments.output_dir / "native" / "runtime-descriptor.json").read_text("utf-8")
            )
            self.assertTrue(descriptor["qualificationOnly"])
            self.assertFalse(descriptor["released"])
            self.assertEqual(descriptor["autoPolicy"]["providers"], ["cpu"])
            self.assertIn("webgpu", descriptor["providers"])
            compatibility_record = descriptor["providers"]["webgpu"][
                "compatibilityManifest"
            ]
            self.assertEqual(
                compatibility_record["path"], "native/webgpu-compatibility.json"
            )
            self.assertIn(
                compatibility_record,
                descriptor["providers"]["webgpu"]["artifacts"],
            )
            with self.assertRaisesRegex(RuntimeError, "cannot enter npm release"):
                npm_release.validate_runtime_descriptor(
                    descriptor, arguments.output_dir, require_released=True
                )


if __name__ == "__main__":
    unittest.main()
