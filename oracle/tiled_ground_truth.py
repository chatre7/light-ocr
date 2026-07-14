#!/usr/bin/env python3
"""Validate the immutable tiled-v1 fixture and annotation contract."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = {
    "tiled-small-text-2048",
    "tiled-dense-2048",
    "tiled-horizontal-boundary-2048",
    "tiled-vertical-boundary-2048",
    "tiled-four-way-intersection-2048",
    "tiled-original-edges-2048",
    "tiled-near-neighbor-2048",
    "tiled-reading-order-2048",
}


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signed_area(quad: list[list[float]]) -> float:
    return sum(
        quad[index][0] * quad[(index + 1) % 4][1]
        - quad[(index + 1) % 4][0] * quad[index][1]
        for index in range(4)
    ) / 2.0


def verify_quad(quad: object, fixture_id: str) -> None:
    if not isinstance(quad, list) or len(quad) != 4:
        raise RuntimeError(f"annotation quad is invalid: {fixture_id}")
    signs = []
    for index, point in enumerate(quad):
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
            or not all(0 <= value <= 2048 for value in point)
        ):
            raise RuntimeError(f"annotation point is invalid: {fixture_id}")
        following = quad[(index + 1) % 4]
        after = quad[(index + 2) % 4]
        cross = (
            (following[0] - point[0]) * (after[1] - following[1])
            - (following[1] - point[1]) * (after[0] - following[0])
        )
        signs.append(cross)
    if signed_area(quad) <= 0 or any(value <= 0 for value in signs):
        raise RuntimeError(f"annotation quad is not clockwise convex: {fixture_id}")


def crosses(quad: list[list[float]], axis: int, boundary: float) -> bool:
    values = [point[axis] for point in quad]
    return min(values) <= boundary <= max(values)


def verify_matrix(fixtures: dict[str, dict[str, Any]]) -> None:
    small = fixtures["tiled-small-text-2048"]["annotations"]
    if len(small) < 32 or sum(item["design"]["fontSize"] <= 24 for item in small) < 30:
        raise RuntimeError("small-text fixture does not lock 32 primarily 12-24 px lines")
    if not any("mixed-script" in item["tags"] for item in small):
        raise RuntimeError("small-text fixture has no Chinese/Latin/digit control")

    dense = fixtures["tiled-dense-2048"]["annotations"]
    if len(dense) < 100:
        raise RuntimeError("dense fixture has fewer than 100 independent lines")

    horizontal = fixtures["tiled-horizontal-boundary-2048"]["annotations"]
    if len(horizontal) < 8 or not all(
        any(crosses(item["quad"], 1, boundary) for boundary in (768, 1280))
        for item in horizontal
    ):
        raise RuntimeError("horizontal-boundary targets do not cross locked tile edges")

    vertical = fixtures["tiled-vertical-boundary-2048"]["annotations"]
    if len(vertical) < 8 or not all(
        any(crosses(item["quad"], 0, boundary) for boundary in (768, 1280))
        for item in vertical
    ):
        raise RuntimeError("vertical-boundary targets do not cross locked tile edges")

    four_way = fixtures["tiled-four-way-intersection-2048"]["annotations"]
    targets = [item for item in four_way if "four-way-target" in item["tags"]]
    controls = [item for item in four_way if "four-way-control" in item["tags"]]
    if len(targets) < 4 or len(controls) < 4:
        raise RuntimeError("four-way fixture lacks target/control coverage")
    for item in targets:
        center = item["design"]["center"]
        if not (768 < center[0] < 1280 and 768 < center[1] < 1280):
            raise RuntimeError("four-way target is outside the four-tile common overlap")

    original = fixtures["tiled-original-edges-2048"]["annotations"]
    required_edges = {
        "top", "bottom", "left", "right", "top-left", "top-right",
        "bottom-left", "bottom-right",
    }
    actual_edges = {tag for item in original for tag in item["tags"]}
    if not required_edges <= actual_edges:
        raise RuntimeError("original-edge fixture does not cover all sides and corners")
    if not all(any(value in (0.0, 2048.0) for point in item["quad"] for value in point)
               for item in original):
        raise RuntimeError("original-edge fixture has an annotation that does not touch an edge")

    neighbors = fixtures["tiled-near-neighbor-2048"]["annotations"]
    pair_tags = {tag for item in neighbors for tag in item["tags"] if tag.startswith("pair-")}
    if len(neighbors) < 16 or len(pair_tags) < 8:
        raise RuntimeError("near-neighbor fixture has fewer than eight distinct pairs")

    order = fixtures["tiled-reading-order-2048"]["annotations"]
    centers = [item["design"]["center"] for item in order]
    if len(order) < 16 or len({value[0] for value in centers}) < 4 or len({value[1] for value in centers}) < 4:
        raise RuntimeError("reading-order fixture does not span four rows and columns")


def verify_tiled_ground_truth(
    fixtures_root: Path,
    lock_path: Path,
) -> list[dict[str, Any]]:
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    if (
        lock.get("schemaVersion") != "1.0"
        or lock.get("contractVersion") != "tiled-v1"
        or set(record["fixtureId"] for record in lock.get("fixtures", [])) != EXPECTED_IDS
    ):
        raise RuntimeError("tiled ground-truth lock identity is invalid")
    if lock.get("plannerVectorSha256") != sha256(canonical(lock.get("plannerVectors"))):
        raise RuntimeError("tiled planner vector lock is invalid")
    if lock["plannerVectors"] != {
        "contractVersion": "tiled-v1",
        "tileSide": 1280,
        "minimumOverlap": 128,
        "image": [2048, 2048],
        "xStarts": [0, 768],
        "yStarts": [0, 768],
        "tileOrder": [
            [0, 0, 0, 1280, 1280],
            [1, 768, 0, 1280, 1280],
            [2, 0, 768, 1280, 1280],
            [3, 768, 768, 1280, 1280],
        ],
    }:
        raise RuntimeError("tiled planner vectors changed without a contract version")

    source_lock = json.loads((ROOT / "corpus" / "sources.lock.json").read_text("utf-8"))
    font_source = next(
        record for record in source_lock["resources"]
        if record["name"] == lock["font"]["name"]
    )
    if font_source["sha256"] != lock["font"]["sha256"]:
        raise RuntimeError("tiled corpus font identity is stale")
    generator = ROOT / "corpus" / "generate_tiled_corpus.py"
    records_by_id = {record["fixtureId"]: record for record in lock["fixtures"]}
    actual_directories = {path.name for path in fixtures_root.iterdir() if path.is_dir()}
    if actual_directories != EXPECTED_IDS:
        raise RuntimeError("tiled fixture directory set is not exact")

    verified: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for fixture_id in sorted(EXPECTED_IDS):
        directory = fixtures_root / fixture_id
        fixture_bytes = (directory / "fixture.json").read_bytes()
        fixture = json.loads(fixture_bytes)
        pixels = (directory / "pixels.bin").read_bytes()
        locked = records_by_id[fixture_id]
        if (
            fixture.get("id") != fixture_id
            or fixture.get("corpusRevision") != lock["revision"]
            or fixture.get("contractVersion") != "tiled-v1"
            or fixture.get("plannerVectorSha256") != lock["plannerVectorSha256"]
            or fixture.get("width") != 2048
            or fixture.get("height") != 2048
            or fixture.get("stride") != 6144
            or fixture.get("pixelFormat") != "bgr8"
            or len(pixels) != 2048 * 2048 * 3
            or sha256(pixels) != fixture.get("pixelSha256")
            or locked["pixelSha256"] != fixture.get("pixelSha256")
            or locked["fixtureSha256"] != sha256(fixture_bytes)
        ):
            raise RuntimeError(f"tiled fixture identity is stale: {fixture_id}")
        provenance = fixture.get("provenance", {})
        if provenance.get("generatorSha256") != sha256(generator.read_bytes()):
            raise RuntimeError(f"tiled generator identity is stale: {fixture_id}")
        annotations = fixture.get("annotations")
        if not isinstance(annotations, list) or not annotations:
            raise RuntimeError(f"tiled annotations are missing: {fixture_id}")
        if locked["lineCount"] != len(annotations) or locked["annotationsSha256"] != sha256(canonical(annotations)):
            raise RuntimeError(f"tiled annotation lock is stale: {fixture_id}")
        if len({item["id"] for item in annotations}) != len(annotations):
            raise RuntimeError(f"tiled annotation IDs are not unique: {fixture_id}")
        if [item["order"] for item in annotations] != list(range(len(annotations))):
            raise RuntimeError(f"tiled annotation order is not contiguous: {fixture_id}")
        for item in annotations:
            if not item.get("id") or not item.get("text") or not item.get("tags"):
                raise RuntimeError(f"tiled annotation metadata is incomplete: {fixture_id}")
            verify_quad(item.get("quad"), fixture_id)
        ground_truth = fixture.get("groundTruth", {})
        if (
            ground_truth.get("lines") != [item["text"] for item in annotations]
            or ground_truth.get("boxes") != [item["quad"] for item in annotations]
            or not ground_truth.get("source")
            or not ground_truth.get("annotationPolicy")
        ):
            raise RuntimeError(f"tiled compatibility ground truth is stale: {fixture_id}")
        verified.append(fixture)
        by_id[fixture_id] = fixture
    verify_matrix(by_id)
    return verified


if __name__ == "__main__":
    fixtures = ROOT / "corpus" / "tiled-v1" / "fixtures"
    lock = ROOT / "corpus" / "tiled-v1" / "ground-truth.lock.json"
    print(json.dumps({"fixtures": len(verify_tiled_ground_truth(fixtures, lock)), "passed": True}))
