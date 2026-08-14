from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_workspace.catalog import ensure_catalog
from media_workspace.db import (
    connect,
    delete_image_asset_from_catalog,
    get_similarity_group,
    get_visual_signature,
    init_db,
    list_similarity_groups,
    replace_similarity_group,
    set_similarity_group_status,
    upsert_visual_signature,
)


def _asset(connection, asset_id: str, root: Path, size: int) -> None:
    path = root / f"{asset_id}.jpg"
    path.write_bytes(b"x" * size)
    connection.execute(
        """
        INSERT INTO assets (
            asset_id, asset_type, canonical_path, stem, normalized_stem,
            stem_key, extension, fingerprint, file_size, modified_time
        ) VALUES (?, 'image', ?, ?, ?, ?, '.jpg', ?, ?, '2026-01-01')
        """,
        (asset_id, str(path), asset_id, asset_id, asset_id, f"fp-{asset_id}", size),
    )
    connection.execute(
        "INSERT INTO asset_files (file_id, asset_id, path, role) VALUES (?, ?, ?, 'primary')",
        (f"file-{asset_id}", asset_id, str(path)),
    )


class SimilarityDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = ensure_catalog(self.root / "similar.afcatalog")
        self.connection = connect(self.catalog.db_path)
        init_db(self.connection)
        _asset(self.connection, "image_a", self.root, 100)
        _asset(self.connection, "image_b", self.root, 40)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_signature_and_regions_replace_atomically(self) -> None:
        upsert_visual_signature(
            self.connection,
            asset_id="image_a",
            phash="0123456789abcdef",
            bands=(0x0123, 0x4567, 0x89AB, 0xCDEF),
            width=100,
            height=80,
            file_size=100,
            preview_fingerprint="preview-a",
            regions=[
                {
                    "region_kind": "full",
                    "normalized_rect": [0, 0, 1, 1],
                    "phash": "0123456789abcdef",
                    "bands": [0x0123, 0x4567, 0x89AB, 0xCDEF],
                }
            ],
        )

        signature = get_visual_signature(self.connection, "image_a")
        self.assertEqual(signature["phash"], "0123456789abcdef")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM visual_region_signatures WHERE asset_id = 'image_a'"
            ).fetchone()[0],
            1,
        )

    def test_group_status_and_asset_delete_cleanup(self) -> None:
        group_id = replace_similarity_group(
            self.connection,
            kind="compressed_family",
            representative_asset_id="image_a",
            probe_root_id=None,
            members=[
                {
                    "asset_id": "image_a",
                    "relation": "source",
                    "score": 1,
                    "file_size": 100,
                },
                {
                    "asset_id": "image_b",
                    "relation": "compressed_of",
                    "parent_asset_id": "image_a",
                    "score": 0.95,
                    "file_size": 40,
                    "evidence": {"hamming": 2},
                },
            ],
        )
        self.assertEqual(get_similarity_group(self.connection, group_id)["total_bytes"], 140)
        self.assertTrue(set_similarity_group_status(self.connection, group_id, "dismissed"))
        self.assertEqual(list_similarity_groups(self.connection, status="dismissed")[0]["group_id"], group_id)

        delete_image_asset_from_catalog(
            self.connection,
            self.catalog.root,
            "image_b",
        )
        remaining = get_similarity_group(self.connection, group_id)
        self.assertEqual([member["asset_id"] for member in remaining["members"]], ["image_a"])
        delete_image_asset_from_catalog(
            self.connection,
            self.catalog.root,
            "image_a",
        )
        self.assertIsNone(get_similarity_group(self.connection, group_id))


if __name__ == "__main__":
    unittest.main()
