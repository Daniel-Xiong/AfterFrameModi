from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from media_workspace.catalog import ensure_catalog
from media_workspace.db import (
    attach_asset_to_resource_set,
    connect,
    init_db,
    list_image_assets,
    list_similar_clusters,
    rebuild_capture_graph,
    set_burst_keeper,
    set_catalog_path,
)
from media_workspace.derived import create_derived_crop, register_image_file
from media_workspace.metadata import stable_asset_id


def _insert_browseable(
    connection: sqlite3.Connection,
    *,
    path: str,
    asset_type: str = "image",
    stem: str | None = None,
    fingerprint: str | None = None,
    capture_time: str | None = None,
    camera_model: str | None = None,
    rating: int = 0,
    paired_raw_id: str | None = None,
) -> str:
    stem = stem or Path(path).stem
    fingerprint = fingerprint or f"fp-{stem}-{asset_type}"
    prefix = "raw" if asset_type == "raw" else ("video" if asset_type == "video" else "image")
    asset_id = stable_asset_id(prefix, fingerprint, path)
    meta = {
        "capture_time": capture_time,
        "camera_model": camera_model,
        "width": 800,
        "height": 600,
    }
    connection.execute(
        """
        INSERT INTO assets (
            asset_id, asset_type, canonical_path, stem, normalized_stem, stem_key,
            extension, fingerprint, file_size, modified_time, metadata_json, app_rating
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id, asset_type, path, stem, stem.lower(), stem.lower(),
            Path(path).suffix.lower() or ".jpg", fingerprint, 100, "2026-01-01T00:00:00",
            json.dumps(meta), rating,
        ),
    )
    connection.execute(
        "INSERT INTO asset_files (file_id, asset_id, path, role) VALUES (?, ?, ?, 'primary')",
        (f"file_{asset_id[:16]}", asset_id, path),
    )
    connection.execute(
        """
        INSERT INTO image_lookup_registry (
            image_path, image_asset_id, raw_asset_id, match_status, score, resolver_version
        ) VALUES (?, ?, ?, ?, 0, 'test')
        """,
        (path, asset_id, paired_raw_id, "auto_bound" if paired_raw_id else "unmatched"),
    )
    attach_asset_to_resource_set(connection, asset_id, version_kind="import", commit=False)
    return asset_id


def _write_jpeg(path: Path) -> None:
    Image.new("RGB", (640, 480), (80, 120, 160)).save(path, quality=85)


class CaptureGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        init_db(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_every_capture_unit_has_a_version_set(self) -> None:
        jpeg = _insert_browseable(
            self.connection, path="/shots/a.jpg", stem="a", capture_time="2026-01-01T10:00:00",
        )
        stats = rebuild_capture_graph(self.connection)
        self.assertEqual(stats["capture_units"], 1)
        unit = self.connection.execute("SELECT * FROM capture_units").fetchone()
        self.assertEqual(unit["display_asset_id"], jpeg)
        self.assertTrue(unit["resource_set_id"])
        members = self.connection.execute(
            "SELECT role FROM capture_unit_members WHERE unit_id = ?", (unit["unit_id"],)
        ).fetchall()
        self.assertEqual([row["role"] for row in members], ["display"])

    def test_jpeg_and_raw_same_stem_merge_to_one_unit(self) -> None:
        jpeg = _insert_browseable(
            self.connection, path="/shots/IMG_001.jpg", stem="IMG_001",
            capture_time="2026-01-01T10:00:00", camera_model="Canon",
        )
        raw = _insert_browseable(
            self.connection, path="/shots/IMG_001.CR2", asset_type="raw", stem="IMG_001",
            capture_time="2026-01-01T10:00:00", camera_model="Canon",
        )
        rebuild_capture_graph(self.connection)
        units = self.connection.execute("SELECT * FROM capture_units").fetchall()
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["display_asset_id"], jpeg)
        roles = {
            row["asset_id"]: row["role"]
            for row in self.connection.execute("SELECT asset_id, role FROM capture_unit_members")
        }
        self.assertEqual(roles[jpeg], "display")
        self.assertEqual(roles[raw], "raw")
        visible = list_image_assets(self.connection, "all", representatives=True)
        self.assertEqual([row["asset_id"] for row in visible], [jpeg])
        hidden = list_image_assets(self.connection, "all", representatives=False)
        self.assertEqual({row["asset_id"] for row in hidden}, {jpeg, raw})

    def test_crop_stays_in_same_unit_and_hides_from_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = ensure_catalog(root / "demo.afcatalog")
            connection = connect(catalog.db_path)
            init_db(connection)
            set_catalog_path(connection, catalog.root)
            source = root / "shot.jpg"
            _write_jpeg(source)
            registered = register_image_file(connection, catalog, source)
            derived = create_derived_crop(connection, catalog, registered["asset_id"], "1:1")
            units = connection.execute("SELECT unit_id, display_asset_id FROM capture_units").fetchall()
            self.assertEqual(len(units), 1)
            self.assertEqual(units[0]["display_asset_id"], registered["asset_id"])
            member_ids = {
                row["asset_id"]
                for row in connection.execute(
                    "SELECT asset_id FROM capture_unit_members WHERE unit_id = ?",
                    (units[0]["unit_id"],),
                )
            }
            self.assertIn(registered["asset_id"], member_ids)
            self.assertIn(derived["asset_id"], member_ids)
            visible = list_image_assets(connection, "all", representatives=True)
            self.assertEqual([row["asset_id"] for row in visible], [registered["asset_id"]])
            connection.close()

    def test_burst_groups_consecutive_same_camera_shots(self) -> None:
        first = _insert_browseable(
            self.connection, path="/shots/b1.jpg", stem="b1",
            capture_time="2026-01-01T10:00:00", camera_model="Sony", rating=2,
        )
        second = _insert_browseable(
            self.connection, path="/shots/b2.jpg", stem="b2",
            capture_time="2026-01-01T10:00:01", camera_model="Sony", rating=5,
        )
        third = _insert_browseable(
            self.connection, path="/shots/b3.jpg", stem="b3",
            capture_time="2026-01-01T10:00:02", camera_model="Sony", rating=1,
        )
        _insert_browseable(
            self.connection, path="/shots/later.jpg", stem="later",
            capture_time="2026-01-01T11:00:00", camera_model="Sony",
        )
        stats = rebuild_capture_graph(self.connection)
        self.assertEqual(stats["capture_units"], 4)
        self.assertEqual(stats["burst_groups"], 1)
        burst = self.connection.execute("SELECT * FROM burst_groups").fetchone()
        self.assertEqual(burst["member_count"], 3)
        self.assertEqual(burst["display_asset_id"], second)  # highest rating
        items = self.connection.execute(
            "SELECT resource_set_id, unit_id, is_keeper FROM burst_group_items ORDER BY sort_order"
        ).fetchall()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(row["resource_set_id"] for row in items))
        visible = [row["asset_id"] for row in list_image_assets(self.connection, "all")]
        self.assertIn(second, visible)
        self.assertNotIn(first, visible)
        self.assertNotIn(third, visible)
        set_burst_keeper(self.connection, burst["group_id"], first)
        cover = self.connection.execute(
            "SELECT display_asset_id FROM burst_groups WHERE group_id = ?",
            (burst["group_id"],),
        ).fetchone()["display_asset_id"]
        self.assertEqual(cover, first)

    def test_similar_clusters_fingerprint_not_bursts(self) -> None:
        a = _insert_browseable(
            self.connection, path="/shots/copy-a.jpg", stem="copy-a",
            fingerprint="same-bytes", capture_time="2026-02-01T10:00:00", camera_model="Leica",
        )
        b = _insert_browseable(
            self.connection, path="/export/copy-b.jpg", stem="copy-b",
            fingerprint="same-bytes", capture_time="2026-02-01T12:00:00", camera_model="Leica",
        )
        burst_a = _insert_browseable(
            self.connection, path="/shots/c1.jpg", stem="c1",
            capture_time="2026-03-01T10:00:00", camera_model="Nikon",
        )
        burst_b = _insert_browseable(
            self.connection, path="/shots/c2.jpg", stem="c2",
            capture_time="2026-03-01T10:00:01", camera_model="Nikon",
        )
        rebuild_capture_graph(self.connection)
        clusters = list_similar_clusters(self.connection)
        fingerprint = [c for c in clusters if c["reason"] == "fingerprint"]
        self.assertEqual(len(fingerprint), 1)
        self.assertEqual(set(fingerprint[0]["asset_ids"]), {a, b})
        # Burst mates must not appear as a similar cluster.
        for cluster in clusters:
            members = set(cluster["asset_ids"])
            self.assertFalse({burst_a, burst_b}.issubset(members))

    def test_sidecar_xmp_is_recorded_on_the_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            jpeg = Path(temp_dir) / "IMG_009.jpg"
            xmp = Path(temp_dir) / "IMG_009.xmp"
            jpeg.write_bytes(b"not-a-real-jpeg")
            xmp.write_text("<x:xmpmeta/>")
            _insert_browseable(
                self.connection, path=str(jpeg), stem="IMG_009",
                capture_time="2026-01-01T10:00:00",
            )
            rebuild_capture_graph(self.connection)
            paths = [row["path"] for row in self.connection.execute("SELECT path FROM capture_unit_sidecars")]
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith("IMG_009.xmp"))

    def test_rebuild_is_idempotent(self) -> None:
        _insert_browseable(self.connection, path="/shots/one.jpg", stem="one")
        first = rebuild_capture_graph(self.connection)
        second = rebuild_capture_graph(self.connection)
        self.assertEqual(first, second)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM capture_units").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
