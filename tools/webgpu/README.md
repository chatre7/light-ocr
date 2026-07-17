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

The output directory is published atomically and must not already exist. It
contains the versioned library, two relative SONAME/linker-name symlinks, and
`artifact-manifest.json`. The manifest records the actual byte count, SHA-256,
dynamic dependencies, source revisions, and exact upstream build arguments.
Artifact validation fails on an unexpected SONAME, RUNPATH, or dependency.

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
