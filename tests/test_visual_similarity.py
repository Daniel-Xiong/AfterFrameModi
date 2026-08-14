from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from media_workspace.visual_evaluation import evaluate_visual_truth
from media_workspace.visual_similarity import (
    VISUAL_ALGORITHM_VERSION,
    aligned_error,
    band_neighbors,
    fixed_region_rects,
    hamming_distance,
    perceptual_hash,
    phash_bands,
    phash_hex,
    region_hashes,
)


def _fixture_image(size: tuple[int, int] = (640, 480)) -> Image.Image:
    width, height = size
    x = np.linspace(0, 255, width, dtype=np.uint8)
    y = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[:, :, 0] = x
    pixels[:, :, 1] = y
    pixels[:, :, 2] = (x[None, :] // 2 + y // 2)
    image = Image.fromarray(pixels, mode="RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 70, 280, 310), outline="white", width=12)
    draw.ellipse((360, 90, 570, 300), fill=(220, 30, 80), outline="black", width=8)
    draw.line((0, height - 20, width, 20), fill=(20, 240, 120), width=16)
    return image


class VisualSimilarityTest(unittest.TestCase):
    def test_phash_is_deterministic_and_recompression_resistant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "original.jpg"
            compressed = root / "compressed.jpg"
            resized = root / "resized.jpg"
            image = _fixture_image()
            image.save(original, quality=95)
            image.save(compressed, quality=35)
            image.resize((320, 240), Image.Resampling.LANCZOS).save(resized, quality=45)

            original_hash = perceptual_hash(original)
            self.assertEqual(original_hash, perceptual_hash(original))
            self.assertLessEqual(hamming_distance(original_hash, perceptual_hash(compressed)), 8)
            self.assertLessEqual(hamming_distance(original_hash, perceptual_hash(resized)), 8)
            self.assertLess(aligned_error(original, compressed), 0.08)
            self.assertEqual(len(phash_hex(original_hash)), 16)
            self.assertEqual(len(phash_bands(original_hash)), 4)
            self.assertEqual(VISUAL_ALGORITHM_VERSION, "dct-phash-v1")

    def test_band_neighbors_cover_radius_two(self) -> None:
        value = 0x1234
        neighbors = set(band_neighbors(value, radius=2))
        self.assertEqual(len(neighbors), 1 + 16 + 120)
        self.assertIn(value ^ 1 ^ 2, neighbors)

    def test_region_signatures_are_fixed_and_bounded(self) -> None:
        regions = fixed_region_rects()
        signatures = region_hashes(_fixture_image(), regions)
        self.assertEqual(len(signatures), len(regions))
        self.assertLessEqual(len(signatures), 16)
        self.assertTrue(all(len(item["bands"]) == 4 for item in signatures))

    def test_visual_truth_reports_threshold_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "original.jpg"
            compressed = root / "compressed.jpg"
            negative = root / "negative.jpg"
            image = _fixture_image()
            image.save(original, quality=95)
            image.save(compressed, quality=30)
            Image.new("RGB", image.size, "navy").save(negative, quality=90)
            truth = root / "truth.csv"
            with truth.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["probe_path", "gallery_path", "relation", "notes"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "probe_path": compressed,
                        "gallery_path": original,
                        "relation": "compressed_of",
                        "notes": "quality change",
                    }
                )
                writer.writerow(
                    {
                        "probe_path": negative,
                        "gallery_path": original,
                        "relation": "negative",
                        "notes": "unrelated",
                    }
                )

            report = evaluate_visual_truth(truth, max_hamming=8)
            self.assertEqual(report["summary"]["total"], 2)
            self.assertEqual(len(report["threshold_metrics"]), 9)
            self.assertEqual(report["summary"]["relations"]["compressed_of"], 1)


if __name__ == "__main__":
    unittest.main()
