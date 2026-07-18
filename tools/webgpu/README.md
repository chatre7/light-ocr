# Native WebGPU product runtime

This directory owns the reproducible Linux x64 and Windows x64 Native WebGPU
runtime, its release gate, and the real-device qualification runner. Product
integration is complete in the current source candidate; production release is
intentionally blocked until both platform reports are reviewed and bound to the
exact artifact sets in `runtime-lock.json`.

## Frozen runtime

The product uses the official ONNX Runtime plugin topology:

- `Microsoft.ML.OnnxRuntime` `1.24.4` supplies the C/C++ headers and core
  runtime.
- `Microsoft.ML.OnnxRuntime.EP.WebGpu` `0.1.0` supplies the official
  `WebGpuExecutionProvider` plugin built from upstream tag
  `plugin-ep-webgpu/v0.1.0` at commit
  `d2ede0adeb300958cfb5a256c09d27c66c3a6d71`.
- Linux x64 glibc uses Dawn Native over Vulkan. Windows x64 uses Dawn Native
  over D3D12 and carries the plugin's `dxcompiler.dll` and `dxil.dll`.
- The fixed session policy is NHWC, basic validation, graph capture disabled,
  high-performance power preference, FP32, and no public adapter ordinal.

Every NuGet URL, byte count, SHA-512, ZIP member, staged path, license, header,
and platform identity is locked. The assembler rejects duplicate ZIP members,
unsafe paths, symlinks, missing or extra files, hash drift, and mismatched
manifests. It builds no upstream source and never discovers an unpinned system
ORT/plugin.

## Assemble and verify

Online acquisition followed by an offline replay:

```bash
python3 tools/webgpu/build_runtime.py \
  --platform linux-x64 \
  --package-cache .cache/webgpu-runtime/packages \
  --output-dir dist/webgpu-sdk/linux-x64

python3 tools/webgpu/build_runtime.py \
  --validate-sdk dist/webgpu-sdk/linux-x64

python3 tools/webgpu/build_runtime.py \
  --platform windows-x64 \
  --offline \
  --package-cache .cache/webgpu-runtime/packages \
  --output-dir dist/webgpu-sdk/windows-x64
```

The two locked NuGet packages contain both platforms, so one verified package
cache can assemble either SDK on Linux, Windows, or a review host. An output
directory must not already exist and is published atomically only after all
checks pass.

## CMake and package boundary

Use the verified SDK explicitly:

```bash
cmake -S . -B build-webgpu -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLIGHT_OCR_DEPENDENCY_CACHE_DIR="$PWD/.cache/dependencies" \
  -DLIGHT_OCR_ONNXRUNTIME_FLAVOR=webgpu \
  -DLIGHT_OCR_WEBGPU_SDK_DIR="$PWD/dist/webgpu-sdk/linux-x64" \
  -DLIGHT_OCR_WEBGPU_QUALIFICATION_BUILD=ON
```

CMake revalidates the exact SDK inventory and every file hash before exposing
headers or link inputs. A qualification build may consume the current pending
lock. A normal release build requires `production-qualified`, both accepted
Provider Gates, and a platform artifact-set hash matching the assembled SDK.

The staged Node package is self-contained:

- Linux: addon, `libonnxruntime.so.1`, and
  `libonnxruntime_providers_webgpu.so`.
- Windows: addon, `onnxruntime.dll`, WebGPU plugin, `dxcompiler.dll`, and
  `dxil.dll`.
- Both: schema 2 runtime descriptor, per-file bytes/SHA-256, licenses, SPDX
  SBOM, and release artifact hashes.

The loader accepts no undeclared native file or symlink and rechecks the
provider library immediately before registration. Auto is descriptor-owned and
ordered `webgpu -> cpu`; only a typed D112 skippable creation reason may reach
CPU. Explicit WebGPU never falls back. Unknown Dawn/ORT loading failures remain
fatal and are never classified by parsing exception text.

## Hardware-independent checks

```bash
python3 -m unittest \
  tests.python.test_webgpu_runtime \
  tests.python.test_npm_release \
  tests.python.test_webgpu_qualification
```

`.github/workflows/webgpu-native.yml` repeats those tests on Linux and Windows,
assembles the real SDK online and offline, compiles C++ and Node against ORT
1.24.4, runs CPU-only integration coverage, loads the staged addon from a
sterile directory, and generates licenses/SBOM plus a qualification-only native
package. It does not claim physical GPU placement.

## Real-device Provider Gate

Run this command on a Linux x64 glibc GPU host or Windows x64 GPU host from a
clean checkout of the exact candidate revision:

```bash
python3 tools/webgpu/qualify.py
```

The runner bootstraps pinned dependencies/models, assembles and validates the
host SDK, builds the qualification addon and C++ tools, runs hardware-independent
CTest, stages the exact npm payload, and then exercises:

- Node CPU, WebGPU allow, WebGPU strict, and D112 Auto;
- direct C++ D112 Auto and adjacent-plugin discovery;
- the complete locked 14-fixture parity/quality corpus, including sparse,
  dense, multilingual, rotated, handwriting, and low-contrast inputs;
- deterministic repeated inference and 20 engine create/close cycles;
- CPU-vs-WebGPU text, confidence, and box parity;
- per-fixture P95 regression, aggregate P50 speedup, cold-start, and 2 GiB
  resident-memory ceilings fixed before device results are observed;
- ORT profiling evidence for real `WebGpuExecutionProvider` node placement,
  including zero CPU nodes in strict mode.

Outputs are written to
`reports/webgpu-qualification/<platform>/qualification-report.json` with a
sidecar SHA-256, raw cases, profiles, and command logs. A nonzero exit means at
least one Provider Gate failed; the report is still retained.

The production Gate is fixed to all 14 fixtures, at least 10 measured runs per
normal case, and 20 create/close lifecycle cycles. Reduced `--fixture`,
`--iterations`, or `--cycles` values remain useful for diagnosis, but the
resulting report cannot set `passed: true` and cannot qualify a release.

Useful options:

```bash
# Reuse a complete prior build and rerun only device evidence.
python3 tools/webgpu/qualify.py --skip-build

# Fully offline rebuild after caches and Node development files exist.
python3 tools/webgpu/qualify.py --offline

# Use distribution-provided Node headers; Windows also passes node.lib.
python3 tools/webgpu/qualify.py \
  --offline \
  --node-include-dir /absolute/path/to/include/node
```

Windows requires a current D3D12 graphics driver and the Microsoft Visual C++
2015-2022 x64 runtime used by the official binaries. Linux requires an
accessible `/dev/dri/renderD*` node, a working Vulkan loader, and a vendor
driver; an absent render node is classified as typed `adapter_unavailable`
before Dawn is loaded. The Gate records Windows driver identity via
PowerShell/CIM and Linux identity via DRM sysfs (plus `vulkaninfo --summary`
when available); absence of a driver identity fails the report. No CUDA, ROCm,
OpenVINO, Python inference runtime, or source compiler is a product runtime
prerequisite.

Do not edit the pending qualification fields from a successful exit code alone.
Both reports must be reviewed for device identity, placement, quality,
performance, memory, lifecycle, and supported compatibility scope before their
hashes and artifact-set identities can enter the production lock.
