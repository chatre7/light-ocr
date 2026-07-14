#!/usr/bin/env python3
"""Independent Python implementation of the tiled-v1 planner and merge."""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np


def plan_axis(length: int, tile_side: int, overlap: int) -> list[int]:
    if length <= 0 or tile_side <= 0 or overlap <= 0 or overlap >= tile_side:
        raise ValueError("invalid tiled-v1 axis contract")
    starts = [0]
    if length <= tile_side:
        return starts
    stride = tile_side - overlap
    while starts[-1] + tile_side < length:
        following = min(starts[-1] + stride, length - tile_side)
        if following <= starts[-1]:
            raise ValueError("tiled-v1 axis planning did not advance")
        starts.append(following)
    return starts


def plan_tiles(width: int, height: int, profile: dict[str, Any]) -> list[dict[str, int]]:
    side = int(profile["tileSide"])
    overlap = int(profile["minimumOverlap"])
    tiles = []
    for y in plan_axis(height, side, overlap):
        for x in plan_axis(width, side, overlap):
            tiles.append({
                "ordinal": len(tiles), "x": x, "y": y,
                "width": min(side, width - x), "height": min(side, height - y),
            })
    return tiles


def area(points: list[list[float]]) -> float:
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2.0


def signed_area(points: list[list[float]]) -> float:
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) / 2.0


def subtract(left: list[float], right: list[float]) -> list[float]:
    return [left[0] - right[0], left[1] - right[1]]


def cross(left: list[float], right: list[float]) -> float:
    return left[0] * right[1] - left[1] * right[0]


def inside(point: list[float], begin: list[float], end: list[float], orientation: float) -> bool:
    value = cross(subtract(end, begin), subtract(point, begin))
    return value >= -1e-9 if orientation > 0 else value <= 1e-9


def line_intersection(
    segment_begin: list[float], segment_end: list[float],
    edge_begin: list[float], edge_end: list[float]
) -> list[float]:
    segment = subtract(segment_end, segment_begin)
    edge = subtract(edge_end, edge_begin)
    denominator = cross(segment, edge)
    if abs(denominator) <= 1e-9:
        return segment_end
    ratio = cross(subtract(edge_begin, segment_begin), edge) / denominator
    return [
        segment_begin[0] + ratio * segment[0],
        segment_begin[1] + ratio * segment[1],
    ]


def polygon_intersection(
    subject: list[list[float]], clip: list[list[float]]
) -> list[list[float]]:
    output = [list(point) for point in subject]
    orientation = signed_area(clip)
    for edge_index, edge_begin in enumerate(clip):
        edge_end = clip[(edge_index + 1) % len(clip)]
        input_points = output
        output = []
        if not input_points:
            break
        previous = input_points[-1]
        previous_inside = inside(previous, edge_begin, edge_end, orientation)
        for current in input_points:
            current_inside = inside(current, edge_begin, edge_end, orientation)
            if current_inside != previous_inside:
                output.append(line_intersection(previous, current, edge_begin, edge_end))
            if current_inside:
                output.append(current)
            previous = current
            previous_inside = current_inside
    return output


def bounds(quad: list[list[float]]) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in quad), min(point[1] for point in quad),
        max(point[0] for point in quad), max(point[1] for point in quad),
    )


def rectangles_overlap(left: dict[str, int], right: dict[str, int]) -> bool:
    return (
        left["x"] < right["x"] + right["width"]
        and right["x"] < left["x"] + left["width"]
        and left["y"] < right["y"] + right["height"]
        and right["y"] < left["y"] + left["height"]
    )


