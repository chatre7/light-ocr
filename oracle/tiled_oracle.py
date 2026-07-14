#!/usr/bin/env python3
"""Pinned, test-only PP-OCRv6 tiled-v1 stage oracle."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np

from oracle import (
    crop_text,
    db_postprocess,
    decode,
    detection_input,
    load_raw,
    recognition_batches,
    session,
    sha256,
    sort_boxes,
    tensor_record,
)
from tiled import run_tiled_detection


def run(
    bundle: Path,
    pixels: Path,
    width: int,
    height: int,
    stride: int,
    pixel_format: str,
    include_crop_pixels: bool = False,
) -> dict[str, Any]:
    manifest = json.loads((bundle / "manifest.json").read_text("utf-8"))
    config = json.loads((bundle / manifest["normalizedConfigPath"]).read_text("utf-8"))
    if config.get("schemaVersion") != "1.2":
        raise RuntimeError("tiled oracle requires normalized config schema 1.2")
    profile = config.get("runtimeProfiles", {}).get("tiled")
    if profile is None or profile.get("contractVersion") != "tiled-v1":
        raise RuntimeError("tiled oracle requires the tiled-v1 runtime contract")
    characters = json.loads(
        (bundle / config["recognition"]["decode"]["dictionaryPath"]).read_text("utf-8")
    )["characters"]
    image = load_raw(pixels, width, height, stride, pixel_format)
    detection_session = session(bundle / manifest["models"]["detection"]["modelPath"])
    recognition_session = session(bundle / manifest["models"]["recognition"]["modelPath"])
    boxes, detection_record = run_tiled_detection(
        image, config, detection_session, detection_input, db_postprocess, tensor_record
    )
    boxes = sort_boxes(boxes, config["geometry"]["rowBandPixels"])
    crops = [crop_text(image, box, config["geometry"]) for box in boxes]
    batch_size = int(config["runtimeDefaults"]["recognitionBatchSize"])
    result: dict[str, Any] = {
        "schemaVersion": "1.0",
        "profile": "tiled_v1",
        "modelBundleId": manifest["bundleId"],
        "image": {"width": width, "height": height},
        "models": {
            "detection": {
                "inputName": detection_session.get_inputs()[0].name,
                "outputName": detection_session.get_outputs()[0].name,
            },
            "recognition": {
                "inputName": recognition_session.get_inputs()[0].name,
                "outputName": recognition_session.get_outputs()[0].name,
            },
        },
        **detection_record,
        "boxes": [box.tolist() for box in boxes],
        "crops": [
            {
                "index": index,
                "width": crop.shape[1],
                "height": crop.shape[0],
                "channels": crop.shape[2],
                "sha256Bgr8": sha256(crop.tobytes()),
                **(
                    {"pixelsBgr8Base64": base64.b64encode(crop.tobytes()).decode("ascii")}
                    if include_crop_pixels
                    else {}
                ),
            }
            for index, crop in enumerate(crops)
        ],
        "recognitionBatches": [],
        "decoded": [{} for _ in boxes],
        "lines": [],
    }
    for batch_index, (indices, recognition_input) in enumerate(
        recognition_batches(crops, config["recognition"], batch_size)
    ):
        recognition_output = np.asarray(
            recognition_session.run(
                None, {recognition_session.get_inputs()[0].name: recognition_input}
            )[0],
            dtype=np.float32,
        )
        input_record = tensor_record(recognition_input)
        output_record = tensor_record(recognition_output)
        result["recognitionBatches"].append({
            "batchIndex": batch_index,
            "inputIndices": indices,
            "inputShape": input_record["shape"],
            "inputSha256Float32LE": input_record["sha256Float32LE"],
            "inputSamples": input_record["samples"],
            "outputShape": output_record["shape"],
            "outputSha256Float32LE": output_record["sha256Float32LE"],
            "outputSamples": output_record["samples"],
        })
        for source_index, decoded_value in zip(
            indices, decode(recognition_output, characters)
        ):
            result["decoded"][source_index] = decoded_value
    threshold = float(config["recognition"]["defaultScoreThreshold"])
    for box, decoded_value in zip(boxes, result["decoded"]):
        if decoded_value["text"] and decoded_value["confidence"] >= threshold:
            result["lines"].append({
                "text": decoded_value["text"],
                "confidence": decoded_value["confidence"],
                "box": box.tolist(),
            })
    return result
