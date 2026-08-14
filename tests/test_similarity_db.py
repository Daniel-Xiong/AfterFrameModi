from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_workspace.catalog import ensure_catalog
from media_workspace.db import (
    add_asset_to_resource_set,
    confirm_raw_similarity_proposal,
    confirm_similarity_group,
    connect,
    create_resource_set,
    delete_image_asset_from_catalog,
    get_similarity_group,
    get_resource_set_for_asset,
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

    def test_confirm_merges_non_singleton_families(self) -> None:
        _asset(self.connection, "image_a_edit", self.root, 80)
        _asset(self.connection, "image_b_edit", self.root, 30)
        set_a = create_resource_set(self.connection, "image_a", commit=False)
        add_asset_to_resource_set(
            self.connection,
            set_a,
            "image_a_edit",
            role="version",
            version_kind="edit",
            parent_asset_id="image_a",
            commit=False,
        )
        set_b = create_resource_set(self.connection, "image_b", commit=False)
        add_asset_to_resource_set(
            self.connection,
            set_b,
            "image_b_edit",
            role="version",
            version_kind="edit",
            parent_asset_id="image_b",
            commit=False,
        )
        group_id = replace_similarity_group(
            self.connection,
            kind="compressed_family",
            representative_asset_id="image_a",
            probe_root_id=None,
            members=[
                {"asset_id": "image_a", "relation": "source", "score": 1},
                {
                    "asset_id": "image_b",
                    "relation": "compressed_of",
                    "parent_asset_id": "image_a",
                    "score": 0.98,
                },
            ],
            commit=False,
        )
        self.connection.commit()

        result = confirm_similarity_group(self.connection, group_id)

        target_set = get_resource_set_for_asset(self.connection, "image_a")
        self.assertEqual(result["resource_set_id"], target_set["set_id"])
        for asset_id in ("image_a_edit", "image_b", "image_b_edit"):
            self.assertEqual(
                get_resource_set_for_asset(self.connection, asset_id)["set_id"],
                target_set["set_id"],
            )
        self.assertEqual(get_similarity_group(self.connection, group_id)["status"], "reviewed")
        link = self.connection.execute(
            """
            SELECT relation_type FROM asset_links
            WHERE parent_asset_id = 'image_a' AND child_asset_id = 'image_b'
            """
        ).fetchone()
        self.assertEqual(link["relation_type"], "compressed_of")

    def test_confirm_raw_proposal_uses_registry_confirmation(self) -> None:
        raw_path = self.root / "raw_a.cr3"
        raw_path.write_bytes(b"raw")
        self.connection.execute(
            """
            INSERT INTO assets (
                asset_id, asset_type, canonical_path, stem, normalized_stem,
                stem_key, extension, fingerprint, file_size, modified_time
            ) VALUES ('raw_a', 'raw', ?, 'raw_a', 'raw_a', 'raw_a', '.cr3', 'raw-fp', 3, '2026-01-01')
            """,
            (str(raw_path),),
        )
        image_path = str((self.root / "image_a.jpg").resolve())
        self.connection.execute(
            """
            INSERT INTO image_lookup_registry (
                image_path, image_asset_id, match_status, score, resolver_version
            ) VALUES (?, 'image_a', 'unmatched', 0, 'test')
            """,
            (image_path,),
        )
        group_id = replace_similarity_group(
            self.connection,
            kind="raw_proposal",
            representative_asset_id="image_a",
            probe_root_id=None,
            members=[
                {"asset_id": "image_a", "relation": "source", "score": 1},
                {
                    "asset_id": "raw_a",
                    "relation": "raw_candidate",
                    "parent_asset_id": "image_a",
                    "score": 0.9,
                },
            ],
            commit=False,
        )
        self.connection.commit()

        confirm_raw_similarity_proposal(self.connection, group_id)

        registry = self.connection.execute(
            "SELECT raw_asset_id, match_status FROM image_lookup_registry WHERE image_asset_id = 'image_a'"
        ).fetchone()
        self.assertEqual(registry["raw_asset_id"], "raw_a")
        self.assertEqual(registry["match_status"], "manual_confirmed")


if __name__ == "__main__":
    unittest.main()
