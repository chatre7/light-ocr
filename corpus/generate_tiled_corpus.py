#!/usr/bin/env python3
"""Generate the reviewed synthetic corpus for the tiled-v1 release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

import cv2
import numpy as np
import PIL
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
REVISION = "20260714.tiled1"
CONTRACT_VERSION = "tiled-v1"
IMAGE_SIDE = 2048
FONT_REVISION = "f8d157532fbfaeda587e826d4cd5b21a49186f7c"
RIGHTS = (
    "Generated entirely by project code using Noto Sans CJK under OFL-1.1; "
    "the rendered raw pixels are redistributable with light-ocr."
)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PLANNER_VECTORS = {
    "contractVersion": CONTRACT_VERSION,
    "tileSide": 1280,
    "minimumOverlap": 128,
    "image": [IMAGE_SIDE, IMAGE_SIDE],
    "xStarts": [0, 768],
    "yStarts": [0, 768],
    "tileOrder": [
        [0, 0, 0, 1280, 1280],
        [1, 768, 0, 1280, 1280],
        [2, 0, 768, 1280, 1280],
        [3, 768, 768, 1280, 1280],
    ],
}
PLANNER_VECTOR_SHA256 = sha256(canonical(PLANNER_VECTORS))


def clip(value: float) -> float:
    return float(max(0.0, min(float(IMAGE_SIDE), value)))


def make_canvas() -> np.ndarray:
    return np.full((IMAGE_SIDE, IMAGE_SIDE, 3), 255, dtype=np.uint8)


def paste_patch(canvas: np.ndarray, patch: np.ndarray, left: int, top: int) -> None:
    patch_height, patch_width = patch.shape[:2]
    source_left = max(0, -left)
    source_top = max(0, -top)
    destination_left = max(0, left)
    destination_top = max(0, top)
    width = min(patch_width - source_left, IMAGE_SIDE - destination_left)
    height = min(patch_height - source_top, IMAGE_SIDE - destination_top)
    if width <= 0 or height <= 0:
        raise RuntimeError("tiled corpus label lies outside the image")
    destination = canvas[
        destination_top : destination_top + height,
        destination_left : destination_left + width,
    ]
    source = patch[source_top : source_top + height, source_left : source_left + width]
    ink = np.any(source < 254, axis=2)
    destination[ink] = source[ink]


def render_label(
    canvas: np.ndarray,
    font_path: Path,
    line_id: str,
    text: str,
    center: tuple[float, float],
    font_size: int,
    order: int,
    tags: list[str],
    angle: float = 0.0,
    padding: int = 8,
) -> dict[str, Any]:
    font = ImageFont.truetype(str(font_path), font_size)
    measuring = ImageDraw.Draw(Image.new("RGB", (1, 1), "white"))
    bounds = measuring.textbbox((0, 0), text, font=font)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    margin = max(32, padding + 20)
    side = int(max(text_width, text_height) + margin * 2)
    patch_rgb = Image.new("RGB", (side, side), "white")
    draw = ImageDraw.Draw(patch_rgb)
    origin_x = (side - text_width) / 2.0 - bounds[0]
    origin_y = (side - text_height) / 2.0 - bounds[1]
    draw.text((origin_x, origin_y), text, font=font, fill=(12, 12, 12))
    actual = draw.textbbox((origin_x, origin_y), text, font=font)
    local_quad = np.asarray(
        [
            [actual[0] - padding, actual[1] - padding],
            [actual[2] + padding, actual[1] - padding],
            [actual[2] + padding, actual[3] + padding],
            [actual[0] - padding, actual[3] + padding],
        ],
        dtype=np.float32,
    )
    patch = cv2.cvtColor(np.asarray(patch_rgb), cv2.COLOR_RGB2BGR)
    transform = cv2.getRotationMatrix2D((side / 2.0, side / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(
        patch,
        transform,
        (side, side),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    transformed = cv2.transform(local_quad.reshape(1, 4, 2), transform)[0]
    left = int(round(center[0] - side / 2.0))
    top = int(round(center[1] - side / 2.0))
    paste_patch(canvas, rotated, left, top)
    quad = [[clip(point[0] + left), clip(point[1] + top)] for point in transformed]
    return {
        "id": line_id,
        "text": text,
        "order": order,
        "quad": quad,
        "tags": sorted(tags),
        "design": {
            "center": [center[0], center[1]],
            "fontSize": font_size,
            "rotationDegrees": angle,
            "padding": padding,
        },
    }


def add_grid(
    canvas: np.ndarray,
    font: Path,
    prefix: str,
    text: str,
    xs: list[int],
    ys: list[int],
    font_size: int,
    tags: list[str],
    annotations: list[dict[str, Any]],
) -> None:
    for y in ys:
        for x in xs:
            index = len(annotations)
            annotations.append(
                render_label(
                    canvas, font, f"{prefix}-{index + 1:03d}", text,
                    (x, y), font_size, index, tags
                )
            )


def fixture_small_text(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    ys = [96 + row * 118 for row in range(16)]
    for row, y in enumerate(ys):
        for column, x in enumerate((360, 1380)):
            index = len(annotations)
            text = "轻量 OCR 2026" if (row, column) in {(2, 0), (11, 1)} else "OCR 2026"
            annotations.append(
                render_label(
                    canvas, font, f"small-{index + 1:03d}", text, (x, y),
                    24 if "轻量" not in text else 26, index,
                    ["small-text", "mixed-script" if "轻量" in text else "latin-digits"],
                    padding=7,
                )
            )
    return canvas, annotations


def fixture_dense(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    add_grid(
        canvas, font, "dense", "OCR", [105 + index * 202 for index in range(10)],
        [105 + index * 202 for index in range(10)], 28,
        ["dense", "regular-spacing"], annotations,
    )
    return canvas, annotations


def fixture_horizontal_boundary(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    for y in (768, 1280):
        for column, x in enumerate((220, 690, 1180, 1680)):
            index = len(annotations)
            annotations.append(
                render_label(
                    canvas, font, f"horizontal-{index + 1:02d}", "H EDGE",
                    (x, y), 34, index, ["horizontal-boundary", f"boundary-y-{y}"],
                    angle=(-3.0 if column % 2 else 0.0),
                )
            )
    return canvas, annotations


def fixture_vertical_boundary(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    for y in (220, 650, 1120, 1640):
        for column, x in enumerate((730, 1250)):
            index = len(annotations)
            annotations.append(
                render_label(
                    canvas, font, f"vertical-{index + 1:02d}", "VEDGE",
                    (x, y), 34, index,
                    ["vertical-boundary", f"boundary-x-{768 if column == 0 else 1280}"],
                    angle=0.0,
                )
            )
    return canvas, annotations


def fixture_four_way(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    targets = [(860, 860), (1160, 860), (860, 1160), (1160, 1160)]
    controls = [(620, 1010), (1400, 1010), (1010, 620), (1010, 1400)]
    for center in targets:
        index = len(annotations)
        annotations.append(
            render_label(
                canvas, font, f"four-way-{index + 1:02d}", "FOUR", center, 34,
                index, ["four-way-target", "four-tile-common-overlap"],
            )
        )
    for center in controls:
        index = len(annotations)
        annotations.append(
            render_label(
                canvas, font, f"four-way-{index + 1:02d}", "NEAR", center, 32,
                index, ["four-way-control", "near-not-intersecting"],
            )
        )
    return canvas, annotations


def fixture_original_edges(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    cases = [
        ("TOP", (1024, 21), 0.0, "top"),
        ("BOTTOM", (1024, 2027), 0.0, "bottom"),
        ("LEFT", (21, 1024), 90.0, "left"),
        ("RIGHT", (2027, 1024), -90.0, "right"),
        ("CORNER", (73, 22), 0.0, "top-left"),
        ("CORNER", (1975, 22), 0.0, "top-right"),
        ("CORNER", (73, 2026), 0.0, "bottom-left"),
        ("CORNER", (1975, 2026), 0.0, "bottom-right"),
    ]
    for text, center, angle, edge in cases:
        index = len(annotations)
        annotations.append(
            render_label(
                canvas, font, f"original-edge-{index + 1:02d}", text, center,
                32, index, ["original-edge", edge], angle=angle, padding=10,
            )
        )
    return canvas, annotations


def fixture_near_neighbor(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    pair_centers = [(700, 180 + row * 225) for row in range(8)]
    for pair, (x, y) in enumerate(pair_centers):
        for offset in (-27, 27):
            index = len(annotations)
            annotations.append(
                render_label(
                    canvas, font, f"neighbor-{index + 1:02d}", "TWIN",
                    (x if pair % 2 == 0 else 1240, y + offset), 30, index,
                    ["near-neighbor", f"pair-{pair + 1:02d}", "same-text-distinct-line"],
                    padding=6,
                )
            )
    return canvas, annotations


def fixture_reading_order(font: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    canvas = make_canvas()
    annotations: list[dict[str, Any]] = []
    add_grid(
        canvas, font, "order", "ORDER", [300, 820, 1230, 1750],
        [360, 820, 1230, 1690], 32,
        ["reading-order", "multi-column", "cross-axis"], annotations,
    )
    return canvas, annotations


FIXTURES = {
    "tiled-small-text-2048": fixture_small_text,
    "tiled-dense-2048": fixture_dense,
    "tiled-horizontal-boundary-2048": fixture_horizontal_boundary,
    "tiled-vertical-boundary-2048": fixture_vertical_boundary,
    "tiled-four-way-intersection-2048": fixture_four_way,
    "tiled-original-edges-2048": fixture_original_edges,
    "tiled-near-neighbor-2048": fixture_near_neighbor,
    "tiled-reading-order-2048": fixture_reading_order,
}


def write_fixture(
    output: Path, fixture_id: str, image: np.ndarray,
    annotations: list[dict[str, Any]]
) -> dict[str, Any]:
    directory = output / fixture_id
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    pixels = np.ascontiguousarray(image, dtype=np.uint8).tobytes()
    (directory / "pixels.bin").write_bytes(pixels)
    # The expected order comes from authored quads, not an OCR result.  This is
    # the independently implemented 10-pixel row-band contract used by the
    # public reading-order definition.
    ordered = sorted(
        annotations,
        key=lambda value: (
            value["design"]["center"][1], value["design"]["center"][0], value["id"]
        ),
    )
    for index in range(len(ordered) - 1):
        current = index
        while current >= 0:
            following = ordered[current + 1]
            previous = ordered[current]
            if (
                abs(
                    following["design"]["center"][1]
                    - previous["design"]["center"][1]
                ) < 10
                and following["design"]["center"][0]
                < previous["design"]["center"][0]
            ):
                ordered[current], ordered[current + 1] = following, previous
                current -= 1
            else:
                break
    for order, annotation in enumerate(ordered):
        annotation["order"] = order
    fixture = {
        "schemaVersion": "1.0",
        "id": fixture_id,
        "corpusRevision": REVISION,
        "contractVersion": CONTRACT_VERSION,
        "plannerVectorSha256": PLANNER_VECTOR_SHA256,
        "width": IMAGE_SIDE,
        "height": IMAGE_SIDE,
        "stride": IMAGE_SIDE * 3,
        "pixelFormat": "bgr8",
        "pixelSha256": sha256(pixels),
        "rights": RIGHTS,
        "tags": sorted({tag for annotation in ordered for tag in annotation["tags"]}),
        "provenance": {
            "generator": "corpus/generate_tiled_corpus.py",
            "generatorSha256": sha256(Path(__file__).read_bytes()),
            "renderer": "Pillow plus OpenCV affine rasterization",
            "pillowVersion": PIL.__version__,
            "opencvVersion": cv2.__version__,
            "numpyVersion": np.__version__,
            "font": "NotoSansCJKjp-Regular.otf",
            "fontRevision": FONT_REVISION,
            "locale": "C.UTF-8",
            "randomSeed": None,
        },
        "annotations": ordered,
        "groundTruth": {
            "source": "reviewed-tiled-v1-layout",
            "annotationPolicy": (
                "Renderer glyph envelope expanded by each annotation's locked padding; "
                "expected text and order are authored before OCR execution."
            ),
            "lines": [annotation["text"] for annotation in ordered],
            "boxes": [annotation["quad"] for annotation in ordered],
        },
    }
    serialized = canonical(fixture) + b"\n"
    (directory / "fixture.json").write_bytes(serialized)
    return {
        "fixtureId": fixture_id,
        "pixelSha256": fixture["pixelSha256"],
        "fixtureSha256": sha256(serialized),
        "annotationsSha256": sha256(canonical(ordered)),
        "lineCount": len(ordered),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--font", type=Path,
        default=ROOT / ".cache" / "corpus" / "NotoSansCJKjp-Regular.otf",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "corpus" / "tiled-v1" / "fixtures",
    )
    parser.add_argument(
        "--lock", type=Path,
        default=ROOT / "corpus" / "tiled-v1" / "ground-truth.lock.json",
    )
    arguments = parser.parse_args()
    if not arguments.font.is_file():
        raise RuntimeError(f"locked Noto font is unavailable: {arguments.font}")
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for fixture_id, generator in FIXTURES.items():
        image, annotations = generator(arguments.font)
        records.append(write_fixture(arguments.output_dir, fixture_id, image, annotations))
    actual = {path.name for path in arguments.output_dir.iterdir() if path.is_dir()}
    if actual != set(FIXTURES):
        raise RuntimeError("tiled fixture directory set is not exact")
    lock = {
        "schemaVersion": "1.0",
        "revision": REVISION,
        "contractVersion": CONTRACT_VERSION,
        "plannerVectors": PLANNER_VECTORS,
        "plannerVectorSha256": PLANNER_VECTOR_SHA256,
        "font": {
            "name": "NotoSansCJKjp-Regular.otf",
            "revision": FONT_REVISION,
            "license": "OFL-1.1",
            "sha256": sha256(arguments.font.read_bytes()),
        },
        "reviewPolicy": (
            "Expected text, stable line identity, clockwise quads, tags, and reading order "
            "are authored from the renderer layout and must not be replaced by OCR output."
        ),
        "fixtures": records,
    }
    arguments.lock.parent.mkdir(parents=True, exist_ok=True)
    arguments.lock.write_bytes(canonical(lock) + b"\n")
    print(arguments.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
