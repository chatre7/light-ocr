#!/usr/bin/env python3
"""Run all eight locked tiled-v1 fixtures through live stage parity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-probe", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--fixtures", type=Path,
        default=root / "corpus" / "tiled-v1" / "fixtures",
    )
    parser.add_argument("--report-dir", type=Path, required=True)
    arguments = parser.parse_args()
    runner = Path(__file__).with_name("run_tiled_parity.py")
    reports = []
    for fixture in sorted(arguments.fixtures.glob("*/fixture.json")):
        report_path = arguments.report_dir / f"{fixture.parent.name}.json"
        process = subprocess.run(
            [
                sys.executable, str(runner), "--native-probe", str(arguments.native_probe),
                "--bundle", str(arguments.bundle), "--fixture", str(fixture),
                "--report", str(report_path),
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        if not process.stdout:
            raise RuntimeError(f"tiled parity produced no report: {process.stderr}")
        report = json.loads(process.stdout)
        reports.append({"fixtureId": report["fixtureId"], "passed": report["passed"],
                        "report": report_path.name})
        if process.returncode != 0:
            break
    aggregate = {
        "schema": "light-ocr-tiled-parity-corpus-report/1.0",
        "passed": len(reports) == 8 and all(value["passed"] for value in reports),
        "fixtureCount": len(reports),
        "profile": "tiled_v1",
        "fixtures": reports,
    }
    arguments.report_dir.mkdir(parents=True, exist_ok=True)
    (arguments.report_dir / "corpus-report.json").write_text(
        json.dumps(aggregate, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(aggregate, sort_keys=True, separators=(",", ":")))
    return 0 if aggregate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
