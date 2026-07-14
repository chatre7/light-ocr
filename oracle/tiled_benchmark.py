#!/usr/bin/env python3
"""Benchmark the independent Python tiled-v1 oracle with persistent sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np
import psutil

try:
    import resource
except ImportError:  # Windows does not provide the Unix resource module.
    resource = None  # type: ignore[assignment]

from oracle import (
    crop_text,
    db_postprocess,
    decode,
    detection_input,
    load_raw_bytes,
    recognition_batches,
    session,
    sort_boxes,
)
from tiled import make_candidate, merge_candidates, plan_tiles


def elapsed_us(begin: int, end: int) -> int:
    return (end - begin) // 1000


def distribution(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)

    def percentile(value: float) -> int:
        return ordered[min(len(ordered) - 1, math.ceil(value * len(ordered)) - 1)]

    return {
        "minimum": ordered[0],
        "median": percentile(0.5),
        "p95": percentile(0.95),
        "maximum": ordered[-1],
    }


def result_hash(lines: list[dict[str, Any]]) -> str:
    packed = json.dumps(lines, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def benchmark(
    bundle: Path,
    pixels: Path,
    width: int,
    height: int,
    stride: int,
    pixel_format: str,
    warmup: int,
    iterations: int,
) -> dict[str, Any]:
    load_begin = time.perf_counter_ns()
    manifest = json.loads((bundle / "manifest.json").read_text("utf-8"))
    config = json.loads((bundle / manifest["normalizedConfigPath"]).read_text("utf-8"))
    profile = config.get("runtimeProfiles", {}).get("tiled")
    if config.get("schemaVersion") != "1.2" or profile is None:
        raise RuntimeError("tiled benchmark requires the schema 1.2 tiled-v1 contract")
    if profile.get("contractVersion") != "tiled-v1":
        raise RuntimeError("tiled benchmark received an unsupported contract")
    characters = json.loads(
        (bundle / config["recognition"]["decode"]["dictionaryPath"]).read_text("utf-8")
    )["characters"]
    raw_pixels = pixels.read_bytes()
    model_bundle_bytes = sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file())
    load_end = time.perf_counter_ns()

    initialize_begin = time.perf_counter_ns()
    detection_session = session(bundle / manifest["models"]["detection"]["modelPath"])
    recognition_session = session(bundle / manifest["models"]["recognition"]["modelPath"])
    initialize_end = time.perf_counter_ns()

    names = [
        "inputValidation",
        "detectionPreprocess",
        "detectionInference",
        "detectionPostprocess",
        "detectionMerge",
        "cropAndSort",
        "recognitionPreprocess",
        "recognitionInference",
        "recognitionPostprocess",
    ]
    stage_samples: dict[str, list[int]] = {name: [] for name in names}
    wall_samples: list[int] = []
    inference_samples: list[int] = []
    resident_samples: list[int] = []
    hashes: list[str] = []
    last_lines: list[dict[str, Any]] = []
    last_raw_count = 0
    last_suppressed_count = 0

    def run_once(record: bool) -> None:
        nonlocal last_lines, last_raw_count, last_suppressed_count
        wall_begin = time.perf_counter_ns()
        stage_begin = wall_begin
        image = load_raw_bytes(raw_pixels, width, height, stride, pixel_format)
        stage_end = time.perf_counter_ns()
        timings = [elapsed_us(stage_begin, stage_end)]

        raw_candidates: list[dict[str, Any]] = []
        preprocess_us = 0
        detection_inference_us = 0
        postprocess_us = 0
        for tile in plan_tiles(width, height, profile):
            roi = image[
                tile["y"] : tile["y"] + tile["height"],
                tile["x"] : tile["x"] + tile["width"],
            ]
            begin = time.perf_counter_ns()
            detection_tensor = detection_input(
                roi,
                config["detection"],
                config["sourceDetectionResize"],
                "bounded",
                int(profile["tileSide"]),
            )
            end = time.perf_counter_ns()
            preprocess_us += elapsed_us(begin, end)

            begin = end
            detection_output = np.asarray(
                detection_session.run(
                    None,
                    {detection_session.get_inputs()[0].name: detection_tensor},
                )[0],
                dtype=np.float32,
            )
            end = time.perf_counter_ns()
            detection_inference_us += elapsed_us(begin, end)

            begin = end
            probability = (
                detection_output[0, 0]
                if detection_output.ndim == 4
                else detection_output[0]
            )
            _, boxes, _, traces = db_postprocess(
                probability, tile["width"], tile["height"], config["detection"]
            )
            accepted = [trace for trace in traces if trace["decision"] == "accepted"]
            if len(accepted) != len(boxes):
                raise RuntimeError("oracle accepted trace/box count mismatch")
            for candidate_ordinal, (box, trace) in enumerate(zip(boxes, accepted)):
                raw_candidates.append(
                    make_candidate(
                        box.tolist(),
                        float(trace["score"]),
                        tile,
                        width,
                        height,
                        candidate_ordinal,
                        int(profile["artificialBoundaryMargin"]),
                    )
                )
            end = time.perf_counter_ns()
            postprocess_us += elapsed_us(begin, end)
        timings.extend([preprocess_us, detection_inference_us, postprocess_us])

        stage_begin = time.perf_counter_ns()
        representatives, suppressions = merge_candidates(raw_candidates, profile)
        boxes = [np.asarray(candidate["quad"], dtype=np.float32) for candidate in representatives]
        stage_end = time.perf_counter_ns()
        timings.append(elapsed_us(stage_begin, stage_end))

        stage_begin = stage_end
        boxes = sort_boxes(boxes, config["geometry"]["rowBandPixels"])
        crops = [crop_text(image, box, config["geometry"]) for box in boxes]
        stage_end = time.perf_counter_ns()
        timings.append(elapsed_us(stage_begin, stage_end))

        stage_begin = stage_end
        batches = recognition_batches(
            crops,
            config["recognition"],
            int(config["runtimeDefaults"]["recognitionBatchSize"]),
        )
        stage_end = time.perf_counter_ns()
        timings.append(elapsed_us(stage_begin, stage_end))

        stage_begin = stage_end
        outputs: list[tuple[list[int], np.ndarray]] = []
        for indices, recognition_input in batches:
            output = np.asarray(
                recognition_session.run(
                    None,
                    {recognition_session.get_inputs()[0].name: recognition_input},
                )[0],
                dtype=np.float32,
            )
            outputs.append((indices, output))
        stage_end = time.perf_counter_ns()
        timings.append(elapsed_us(stage_begin, stage_end))

        stage_begin = stage_end
        decoded: list[dict[str, Any]] = [{} for _ in boxes]
        for indices, output in outputs:
            for source_index, value in zip(indices, decode(output, characters)):
                decoded[source_index] = value
        threshold = float(config["recognition"]["defaultScoreThreshold"])
        lines = [
            {
                "text": value["text"],
                "confidence": value["confidence"],
                "box": box.tolist(),
            }
            for box, value in zip(boxes, decoded)
            if value["text"] and value["confidence"] >= threshold
        ]
        stage_end = time.perf_counter_ns()
        timings.append(elapsed_us(stage_begin, stage_end))
        wall_end = stage_end
        if not lines:
            raise RuntimeError("tiled oracle benchmark decoded no accepted text")

        if record:
            wall_samples.append(elapsed_us(wall_begin, wall_end))
            inference_samples.append(timings[2] + timings[7])
            for name, value in zip(names, timings):
                stage_samples[name].append(value)
            resident_samples.append(psutil.Process().memory_info().rss)
            hashes.append(result_hash(lines))
            last_lines = lines
            last_raw_count = len(raw_candidates)
            last_suppressed_count = len(suppressions)

    for _ in range(warmup):
        run_once(False)
    for _ in range(iterations):
        run_once(True)

    if resource is None:
        peak_rss = max(resident_samples)
    else:
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak_rss *= 1024

    return {
        "schema": "light-ocr-tiled-oracle-benchmark/1.0",
        "ok": True,
        "backend": "python-oracle",
        "contractVersion": "tiled-v1",
        "modelBundleId": manifest["bundleId"],
        "modelBundleBytes": model_bundle_bytes,
        "runtime": {"intraOpThreads": 1, "interOpThreads": 1},
        "loadUs": elapsed_us(load_begin, load_end),
        "engineInitializationUs": elapsed_us(initialize_begin, initialize_end),
        "warmup": warmup,
        "iterations": iterations,
        "latencyUs": distribution(wall_samples),
        "inferenceOnlyUs": distribution(inference_samples),
        "stagesUs": {
            name: distribution(values) for name, values in stage_samples.items()
        },
        "memoryBytes": {
            "residentMinimum": min(resident_samples),
            "residentMaximum": max(resident_samples),
            "residentFinal": resident_samples[-1],
            "peakResident": peak_rss,
        },
        "result": {
            "acceptedLines": len(last_lines),
            "rawDetectionBoxes": last_raw_count,
            "suppressedDuplicateBoxes": last_suppressed_count,
            "stableSha256": hashes[0],
            "stable": len(set(hashes)) == 1,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--pixels", type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--stride", type=int, required=True)
    parser.add_argument(
        "--format", choices=["gray8", "rgb8", "bgr8", "rgba8"], required=True
    )
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=10)
    arguments = parser.parse_args()
    print(
        json.dumps(
            benchmark(
                arguments.bundle,
                arguments.pixels,
                arguments.width,
                arguments.height,
                arguments.stride,
                arguments.format,
                arguments.warmup,
                arguments.iterations,
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
