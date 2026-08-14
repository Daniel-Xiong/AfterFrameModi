from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def list_user_catalog_roots(
    connection: sqlite3.Connection,
    *,
    root_type: str | None = None,
) -> list[sqlite3.Row]:
    clauses = ["is_active = 1", "user_declared = 1"]
    params: list[object] = []
    if root_type:
        clauses.append("root_type = ?")
        params.append(root_type)
    return connection.execute(
        f"""
        SELECT root_id, root_type, path, is_active, user_declared, created_at, updated_at
        FROM catalog_roots
        WHERE {' AND '.join(clauses)}
        ORDER BY root_type, path
        """,
        params,
    ).fetchall()


def assign_asset_root_membership(
    connection: sqlite3.Connection,
    asset_id: str,
    *,
    commit: bool = True,
) -> str | None:
    asset = connection.execute(
        "SELECT canonical_path, asset_type FROM assets WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()
    if asset is None:
        return None
    asset_path = Path(str(asset["canonical_path"])).resolve()
    root_type = "raw" if str(asset["asset_type"]) == "raw" else "image"
    candidates: list[tuple[int, sqlite3.Row, Path]] = []
    for row in list_user_catalog_roots(connection, root_type=root_type):
        root_path = Path(str(row["path"])).resolve()
        if _contains(root_path, asset_path):
            candidates.append((len(root_path.parts), row, root_path))

    connection.execute("DELETE FROM asset_root_memberships WHERE asset_id = ?", (asset_id,))
    selected_id: str | None = None
    if candidates:
        _, selected, root_path = max(candidates, key=lambda item: item[0])
        selected_id = str(selected["root_id"])
        relative = os.fspath(asset_path.relative_to(root_path))
        connection.execute(
            """
            INSERT INTO asset_root_memberships (asset_id, root_id, relative_path)
            VALUES (?, ?, ?)
            """,
            (asset_id, selected_id, relative),
        )
    if commit:
        connection.commit()
    return selected_id


def backfill_asset_root_memberships(
    connection: sqlite3.Connection,
    *,
    root_ids: Iterable[str] | None = None,
    commit: bool = True,
) -> int:
    selected_roots = set(root_ids or [])
    rows = connection.execute(
        "SELECT asset_id FROM assets WHERE status = 'active' ORDER BY asset_id"
    ).fetchall()
    assigned = 0
    for row in rows:
        root_id = assign_asset_root_membership(
            connection,
            str(row["asset_id"]),
            commit=False,
        )
        if root_id and (not selected_roots or root_id in selected_roots):
            assigned += 1
    if commit:
        connection.commit()
    return assigned


def list_assets_in_roots(
    connection: sqlite3.Connection,
    root_ids: Iterable[str],
    *,
    asset_type: str = "image",
    exclude_root_ids: Iterable[str] = (),
) -> list[sqlite3.Row]:
    included = sorted(set(root_ids))
    excluded = sorted(set(exclude_root_ids))
    if not included:
        return []
    include_placeholders = ",".join("?" for _ in included)
    params: list[object] = [*included, asset_type]
    exclude_sql = ""
    if excluded:
        exclude_placeholders = ",".join("?" for _ in excluded)
        exclude_sql = (
            " AND NOT EXISTS ("
            "SELECT 1 FROM asset_root_memberships excluded "
            f"WHERE excluded.asset_id = assets.asset_id AND excluded.root_id IN ({exclude_placeholders})"
            ")"
        )
        params.extend(excluded)
    return connection.execute(
        f"""
        SELECT DISTINCT assets.*
        FROM asset_root_memberships membership
        JOIN assets ON assets.asset_id = membership.asset_id
        WHERE membership.root_id IN ({include_placeholders})
          AND assets.asset_type = ?
          AND assets.status = 'active'
          AND assets.exists_on_disk = 1
          {exclude_sql}
        ORDER BY assets.asset_id
        """,
        params,
    ).fetchall()


def list_gallery_assets_except_roots(
    connection: sqlite3.Connection,
    probe_root_ids: Iterable[str],
    *,
    asset_type: str = "image",
) -> list[sqlite3.Row]:
    excluded = sorted(set(probe_root_ids))
    params: list[object] = [asset_type]
    excluded_sql = ""
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        excluded_sql = (
            " AND NOT EXISTS ("
            "SELECT 1 FROM asset_root_memberships probe "
            f"WHERE probe.asset_id = assets.asset_id AND probe.root_id IN ({placeholders})"
            ")"
        )
        params.extend(excluded)
    return connection.execute(
        f"""
        SELECT assets.*
        FROM assets
        WHERE assets.asset_type = ?
          AND assets.status = 'active'
          AND assets.exists_on_disk = 1
          {excluded_sql}
        ORDER BY assets.asset_id
        """,
        params,
    ).fetchall()
