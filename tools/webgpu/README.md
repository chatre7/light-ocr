# Linux WebGPU runtime product contract

This directory implements **WGPU-001**: a reproducible build contract for the
Linux x86_64 glibc WebGPU runtime. It does not connect WebGPU to light-ocr,
change the current CPU dependency, stage npm packages, or claim that the Linux
Provider Gate has passed.

## Frozen topology

- ONNX Runtime `v1.23.0` at commit
  `be835efc56aca19b8e810538ec93c8e150e0fc61`.
- One monolithic `libonnxruntime` containing the CPU and Native WebGPU
  providers. There is no separate provider plugin.
- Dawn Native revision `9733be39e18186961d503e064874afe3e9ceb8d1`,
  locked by ONNX Runtime's `cmake/deps.txt`, with Vulkan as the only graphics
  backend.
- Release shared library with SONAME `libonnxruntime.so.1`, `$ORIGIN` RUNPATH,
  and the dynamic-dependency allowlist in `runtime-lock.json`.

The earlier inference-only proof of concept used this ORT/Dawn source topology.
Its binary hash remains evidence only: the production hash is deliberately
unset until a release builder produces and qualifies an artifact.

## Prerequisites

The build host must be Linux x86_64 with glibc. It needs Python 3, Git, CMake,
Ninja, `readelf`, a C/C++ toolchain, and the development prerequisites required
by ONNX Runtime and Dawn. Network access is required when the tool fetches the
source and upstream dependency archives. It never fetches a GPU driver; the
host supplies the normal Vulkan loader and driver.

Validate the lock without network access or a build:

```bash
python3 tools/webgpu/build_runtime.py --validate-lock
```

Build with a fresh source checkout under the ignored cache directory:

```bash
python3 tools/webgpu/build_runtime.py \
  --work-dir .cache/webgpu-runtime \
  --output-dir dist/webgpu-runtime \
  --jobs 8
```

A previously checked-out tree can be reused only when it is clean and exactly
at the locked commit:

```bash
python3 tools/webgpu/build_runtime.py \
  --source-dir /path/to/onnxruntime \
  --work-dir .cache/webgpu-runtime \
  --output-dir dist/webgpu-runtime
```

The output directory is published atomically and must not already exist. It is
a matching C++ SDK containing `include/`, the versioned library and two relative
SONAME/linker-name symlinks under `lib/`, plus `artifact-manifest.json`. The
headers and runtime always come from the same exact ONNX Runtime checkout. The
manifest records every regular SDK file's byte count and SHA-256, a deterministic
header-set identity, source revisions, exact upstream build arguments, runtime
flavor, qualification state, and the locked session options. Artifact validation
fails on an unexpected SONAME, RUNPATH, dependency, header, or source identity.

The C++ integration uses the locked generic provider API with provider name
`WebGPU`, Vulkan, NHWC, graph capture disabled, and basic validation. `deviceId`
is intentionally unsupported in the first product contract because ORT 1.23
interprets it as an externally injected WebGPU context ID, not a GPU ordinal.

CMake consumes this SDK only when `LIGHT_OCR_ONNXRUNTIME_FLAVOR=webgpu` and an
explicit `LIGHT_OCR_WEBGPU_SDK_DIR` are supplied. `bootstrap_dependencies.py`
does not download WebGPU from `models/deps.lock.json`; it reports this external,
verified-SDK boundary and points to this builder instead. Native builds verify
glibc from compiler headers. A verified cross toolchain must explicitly set
`LIGHT_OCR_TARGET_LIBC=glibc`; any other value is rejected.

CMake re-hashes every declared public header, verifies both relative symlink
targets, and freezes the exact runtime and session-option identity before use. A
pending artifact is accepted only with `LIGHT_OCR_WEBGPU_QUALIFICATION_BUILD=ON`.
Normal release configuration and npm staging both require the manifest's exact
production hash, `productionArtifactQualified=true`, and accepted Provider Gate;
the current proof-of-concept manifest deliberately cannot satisfy that gate.

## Qualification boundary

The current evidence is one Linux x64 NVIDIA/Vulkan inference-only run. The
detector was fully placed on WebGPU, but recognition placed `Slice.2`,
`Concat.2`, and `Gather` on CPU; recognition therefore fails with
`cpuPartition=forbid`. This contract does not accept that partition for a
Preview and does not replace the required end-to-end, cross-vendor Provider
Gate.

Downstream work must separately add dependency bootstrap/platform filtering,
select the runtime flavor in CMake, support multi-file npm runtime descriptors,
generate license/SBOM metadata without a NuGet assumption, connect provider
creation and D112 Auto selection, and add GPU qualification CI. The current
CPU-only `cmake/Dependencies.cmake` and `models/deps.lock.json` intentionally
remain unchanged in WGPU-001.
