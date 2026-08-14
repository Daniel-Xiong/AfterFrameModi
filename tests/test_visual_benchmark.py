from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_workspace.catalog import ensure_catalog
from media_workspace.config import VisualThresholds
from media_workspace.db import (
    assign_asset_root_membership,
    connect,
    init_db,
    upsert_catalog_root,
    upsert_preview_entry,
)
from media_workspace.visual_benchmark import benchmark_visual_cleanup


class VisualBenchmarkTest(unittest.TestCase):
    def test_benchmark_reports_stage_metrics_and_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = ensure_catalog(root / "bench.afcatalog")
            connection = connect(catalog.db_path)
            init_db(connection)
            probe_dir = root / "probe"
            gallery_dir = root / "gallery"
            probe_dir.mkdir()
            gallery_dir.mkdir()
            probe_root = upsert_catalog_root(
                connection,
                "image",
                probe_dir,
                user_declared=True,
            )
            gallery_root = upsert_catalog_root(
                connection,
                "image",
                gallery_dir,
                user_declared=True,
            )

            for index in range(3):
                for asset_id, directory, quality, size in (
                    (f"probe_{index}", probe_dir, 40 + index, 20_000),
                    (f"gallery_{index}", gallery_dir, 90, 180_000),
                ):
                    source = directory / f"{asset_id}.jpg"
                    source.write_bytes(
                        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                        b"\xff\xc0\x00\x11\x08\x03\x00\x04\x00\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
                    )
                    connection.execute(
                        """
                        INSERT INTO assets (
                            asset_id, asset_type, canonical_path, stem, normalized_stem,
                            stem_key, extension, fingerprint, file_size, modified_time,
                            metadata_json
                        ) VALUES (?, 'image', ?, ?, ?, ?, '.jpg', ?, ?, '2026-01-01', '{}')
                        """,
                        (
                            asset_id,
                            str(source),
                            asset_id,
                            asset_id,
                            asset_id,
                            f"fp-{asset_id}",
                            size,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO asset_files (file_id, asset_id, path, role)
                        VALUES (?, ?, ?, 'primary')
                        """,
                        (f"file-{asset_id}", asset_id, str(source)),
                    )
                    assign_asset_root_membership(connection, asset_id, commit=False)
                    preview = catalog.previews_dir / asset_id[:2] / f"{asset_id}.jpg"
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    preview.write_bytes(source.read_bytes())
                    upsert_preview_entry(
                        connection,
                        asset_id,
                        kind="preview",
                        relative_path=str(preview.relative_to(catalog.root)),
                        width=400,
                        height=300,
                        status="ready",
                        commit=False,
                    )
            connection.commit()

            result = benchmark_visual_cleanup(
                connection,
                catalog,
                probe_root_id=probe_root,
                thresholds=VisualThresholds(recall_limit=16),
                include_raw_proposals=False,
                track_memory=True,
            )

            self.assertIn("visual_index", result["stages"])
            self.assertIn("visual_match", result["stages"])
            self.assertGreaterEqual(result["stages"]["visual_index"]["processed"], 3)
            self.assertTrue(result["invariants"]["candidate_count_within_budget"])
            self.assertIsNotNone(result["memory_peak_kb"])
            self.assertEqual(result["probe_root_id"], probe_root)
            self.assertGreater(result["stages"]["visual_match"]["elapsed_seconds"], 0.0)
            connection.close()


if __name__ == "__main__":
    unittest.main()
