from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_workspace.catalog import ensure_catalog
from media_workspace.db import (
    connect,
    create_relocation_operation,
    init_db,
    list_relocation_operations,
    update_relocation_operation,
)


class RelocationJournalTest(unittest.TestCase):
    def test_journal_tracks_recoverable_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = ensure_catalog(root / "relocate.afcatalog")
            connection = connect(catalog.db_path)
            self.addCleanup(connection.close)
            init_db(connection)
            source = root / "source.jpg"
            source.write_bytes(b"source")
            connection.execute(
                """
                INSERT INTO assets (
                    asset_id, asset_type, canonical_path, stem, normalized_stem,
                    stem_key, extension, fingerprint, file_size, modified_time
                ) VALUES ('image_a', 'image', ?, 'source', 'source', 'source', '.jpg', 'fp', 6, '2026-01-01')
                """,
                (str(source),),
            )
            connection.commit()

            operation = create_relocation_operation(
                connection,
                asset_id="image_a",
                source_path=str(source),
                destination_path=str(root / "archive" / "source.jpg"),
                mode="move",
                expected_size=6,
                expected_hash="abc",
            )
            self.assertEqual(operation["state"], "planned")
            updated = update_relocation_operation(
                connection,
                operation["operation_id"],
                state="recovery_needed",
                error_text="simulated interruption",
            )
            self.assertEqual(updated["state"], "recovery_needed")
            unfinished = list_relocation_operations(connection, unfinished_only=True)
            self.assertEqual([row["operation_id"] for row in unfinished], [operation["operation_id"]])


if __name__ == "__main__":
    unittest.main()
