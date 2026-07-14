#!/usr/bin/env python3
"""Run the hard tiled-v1 ground-truth and determinism release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import unicodedata
from typing import Any

from run_quality import detection_match, edit_distance
from tiled_ground_truth import canonical, sha256, verify_tiled_ground_truth


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def stable_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "modelBundleId": result["modelBundleId"],
        "lines": result["lines"],
        "diagnostics": result["diagnostics"],
    }


def run_native(
    binary: Path, bundle: Path, fixture_path: Path, diagnostics: bool
) -> dict[str, Any]:
    fixture = json.loads(fixture_path.read_text("utf-8"))
    command = [
        str(binary), "--bundle", str(bundle), "--pixels", str(fixture_path.parent / "pixels.bin"),
        "--width", str(fixture["width"]), "--height", str(fixture["height"]),
        "--stride", str(fixture["stride"]), "--format", fixture["pixelFormat"],
        "--profile", "tiled_v1",
    ]
    if diagnostics:
        command.append("--diagnostics")
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if process.returncode != 0:
        raise RuntimeError(f"native tiled quality run failed: {process.stdout}{process.stderr}")
    result = json.loads(process.stdout)
    if not result.get("ok"):
        raise RuntimeError(f"native tiled quality result failed: {result}")
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-validate", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--fixtures", type=Path,
        default=root / "corpus" / "tiled-v1" / "fixtures",
    )
    parser.add_argument(
        "--ground-truth-lock", type=Path,
        default=root / "corpus" / "tiled-v1" / "ground-truth.lock.json",
    )
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.repetitions < 1:
        raise RuntimeError("tiled quality repetitions must be positive")
    fixtures = verify_tiled_ground_truth(arguments.fixtures, arguments.ground_truth_lock)
    records = []
    total_expected = 0
    total_observed = 0
    total_errors = 0
    total_characters = 0
    total_suppressed = 0
    model_bundle_id = None
    for fixture in fixtures:
        fixture_path = arguments.fixtures / fixture["id"] / "fixture.json"
        results = [
            run_native(arguments.native_validate, arguments.bundle, fixture_path, True)
            for _ in range(arguments.repetitions)
        ]
        without_diagnostics = run_native(
            arguments.native_validate, arguments.bundle, fixture_path, False
        )
        first = results[0]
        model_bundle_id = model_bundle_id or first["modelBundleId"]
        expected_annotations = fixture["annotations"]
        expected_text = [normalized(item["text"]) for item in expected_annotations]
        observed_text = [normalized(item["text"]) for item in first["lines"]]
        matches = detection_match(
            [item["box"] for item in first["lines"]],
            [item["quad"] for item in expected_annotations],
        )
        expected_sequence = "\n".join(expected_text)
        observed_sequence = "\n".join(observed_text)
        character_errors = edit_distance(expected_sequence, observed_sequence)
        stable_hashes = [sha256(canonical(stable_result(result))) for result in results]
        diagnostics = first["diagnostics"]
        passes = diagnostics["detectionPasses"]
        plan = [
            [item["tileOrdinal"], *item["roi"]]
            for item in passes
        ]
        matched_order = [item["predictedIndex"] for item in matches["matches"]]
        checks = {
            "exactTextAndReadingOrder": observed_text == expected_text,
            "oneToOnePolygonMatch": (
                matches["truePositives"] == len(expected_annotations)
                and matches["falsePositives"] == 0
                and matches["falseNegatives"] == 0
                and matched_order == list(range(len(expected_annotations)))
            ),
            "characterErrorRateZero": character_errors == 0,
            "candidateAccounting": (
                diagnostics["rawDetectionBoxes"]
                - diagnostics["suppressedDuplicateBoxes"]
                == diagnostics["acceptedBoxes"]
                == len(first["lines"])
            ),
            "tilePlan": plan == [
                [0, 0, 0, 1280, 1280],
                [1, 768, 0, 1280, 1280],
                [2, 0, 768, 1280, 1280],
                [3, 768, 768, 1280, 1280],
            ],
            "singleLivePass": diagnostics["maxLiveDetectionPassBuffers"] == 1,
            "boundedPassShapes": all(
                item["tensorShape"][2] <= 1280 and item["tensorShape"][3] <= 1280
                for item in passes
            ),
            "diagnosticsInvariant": without_diagnostics["lines"] == first["lines"],
            "tenRunDeterminism": len(set(stable_hashes)) == 1,
        }
        passed = all(checks.values())
        records.append({
            "fixtureId": fixture["id"],
            "pixelSha256": fixture["pixelSha256"],
            "expectedLines": len(expected_annotations),
            "observedLines": len(first["lines"]),
            "characterErrors": character_errors,
            "detection": matches,
            "rawBoxes": diagnostics["rawDetectionBoxes"],
            "suppressedDuplicates": diagnostics["suppressedDuplicateBoxes"],
            "acceptedBoxes": diagnostics["acceptedBoxes"],
            "stableResultSha256": stable_hashes[0],
            "repetitions": arguments.repetitions,
            "checks": checks,
            "passed": passed,
        })
        total_expected += len(expected_annotations)
        total_observed += len(first["lines"])
        total_errors += character_errors
        total_characters += len(expected_sequence)
        total_suppressed += diagnostics["suppressedDuplicateBoxes"]
    passed = all(record["passed"] for record in records)
    report = {
        "schema": "light-ocr-tiled-quality-report/1.0",
        "passed": passed,
        "contractVersion": "tiled-v1",
        "modelBundleId": model_bundle_id,
        "groundTruthLockSha256": hashlib.sha256(arguments.ground_truth_lock.read_bytes()).hexdigest(),
        "fixtureCount": len(records),
        "expectedLines": total_expected,
        "observedLines": total_observed,
        "characterErrors": total_errors,
        "characterErrorRate": total_errors / total_characters if total_characters else 0.0,
        "duplicateLines": 0 if passed else None,
        "suppressedDuplicateBoxes": total_suppressed,
        "fixtures": records,
    }
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
