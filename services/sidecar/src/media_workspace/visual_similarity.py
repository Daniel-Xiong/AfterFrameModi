from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageOps


VISUAL_ALGORITHM_VERSION = "dct-phash-v1"
PHASH_SIZE = 8
PHASH_INPUT_SIZE = 32
PHASH_BITS = PHASH_SIZE * PHASH_SIZE
PHASH_BAND_BITS = 16


def _open_image(source: Path | str | Image.Image) -> Image.Image:
    if isinstance(source, Image.Image):
        return source.copy()
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass
    with Image.open(Path(source)) as image:
        return image.copy()


def normalize_preview(
    source: Path | str | Image.Image,
    *,
    size: tuple[int, int] = (PHASH_INPUT_SIZE, PHASH_INPUT_SIZE),
) -> Image.Image:
    image = ImageOps.exif_transpose(_open_image(source)).convert("L")
    return image.resize(size, Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def _dct_matrix(size: int) -> np.ndarray:
    coordinates = np.arange(size, dtype=np.float64)
    frequencies = coordinates[:, None]
    matrix = np.cos((np.pi / size) * (coordinates + 0.5) * frequencies)
    matrix[0, :] *= np.sqrt(1.0 / size)
    matrix[1:, :] *= np.sqrt(2.0 / size)
    return matrix


def perceptual_hash(source: Path | str | Image.Image) -> int:
    normalized = normalize_preview(source)
    pixels = np.asarray(normalized, dtype=np.float64)
    transform = _dct_matrix(PHASH_INPUT_SIZE)
    dct = transform @ pixels @ transform.T
    low = dct[:PHASH_SIZE, :PHASH_SIZE].flatten()
    median = float(np.median(low[1:]))
    value = 0
    for coefficient in low:
        value = (value << 1) | int(coefficient > median)
    return value


def phash_hex(value: int) -> str:
    return f"{int(value) & ((1 << PHASH_BITS) - 1):016x}"


def parse_phash(value: str | int) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


def phash_bands(value: str | int) -> tuple[int, int, int, int]:
    integer = parse_phash(value)
    mask = (1 << PHASH_BAND_BITS) - 1
    return tuple(
        (integer >> shift) & mask
        for shift in (PHASH_BAND_BITS * 3, PHASH_BAND_BITS * 2, PHASH_BAND_BITS, 0)
    )


def hamming_distance(left: str | int, right: str | int) -> int:
    return (parse_phash(left) ^ parse_phash(right)).bit_count()


def band_neighbors(value: int, radius: int = 2) -> tuple[int, ...]:
    if radius < 0 or radius > 2:
        raise ValueError("band radius must be between 0 and 2")
    values = {int(value)}
    if radius >= 1:
        for first in range(PHASH_BAND_BITS):
            values.add(value ^ (1 << first))
    if radius >= 2:
        for first in range(PHASH_BAND_BITS):
            for second in range(first + 1, PHASH_BAND_BITS):
                values.add(value ^ (1 << first) ^ (1 << second))
    return tuple(sorted(values))


def aligned_error(left: Path | str | Image.Image, right: Path | str | Image.Image) -> float:
    left_pixels = np.asarray(normalize_preview(left, size=(128, 128)), dtype=np.float32)
    right_pixels = np.asarray(normalize_preview(right, size=(128, 128)), dtype=np.float32)
    return round(float(np.mean(np.abs(left_pixels - right_pixels)) / 255.0), 6)


def fixed_region_rects() -> tuple[tuple[str, tuple[float, float, float, float]], ...]:
    """Bounded crop-recall regions expressed as normalized x/y/width/height."""
    regions: list[tuple[str, tuple[float, float, float, float]]] = [
        ("full", (0.0, 0.0, 1.0, 1.0)),
    ]
    for ratio_name, target_ratio in (("square", 1.0), ("four-five", 0.8), ("three-four", 0.75)):
        # These normalized boxes assume the common landscape parent. Portrait
        # variants are covered by the same centered/edge anchors after the
        # caller maps against source dimensions.
        width = min(1.0, target_ratio)
        for anchor, x in (("left", 0.0), ("center", (1.0 - width) / 2.0), ("right", 1.0 - width)):
            regions.append((f"{ratio_name}-{anchor}", (x, 0.0, width, 1.0)))
    for scale in (0.9, 0.8, 0.7):
        offset = (1.0 - scale) / 2.0
        regions.append((f"center-{int(scale * 100)}", (offset, offset, scale, scale)))
    return tuple(regions)


def region_hashes(
    source: Path | str | Image.Image,
    regions: Iterable[tuple[str, tuple[float, float, float, float]]] | None = None,
) -> list[dict[str, object]]:
    image = ImageOps.exif_transpose(_open_image(source)).convert("RGB")
    width, height = image.size
    output: list[dict[str, object]] = []
    for kind, (x, y, region_width, region_height) in regions or fixed_region_rects():
        box = (
            max(0, round(x * width)),
            max(0, round(y * height)),
            min(width, round((x + region_width) * width)),
            min(height, round((y + region_height) * height)),
        )
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            continue
        value = perceptual_hash(image.crop(box))
        output.append(
            {
                "region_kind": kind,
                "normalized_rect": [x, y, region_width, region_height],
                "phash": phash_hex(value),
                "bands": list(phash_bands(value)),
            }
        )
    return output


def bounded_crop_match(
    child_source: Path | str | Image.Image,
    parent_source: Path | str | Image.Image,
    *,
    scales: tuple[float, ...] = (0.9, 0.8, 0.7, 0.6, 0.5),
    grid_size: int = 5,
    retain: int = 4,
    angles: tuple[float, ...] = (-3.0, 0.0, 3.0),
) -> dict[str, object]:
    """Coarse-to-fine crop confirmation with fixed operation bounds."""
    child = ImageOps.exif_transpose(_open_image(child_source)).convert("L")
    parent = ImageOps.exif_transpose(_open_image(parent_source)).convert("L")
    parent_width, parent_height = parent.size
    child_ratio = child.width / child.height
    coarse_child = np.asarray(child.resize((64, 64), Image.Resampling.LANCZOS), dtype=np.float32)
    coarse: list[tuple[float, tuple[int, int, int, int]]] = []
    operations = 0
    for scale in scales:
        crop_width = max(16, round(parent_width * scale))
        crop_height = max(16, round(crop_width / child_ratio))
        if crop_height > parent_height:
            crop_height = max(16, round(parent_height * scale))
            crop_width = max(16, round(crop_height * child_ratio))
        if crop_width > parent_width or crop_height > parent_height:
            continue
        x_steps = np.linspace(0, parent_width - crop_width, grid_size, dtype=int)
        y_steps = np.linspace(0, parent_height - crop_height, grid_size, dtype=int)
        for x in x_steps:
            for y in y_steps:
                box = (int(x), int(y), int(x + crop_width), int(y + crop_height))
                region = parent.crop(box).resize((64, 64), Image.Resampling.LANCZOS)
                error = float(
                    np.mean(np.abs(np.asarray(region, dtype=np.float32) - coarse_child)) / 255.0
                )
                coarse.append((error, box))
                operations += 1
    coarse.sort(key=lambda item: item[0])
    finalists = coarse[: max(1, retain)]
    fine_child = child.resize((128, 128), Image.Resampling.LANCZOS)
    best: tuple[float, tuple[int, int, int, int], float] | None = None
    for _, box in finalists:
        region = parent.crop(box).resize((128, 128), Image.Resampling.LANCZOS)
        for angle in angles:
            rotated = (
                fine_child
                if angle == 0
                else fine_child.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
            )
            error = float(
                np.mean(
                    np.abs(
                        np.asarray(region, dtype=np.float32)
                        - np.asarray(rotated, dtype=np.float32)
                    )
                )
                / 255.0
            )
            operations += 1
            candidate = (error, box, angle)
            if best is None or candidate[0] < best[0]:
                best = candidate
    if best is None:
        return {"error": 1.0, "normalized_rect": None, "angle": 0.0, "operations": operations}
    error, box, angle = best
    return {
        "error": round(error, 6),
        "normalized_rect": [
            box[0] / parent_width,
            box[1] / parent_height,
            (box[2] - box[0]) / parent_width,
            (box[3] - box[1]) / parent_height,
        ],
        "angle": angle,
        "operations": operations,
    }
