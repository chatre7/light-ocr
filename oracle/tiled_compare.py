#!/usr/bin/env python3
"""Compare native tiled-v1 stage records with the independent Python oracle."""

from __future__ import annotations

import json
import math
from typing import Any

from compare import box_metrics, compare, maximum_point_difference


def compare_tiled(
    native: dict[str, Any], oracle: dict[str, Any],
    exceptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def exact(name: str, left: Any, right: Any) -> None:
        checks.append({"checkpoint": name, "passed": left == right,
                       "native": left, "oracle": right})

    def close(name: str, left: float, right: float, tolerance: float) -> None:
        difference = abs(left - right)
        checks.append({"checkpoint": name, "passed": difference <= tolerance,
                       "absoluteDifference": difference, "tolerance": tolerance})

    exact("profile", native.get("profile"), "tiled_v1")
    exact("contractVersion", native.get("contractVersion"), oracle.get("contractVersion"))
    exact("detectionPassCount", len(native.get("detectionPasses", [])),
          len(oracle.get("detectionPasses", [])))
    for pass_index, (native_pass, oracle_pass) in enumerate(
        zip(native.get("detectionPasses", []), oracle.get("detectionPasses", []))
    ):
        prefix = f"detectionPasses[{pass_index}]"
        for field in ("tileOrdinal", "roi", "contourCandidates", "thresholdBitmapSha256",
                      "acceptedCandidates"):
            exact(f"{prefix}.{field}", native_pass[field], oracle_pass[field])
        for tensor in ("detectionInput", "detectionOutput"):
            exact(f"{prefix}.{tensor}.shape", native_pass[tensor]["shape"],
                  oracle_pass[tensor]["shape"])
            exact(f"{prefix}.{tensor}.sha256", native_pass[tensor]["sha256Float32LE"],
                  oracle_pass[tensor]["sha256Float32LE"])
        native_candidates = native_pass["detectionCandidates"]
        oracle_candidates = oracle_pass["detectionCandidates"]
        exact(f"{prefix}.candidateCount", len(native_candidates), len(oracle_candidates))
        for candidate_index, (native_candidate, oracle_candidate) in enumerate(
            zip(native_candidates, oracle_candidates)
        ):
            candidate_prefix = f"{prefix}.detectionCandidates[{candidate_index}]"
            exact(f"{candidate_prefix}.candidateIndex", native_candidate["candidateIndex"],
                  oracle_candidate["candidateIndex"])
            exact(f"{candidate_prefix}.decision", native_candidate["decision"],
                  oracle_candidate["decision"])
            native_score = native_candidate["score"]
            oracle_score = oracle_candidate["score"]
            if native_score is None or oracle_score is None:
                exact(f"{candidate_prefix}.score", native_score, oracle_score)
            else:
                close(f"{candidate_prefix}.score", native_score, oracle_score, 1e-5)
            for field in ("initialQuad", "expandedPolygon", "expandedQuad", "restoredQuad"):
                difference = maximum_point_difference(
                    native_candidate[field], oracle_candidate[field]
                )
                checks.append({
                    "checkpoint": f"{candidate_prefix}.{field}",
                    "passed": difference is not None and difference <= 0.01,
                    "maximumPointDifference": difference,
                    "tolerance": 0.01,
                })

    native_raw = native.get("rawCandidates", [])
    oracle_raw = oracle.get("rawCandidates", [])
    exact("rawCandidateCount", len(native_raw), len(oracle_raw))
    for index, (native_candidate, oracle_candidate) in enumerate(zip(native_raw, oracle_raw)):
        prefix = f"rawCandidates[{index}]"
        for field in ("tileOrdinal", "candidateOrdinal", "sourceTile",
                      "nearbyArtificialEdges"):
            exact(f"{prefix}.{field}", native_candidate[field], oracle_candidate[field])
        close(f"{prefix}.score", native_candidate["score"], oracle_candidate["score"], 1e-5)
        close(
            f"{prefix}.distanceToNearestArtificialEdge",
            native_candidate["distanceToNearestArtificialEdge"],
            oracle_candidate["distanceToNearestArtificialEdge"], 0.01,
        )
        difference = maximum_point_difference(native_candidate["quad"], oracle_candidate["quad"])
        checks.append({"checkpoint": f"{prefix}.quad",
                       "passed": difference is not None and difference <= 0.01,
                       "maximumPointDifference": difference, "tolerance": 0.01})
    exact("suppressions", native.get("suppressions"), oracle.get("suppressions"))
    exact("representatives", native.get("representatives"), oracle.get("representatives"))

    # Reuse the mature crop/recognition/final comparator by projecting the first
    # pass into its legacy single-pass fields.  Every pass and merge-specific
    # field has already been checked independently above.
    def compatibility(record: dict[str, Any]) -> dict[str, Any]:
        projected = dict(record)
        first = record["detectionPasses"][0]
        projected.update({
            "detectionInput": first["detectionInput"],
            "detectionOutput": first["detectionOutput"],
            "contourCandidates": first["contourCandidates"],
            "thresholdBitmapSha256": first["thresholdBitmapSha256"],
            "detectionCandidates": first["detectionCandidates"],
        })
        return projected

    downstream = compare(compatibility(native), compatibility(oracle), [])
    checks.extend({**check, "checkpoint": f"downstream.{check['checkpoint']}"}
                  for check in downstream["checks"])
    exception_by_checkpoint = {
        value["checkpoint"]: value for value in (exceptions or [])
    }
    applied_exception_ids = []
    for check in checks:
        exception = exception_by_checkpoint.get(check["checkpoint"])
        if exception is None or check["passed"]:
            continue
        tolerance = exception["tolerance"]
        if (
            check.get("maximumAbsoluteDifference", math.inf)
            <= tolerance["maximumAbsoluteDifference"]
            and check.get("meanAbsoluteDifference", math.inf)
            <= tolerance["meanAbsoluteDifference"]
        ):
            check["passed"] = True
            check["exceptionId"] = exception["id"]
            applied_exception_ids.append(exception["id"])
    return {
        "schema": "light-ocr-tiled-parity-report/1.0",
        "passed": all(check["passed"] for check in checks),
        "appliedExceptionIds": sorted(set(applied_exception_ids)),
        "checks": checks,
    }


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    arguments = parser.parse_args()
    report = compare_tiled(
        json.loads(arguments.native.read_text("utf-8")),
        json.loads(arguments.oracle.read_text("utf-8")),
    )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if report["passed"] else 1)
