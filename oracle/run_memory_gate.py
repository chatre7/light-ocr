#!/usr/bin/env python3
"""Run a high-resolution native OCR workload in a child process and gate peak RSS."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import cv2

from oracle import load_raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-benchmark", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--target-width", type=int, default=2048)
    parser.add_argument("--target-height", type=int, default=2048)
    parser.add_argument("--maximum-peak-bytes", type=int, required=True)
    parser.add_argument("--minimum-boxes", type=int, default=0)
    parser.add_argument("--maximum-boxes", type=int)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()

    fixture = json.loads(arguments.fixture.read_text("utf-8"))
    source = load_raw(
        arguments.fixture.parent / "pixels.bin",
        fixture["width"],
        fixture["height"],
        fixture["stride"],
        fixture["pixelFormat"],
    )
    resized = cv2.resize(
        source,
        (arguments.target_width, arguments.target_height),
        interpolation=cv2.INTER_LINEAR,
    )
    with tempfile.NamedTemporaryFile(prefix="light-ocr-memory-", suffix=".bgr") as pixels:
        pixels.write(resized.tobytes())
        pixels.flush()
        command = [
            str(arguments.native_benchmark),
            "--bundle", str(arguments.bundle),
            "--pixels", pixels.name,
            "--width", str(arguments.target_width),
            "--height", str(arguments.target_height),
            "--stride", str(arguments.target_width * 3),
            "--format", "bgr8",
            "--warmup", "0",
            "--iterations", "1",
            "--profile", "bounded_default",
        ]
        process = subprocess.run(
            command, check=False, capture_output=True, text=True, encoding="utf-8"
        )
    if process.returncode != 0:
        raise RuntimeError(
            f"native memory benchmark failed ({process.returncode}): "
            f"{process.stdout}{process.stderr}"
        )
    native = json.loads(process.stdout)
    peak = int(native["memoryBytes"]["peakResident"])
    boxes = int(native["result"]["acceptedBoxes"])
    runtime = native["runtime"]
    gates = {
        "peakResident": {
            "maximumBytes": arguments.maximum_peak_bytes,
            "observedBytes": peak,
            "passed": peak <= arguments.maximum_peak_bytes,
        },
        "acceptedBoxes": {
            "minimum": arguments.minimum_boxes,
            "maximum": arguments.maximum_boxes,
            "observed": boxes,
            "passed": boxes >= arguments.minimum_boxes
            and (arguments.maximum_boxes is None or boxes <= arguments.maximum_boxes),
        },
        "runtimeDefaults": {
            "expected": {
                "detectionStrategy": "bounded",
                "detectionMaxSide": 960,
                "recognitionBatchSize": 1,
            },
            "observed": runtime,
            "passed": runtime
            == {
                "detectionStrategy": "bounded",
                "detectionMaxSide": 960,
                "recognitionBatchSize": 1,
            },
        },
        "detectionInputShape": {
            "expected": [1, 3, 960, 960],
            "observed": native["result"]["detectionInputShape"],
            "passed": native["result"]["detectionInputShape"] == [1, 3, 960, 960],
        },
        "recognitionBatchShapes": {
            "expectedMaximumBatch": 1,
            "observed": native["result"]["recognitionBatchShapes"],
            "passed": all(
                shape[0] == 1
                for shape in native["result"]["recognitionBatchShapes"]
            ),
        },
    }
    passed = all(gate["passed"] for gate in gates.values())
    report = {
        "schemaVersion": "1.0",
        "passed": passed,
        "fixtureId": fixture["id"],
        "image": {"width": arguments.target_width, "height": arguments.target_height},
        "gates": gates,
        "native": native,
    }
    serialized = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