def bounds_overlap(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def is_duplicate(left: dict[str, Any], right: dict[str, Any], profile: dict[str, Any]) -> bool:
    if (
        left["tileOrdinal"] == right["tileOrdinal"]
        or not rectangles_overlap(left["sourceTile"], right["sourceTile"])
        or not bounds_overlap(bounds(left["quad"]), bounds(right["quad"]))
    ):
        return False
    intersection = area(polygon_intersection(left["quad"], right["quad"]))
    if not math.isfinite(intersection) or intersection <= 0:
        return False
    left_area = area(left["quad"])
    right_area = area(right["quad"])
    union = left_area + right_area - intersection
    if union <= 0 or min(left_area, right_area) <= 0:
        return False
    merge = profile["merge"]
    return (
        intersection / union >= float(merge["iouThreshold"])
        or intersection / min(left_area, right_area)
        >= float(merge["intersectionOverSmallerThreshold"])
    )


def make_candidate(
    local_quad: list[list[float]], score: float, tile: dict[str, int],
    image_width: int, image_height: int, candidate_ordinal: int,
    boundary_margin: int,
) -> dict[str, Any]:
    quad = [
        [
            float(min(image_width, max(0.0, point[0] + tile["x"]))),
            float(min(image_height, max(0.0, point[1] + tile["y"]))),
        ]
        for point in local_quad
    ]
    minimum_x, minimum_y, maximum_x, maximum_y = bounds(quad)
    edges: list[str] = []
    distances: list[float] = []
    edge_contract = [
        (tile["x"] > 0, minimum_x - tile["x"], "left"),
        (tile["y"] > 0, minimum_y - tile["y"], "top"),
        (tile["x"] + tile["width"] < image_width,
         tile["x"] + tile["width"] - maximum_x, "right"),
        (tile["y"] + tile["height"] < image_height,
         tile["y"] + tile["height"] - maximum_y, "bottom"),
    ]
    for artificial, distance, name in edge_contract:
        if not artificial:
            continue
        distance = max(0.0, float(distance))
        distances.append(distance)
        if distance <= boundary_margin:
            edges.append(name)
    return {
        "quad": quad,
        "score": float(score),
        "tileOrdinal": tile["ordinal"],
        "candidateOrdinal": candidate_ordinal,
        "sourceTile": dict(tile),
        "nearbyArtificialEdges": edges,
        "distanceToNearestArtificialEdge": (
            min(distances) if distances else float(max(image_width, image_height) + 1)
        ),
    }


def merge_candidates(
    candidates: list[dict[str, Any]], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, list[int]]]]:
    ordered = sorted(
        candidates,
        key=lambda value: (
            bool(value["nearbyArtificialEdges"]),
            -value["score"],
            -value["distanceToNearestArtificialEdge"],
            value["tileOrdinal"],
            value["candidateOrdinal"],
        ),
    )
    representatives: list[dict[str, Any]] = []
    suppressions: list[dict[str, list[int]]] = []
    for candidate in ordered:
        representative = next(
            (value for value in representatives if is_duplicate(value, candidate, profile)),
            None,
        )
        if representative is None:
            representatives.append(candidate)
        else:
            suppressions.append({
                "candidate": [candidate["tileOrdinal"], candidate["candidateOrdinal"]],
                "representative": [
                    representative["tileOrdinal"], representative["candidateOrdinal"]
                ],
            })
    return representatives, suppressions


def run_tiled_detection(
    image: np.ndarray,
    config: dict[str, Any],
    detection_session: Any,
    detection_input_fn: Callable[..., np.ndarray],
    db_postprocess_fn: Callable[..., tuple[int, list[np.ndarray], str, list[dict[str, Any]]]],
    tensor_record_fn: Callable[[np.ndarray], dict[str, Any]],
) -> tuple[list[np.ndarray], dict[str, Any]]:
    profile = config["runtimeProfiles"]["tiled"]
    height, width = image.shape[:2]
    passes = []
    raw_candidates: list[dict[str, Any]] = []
    for tile in plan_tiles(width, height, profile):
        roi = image[
            tile["y"] : tile["y"] + tile["height"],
            tile["x"] : tile["x"] + tile["width"],
        ]
        detection_input = detection_input_fn(
            roi, config["detection"], config["sourceDetectionResize"], "bounded",
            int(profile["tileSide"]),
        )
        detection_output = np.asarray(
            detection_session.run(
                None, {detection_session.get_inputs()[0].name: detection_input}
            )[0],
            dtype=np.float32,
        )
        probability = detection_output[0, 0] if detection_output.ndim == 4 else detection_output[0]
        contour_count, boxes, bitmap_sha256, traces = db_postprocess_fn(
            probability, tile["width"], tile["height"], config["detection"]
        )
        accepted_traces = [trace for trace in traces if trace["decision"] == "accepted"]
        if len(accepted_traces) != len(boxes):
            raise RuntimeError("oracle accepted trace/box count mismatch")
        pass_candidates = []
        for candidate_ordinal, (box, trace) in enumerate(zip(boxes, accepted_traces)):
            candidate = make_candidate(
                box.tolist(), float(trace["score"]), tile, width, height,
                candidate_ordinal, int(profile["artificialBoundaryMargin"]),
            )
            raw_candidates.append(candidate)
            pass_candidates.append({
                "tileOrdinal": candidate["tileOrdinal"],
                "candidateOrdinal": candidate["candidateOrdinal"],
            })
        passes.append({
            "tileOrdinal": tile["ordinal"],
            "roi": [tile["x"], tile["y"], tile["width"], tile["height"]],
            "detectionInput": tensor_record_fn(detection_input),
            "detectionOutput": tensor_record_fn(detection_output),
            "contourCandidates": contour_count,
            "thresholdBitmapSha256": bitmap_sha256,
            "detectionCandidates": traces,
            "acceptedCandidates": pass_candidates,
        })
    representatives, suppressions = merge_candidates(raw_candidates, profile)
    record = {
        "contractVersion": profile["contractVersion"],
        "detectionPasses": passes,
        "rawCandidates": raw_candidates,
        "suppressions": suppressions,
        "representatives": [
            [candidate["tileOrdinal"], candidate["candidateOrdinal"]]
            for candidate in representatives
        ],
    }
    return [np.asarray(candidate["quad"], dtype=np.float32) for candidate in representatives], record
