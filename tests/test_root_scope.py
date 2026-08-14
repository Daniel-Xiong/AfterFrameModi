from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_workspace.catalog import ensure_catalog
from media_workspace.db import (
    assign_asset_root_membership,
    connect,
    init_db,
    list_assets_in_roots,
    list_gallery_assets_except_roots,
    list_user_catalog_roots,
    upsert_catalog_root,
)


def _insert_asset(connection, asset_id: str, path: Path, asset_type: str = "image") -> None:
    connection.execute(
        """
        INSERT INTO assets (
            asset_id, asset_type, canonical_path, stem, normalized_stem,
            stem_key, extension, fingerprint, file_size, modified_time
        ) VALUES (?, ?, ?, ?, ?, ?, '.jpg', ?, 1, '2026-01-01')
        """,
        (asset_id, asset_type, str(path.resolve()), path.stem, path.stem, path.stem, f"fp-{asset_id}"),
    )


class RootScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.catalog = ensure_catalog(root / "scope.afcatalog")
        self.connection = connect(self.catalog.db_path)
        init_db(self.connection)
        self.library = root / "library"
        self.probe = self.library / "phone"
        self.other = root / "other"
        self.probe.mkdir(parents=True)
        self.other.mkdir()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_longest_user_declared_root_owns_nested_asset(self) -> None:
        library_id = upsert_catalog_root(
            self.connection,
            "image",
            self.library,
            user_declared=True,
        )
        probe_id = upsert_catalog_root(
            self.connection,
            "image",
            self.probe,
            user_declared=True,
        )
        path = self.probe / "IMG_0001.jpg"
        _insert_asset(self.connection, "image_probe", path)

        selected = assign_asset_root_membership(self.connection, "image_probe")

        self.assertEqual(selected, probe_id)
        row = self.connection.execute(
            "SELECT root_id, relative_path FROM asset_root_memberships WHERE asset_id = ?",
            ("image_probe",),
        ).fetchone()
        self.assertEqual(row["root_id"], probe_id)
        self.assertEqual(row["relative_path"], "IMG_0001.jpg")
        self.assertNotEqual(row["root_id"], library_id)

    def test_implicit_and_inactive_roots_are_not_scope_choices(self) -> None:
        implicit_id = upsert_catalog_root(self.connection, "image", self.other)
        explicit_id = upsert_catalog_root(
            self.connection,
            "image",
            self.library,
            user_declared=True,
        )
        self.connection.execute(
            "UPDATE catalog_roots SET is_active = 0 WHERE root_id = ?",
            (explicit_id,),
        )
        self.connection.commit()

        self.assertEqual(list_user_catalog_roots(self.connection, root_type="image"), [])
        implicit = self.connection.execute(
            "SELECT user_declared FROM catalog_roots WHERE root_id = ?",
            (implicit_id,),
        ).fetchone()
        self.assertEqual(implicit["user_declared"], 0)

    def test_probe_and_gallery_scope_are_disjoint(self) -> None:
        probe_id = upsert_catalog_root(
            self.connection,
            "image",
            self.probe,
            user_declared=True,
        )
        other_id = upsert_catalog_root(
            self.connection,
            "image",
            self.other,
            user_declared=True,
        )
        _insert_asset(self.connection, "image_probe", self.probe / "probe.jpg")
        _insert_asset(self.connection, "image_other", self.other / "other.jpg")
        assign_asset_root_membership(self.connection, "image_probe")
        assign_asset_root_membership(self.connection, "image_other")

        probe = list_assets_in_roots(self.connection, [probe_id])
        gallery = list_gallery_assets_except_roots(self.connection, [probe_id])

        self.assertEqual([row["asset_id"] for row in probe], ["image_probe"])
        self.assertEqual([row["asset_id"] for row in gallery], ["image_other"])
        self.assertNotEqual(probe_id, other_id)


if __name__ == "__main__":
    unittest.main()
