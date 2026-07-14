#!/usr/bin/env python3
"""Run one locked tiled-v1 fixture through native and independent oracle stages."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from tiled_compare import compare_tiled
from tiled_ground_truth import verify_tiled_ground_truth
from tiled_oracle import run


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-probe", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--native-record", type=Path)
    parser.add_argument("--oracle-record", type=Path)
    arguments = parser.parse_args()
    fixture = json.loads(arguments.fixture.read_text("utf-8"))
    fixtures_root = arguments.fixture.parents[1]
    lock_path = fixtures_root.parent / "ground-truth.lock.json"
    verified = {value["id"] for value in verify_tiled_ground_truth(fixtures_root, lock_path)}
    if fixture["id"] not in verified:
        raise RuntimeError("tiled fixture is not in the verified lock")
    pixels = arguments.fixture.parent / "pixels.bin"
    common = [
        "--bundle", str(arguments.bundle), "--pixels", str(pixels),
        "--width", str(fixture["width"]), "--height", str(fixture["height"]),
        "--stride", str(fixture["stride"]), "--format", fixture["pixelFormat"],
        "--profile", "tiled_v1",
    ]
    process = subprocess.run(
        [str(arguments.native_probe), *common], capture_output=True, text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        raise RuntimeError(f"native tiled probe failed: {process.stdout}{process.stderr}")
    native = json.loads(process.stdout)
    oracle = run(
        arguments.bundle, pixels, fixture["width"], fixture["height"],
        fixture["stride"], fixture["pixelFormat"], include_crop_pixels=True,
    )
    exception_path = fixtures_root.parent / "parity-exceptions.json"
    exceptions = []
    if exception_path.is_file():
        exception_document = json.loads(exception_path.read_text("utf-8"))
        if exception_document.get("schemaVersion") != "1.0":
            raise RuntimeError("unsupported tiled parity exception schema")
        exceptions = [
            value for value in exception_document.get("exceptions", [])
            if value["fixtureId"] == fixture["id"]
            and value["platform"] in ("all", __import__("sys").platform)
            and value["expiryContractVersion"] == fixture["contractVersion"]
        ]
    report = compare_tiled(native, oracle, exceptions)
    report.update({
        "fixtureId": fixture["id"],
        "corpusRevision": fixture["corpusRevision"],
        "pixelSha256": fixture["pixelSha256"],
        "profile": "tiled_v1",
        "oracleMode": "live-independent",
        "oracleIdentity": {
            "environmentLockSha256": file_sha256(Path(__file__).with_name("oracle.lock.json")),
            "oracleSourceSha256": file_sha256(Path(__file__).with_name("tiled_oracle.py")),
            "plannerMergeSourceSha256": file_sha256(Path(__file__).with_name("tiled.py")),
        },
        "nativeProbe": {
            "filename": arguments.native_probe.name,
            "sha256": file_sha256(arguments.native_probe),
        },
    })
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    for path, record in ((arguments.native_record, native), (arguments.oracle_record, oracle)):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
