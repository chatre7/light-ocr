#!/usr/bin/env python3
"""Generate or verify immutable tiled-v1 Python oracle stage records."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform

from tiled_ground_truth import canonical, verify_tiled_ground_truth
from tiled_oracle import run


ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def golden_bytes(bundle: Path, fixture_path: Path) -> bytes:
    fixture = json.loads(fixture_path.read_text("utf-8"))
    expected = run(
        bundle, fixture_path.parent / "pixels.bin", fixture["width"], fixture["height"],
        fixture["stride"], fixture["pixelFormat"], include_crop_pixels=True,
    )
    record = {
        "schemaVersion": "1.0",
        "profile": "tiled_v1",
        "fixtureId": fixture["id"],
        "corpusRevision": fixture["corpusRevision"],
        "pixelSha256": fixture["pixelSha256"],
        "modelBundleId": expected["modelBundleId"],
        "oracleEnvironmentLockSha256": file_sha256(ROOT / "oracle" / "oracle.lock.json"),
        "tiledOracleSourceSha256": file_sha256(ROOT / "oracle" / "tiled_oracle.py"),
        "plannerMergeSourceSha256": file_sha256(ROOT / "oracle" / "tiled.py"),
        "expected": expected,
    }
    return canonical(record) + b"\n"


def identity(bundle: Path, ground_truth_lock: Path) -> dict[str, str]:
    manifest = json.loads((bundle / "manifest.json").read_text("utf-8"))
    return {
        "modelBundleId": manifest["bundleId"],
        "bundleManifestSha256": file_sha256(bundle / "manifest.json"),
        "groundTruthLockSha256": file_sha256(ground_truth_lock),
        "oracleEnvironmentLockSha256": file_sha256(ROOT / "oracle" / "oracle.lock.json"),
        "tiledOracleSourceSha256": file_sha256(ROOT / "oracle" / "tiled_oracle.py"),
        "plannerMergeSourceSha256": file_sha256(ROOT / "oracle" / "tiled.py"),
    }


def verify_lock(
    bundle: Path, fixtures: Path, output: Path, lock_path: Path,
    ground_truth_lock: Path,
) -> dict[str, object]:
    verify_tiled_ground_truth(fixtures, ground_truth_lock)
    lock = json.loads(lock_path.read_text("utf-8"))
    if lock.get("schemaVersion") != "1.0" or lock.get("profile") != "tiled_v1":
        raise RuntimeError("unsupported tiled golden lock identity")
    for key, value in identity(bundle, ground_truth_lock).items():
        if lock.get(key) != value:
            raise RuntimeError(f"tiled golden identity is stale: {key}")
    records = lock.get("fixtures", [])
    if len(records) != 8 or len({value["fixtureId"] for value in records}) != 8:
        raise RuntimeError("tiled golden fixture matrix is incomplete")
    for record in records:
        path = output / record["path"]
        data = path.read_bytes()
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise RuntimeError(f"tiled golden file lock mismatch: {record['fixtureId']}")
        golden = json.loads(data)
        fixture = json.loads((fixtures / record["fixtureId"] / "fixture.json").read_text("utf-8"))
        if (
            golden["fixtureId"] != fixture["id"]
            or golden["corpusRevision"] != fixture["corpusRevision"]
            or golden["pixelSha256"] != fixture["pixelSha256"]
            or golden["modelBundleId"] != lock["modelBundleId"]
        ):
            raise RuntimeError(f"tiled golden fixture identity is stale: {record['fixtureId']}")
    return lock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--fixtures", type=Path,
        default=ROOT / "corpus" / "tiled-v1" / "fixtures",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "corpus" / "tiled-v1" / "goldens",
    )
    parser.add_argument(
        "--lock", type=Path,
        default=ROOT / "corpus" / "tiled-v1" / "goldens.lock.json",
    )
    parser.add_argument(
        "--ground-truth-lock", type=Path,
        default=ROOT / "corpus" / "tiled-v1" / "ground-truth.lock.json",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify-lock-only", action="store_true")
    mode.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    if arguments.write:
        fixtures = verify_tiled_ground_truth(arguments.fixtures, arguments.ground_truth_lock)
        arguments.output.mkdir(parents=True, exist_ok=True)
        records = []
        for fixture in fixtures:
            data = golden_bytes(
                arguments.bundle,
                arguments.fixtures / fixture["id"] / "fixture.json",
            )
            path = arguments.output / f"{fixture['id']}.json"
            path.write_bytes(data)
            records.append({
                "fixtureId": fixture["id"], "path": path.name,
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            })
        lock = {
            "schemaVersion": "1.0", "profile": "tiled_v1",
            "canonicalPlatform": platform.platform(),
            **identity(arguments.bundle, arguments.ground_truth_lock),
            "fixtures": records,
        }
        arguments.lock.write_bytes(canonical(lock) + b"\n")
    else:
        lock = verify_lock(
            arguments.bundle, arguments.fixtures, arguments.output, arguments.lock,
            arguments.ground_truth_lock,
        )
        if arguments.verify:
            for record in lock["fixtures"]:
                expected = (arguments.output / record["path"]).read_bytes()
                actual = golden_bytes(
                    arguments.bundle,
                    arguments.fixtures / record["fixtureId"] / "fixture.json",
                )
                if actual != expected:
                    raise RuntimeError(f"tiled golden replay mismatch: {record['fixtureId']}")
    print(json.dumps({"fixtureCount": 8, "passed": True, "profile": "tiled_v1"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
