from __future__ import annotations

import sqlite3
from uuid import uuid4


RELOCATION_STATES = {
    "planned",
    "copied",
    "renamed",
    "verified",
    "relinked",
    "source_trashed",
    "complete",
    "failed",
    "recovery_needed",
}


def create_relocation_operation(
    connection: sqlite3.Connection,
    *,
    asset_id: str,
    source_path: str,
    destination_path: str,
    mode: str,
    expected_size: int | None = None,
    expected_hash: str | None = None,
    commit: bool = True,
) -> dict[str, object]:
    if mode not in {"move", "archive"}:
        raise ValueError(f"unsupported relocation mode: {mode}")
    operation_id = f"relocate_{uuid4().hex[:20]}"
    connection.execute(
        """
        INSERT INTO relocation_operations (
            operation_id, asset_id, source_path, destination_path, state,
            mode, expected_size, expected_hash
        ) VALUES (?, ?, ?, ?, 'planned', ?, ?, ?)
        """,
        (
            operation_id,
            asset_id,
            source_path,
            destination_path,
            mode,
            expected_size,
            expected_hash,
        ),
    )
    if commit:
        connection.commit()
    return get_relocation_operation(connection, operation_id)


def update_relocation_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    state: str,
    expected_hash: str | None = None,
    error_text: str | None = None,
    commit: bool = True,
) -> dict[str, object]:
    if state not in RELOCATION_STATES:
        raise ValueError(f"unsupported relocation state: {state}")
    changed = connection.execute(
        """
        UPDATE relocation_operations
        SET state = ?,
            expected_hash = COALESCE(?, expected_hash),
            error_text = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE operation_id = ?
        """,
        (state, expected_hash, error_text, operation_id),
    ).rowcount
    if not changed:
        raise ValueError(f"unknown relocation operation: {operation_id}")
    if commit:
        connection.commit()
    return get_relocation_operation(connection, operation_id)


def get_relocation_operation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT * FROM relocation_operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    return dict(row) if row else None


def list_relocation_operations(
    connection: sqlite3.Connection,
    *,
    unfinished_only: bool = False,
    limit: int = 200,
) -> list[dict[str, object]]:
    where = "WHERE state NOT IN ('complete', 'failed')" if unfinished_only else ""
    rows = connection.execute(
        f"""
        SELECT * FROM relocation_operations
        {where}
        ORDER BY updated_at DESC, operation_id
        LIMIT ?
        """,
        (max(1, min(int(limit), 1000)),),
    ).fetchall()
    return [dict(row) for row in rows]
