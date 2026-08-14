from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from media_workspace.catalog import ensure_catalog
from media_workspace.db import (
    assign_asset_root_membership,
    connect,
    create_job,
    init_db,
    list_similarity_groups,
    upsert_catalog_root,
    upsert_preview_entry,
)
from media_workspace.job_runner import run_visual_match_job
from media_workspace.visual_cleanup import _bounded_edges, _complete_link_components, run_visual_cleanup
from media_workspace.visual_index import index_visual_signatures
from media_workspace.visual_matcher import recall_visual_candidates


def _image() -> Image.Image:
    pixels = np.zeros((300, 400, 3), dtype=np.uint8)
    pixels[:, :, 0] = np.linspace(0, 255, 400, dtype=np.uint8)
    pixels[:, :, 1] = np.linspace(255, 0, 300, dtype=np.uint8)[:, None]
    image = Image.fromarray(pixels, "RGB")
    draw = ImageDraw.Draw(image)
    draw.ellipse((80, 50, 260, 230), fill=(240, 40, 80))
    draw.rectangle((280, 120, 390, 290), fill=(20, 220, 130))
    return image


class VisualCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = ensure_catalog(self.root / "visual.afcatalog")
        self.connection = connect(self.catalog.db_path)
        init_db(self.connection)
        self.probe_dir = self.root / "probe"
        self.gallery_dir = self.root / "gallery"
        self.probe_dir.mkdir()
        self.gallery_dir.mkdir()
        self.probe_root = upsert_catalog_root(
            self.connection,
            "image",
            self.probe_dir,
            user_declared=True,
        )
        self.gallery_root = upsert_catalog_root(
            self.connection,
            "image",
            self.gallery_dir,
            user_declared=True,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _add(self, asset_id: str, directory: Path, preview_quality: int, file_size: int) -> None:
        source = directory / f"{asset_id}.jpg"
        _image().save(source, quality=preview_quality)
        metadata = {
            "width": 400,
            "height": 300,
            "camera_model": "Fixture Camera",
            "capture_time": "2026-01-01T12:00:00",
        }
        self.connection.execute(
            """
            INSERT INTO assets (
                asset_id, asset_type, canonical_path, stem, normalized_stem,
                stem_key, extension, fingerprint, file_size, modified_time,
                metadata_json
            ) VALUES (?, 'image', ?, ?, ?, ?, '.jpg', ?, ?, '2026-01-01', ?)
            """,
            (
                asset_id,
                str(source),
                asset_id,
                asset_id,
                asset_id,
                f"fp-{asset_id}",
                file_size,
                json.dumps(metadata),
            ),
        )
        self.connection.execute(
            "INSERT INTO asset_files (file_id, asset_id, path, role) VALUES (?, ?, ?, 'primary')",
            (f"file-{asset_id}", asset_id, str(source)),
        )
        assign_asset_root_membership(self.connection, asset_id, commit=False)
        preview = self.catalog.previews_dir / asset_id[:2] / f"{asset_id}.jpg"
        preview.parent.mkdir(parents=True, exist_ok=True)
        _image().save(preview, quality=preview_quality)
        upsert_preview_entry(
            self.connection,
            asset_id,
            kind="preview",
            relative_path=str(preview.relative_to(self.catalog.root)),
            width=400,
            height=300,
            status="ready",
            commit=False,
        )
        self.connection.commit()

    def test_index_recall_and_cleanup_are_bounded(self) -> None:
        self._add("probe_small", self.probe_dir, 30, 20_000)
        self._add("gallery_original", self.gallery_dir, 95, 200_000)
        report = index_visual_signatures(
            self.connection,
            self.catalog,
            root_ids=[self.probe_root, self.gallery_root],
            include_raw=False,
        )
        self.assertEqual(report["indexed"], 2)
        candidates = recall_visual_candidates(
            self.connection,
            "probe_small",
            probe_root_id=self.probe_root,
        )
        self.assertLessEqual(len(candidates), 64)
        self.assertEqual(candidates[0]["asset_id"], "gallery_original")

        result = run_visual_cleanup(
            self.connection,
            self.catalog,
            probe_root_id=self.probe_root,
            include_raw_proposals=False,
        )
        self.assertEqual(result["processed"], 1)
        groups = list_similarity_groups(self.connection)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "compressed_family")
        self.assertEqual(groups[0]["representative_asset_id"], "gallery_original")

    def test_visual_match_job_completes(self) -> None:
        self._add("probe_small", self.probe_dir, 35, 25_000)
        self._add("gallery_original", self.gallery_dir, 95, 220_000)
        job = create_job(
            self.connection,
            "visual_match",
            payload={"probe_root_id": self.probe_root},
        )

        result = run_visual_match_job(
            self.connection,
            self.catalog.root,
            str(job["job_id"]),
            probe_root_id=self.probe_root,
            include_raw_proposals=False,
        )

        self.assertEqual(result["processed"], 1)
        stored = self.connection.execute(
            "SELECT status, progress FROM jobs WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["progress"], 1.0)

    def test_complete_link_does_not_bridge_dissimilar_endpoints(self) -> None:
        def edge(left: str, right: str, score: float):
            return {
                "left": {"asset_id": left},
                "right": {"asset_id": right},
                "classification": {
                    "kind": "near_duplicate",
                    "score": score,
                    "relation": "visually_similar",
                },
            }

        chain = [edge("a", "b", 0.9), edge("b", "c", 0.89)]
        bounded = _bounded_edges(chain, max_neighbors=4)
        groups = _complete_link_components(bounded, max_members=10)

        self.assertEqual(len(groups), 1)
        member_ids = {
            row[side]["asset_id"]
            for row in groups[0]
            for side in ("left", "right")
        }
        self.assertEqual(member_ids, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
