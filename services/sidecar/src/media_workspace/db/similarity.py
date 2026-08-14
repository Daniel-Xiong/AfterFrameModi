from __future__ import annotations

import json
import sqlite3
from hashlib import sha1
from pathlib import Path
from typing import Iterable

from ..visual_similarity import VISUAL_ALGORITHM_VERSION
from .assets import confirm_match
from .core import _json
from .resource_sets import link_assets, merge_resource_sets


def similarity_group_id(kind: str, asset_ids: Iterable[str], algorithm_version: str) -> str:
    members = ":".join(sorted(set(asset_ids)))
    digest = sha1(f"{kind}:{algorithm_version}:{members}".encode("utf-8")).hexdigest()[:20]
    return f"similar_{digest}"


def get_visual_signature(connection: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM visual_signatures WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()


def upsert_visual_signature(
    connection: sqlite3.Connection,
    *,
    asset_id: str,
    phash: str,
    bands: tuple[int, int, int, int],
    width: int,
    height: int,
    file_size: int,
    preview_fingerprint: str,
    algorithm_version: str = VISUAL_ALGORITHM_VERSION,
    regions: list[dict[str, object]] | None = None,
    commit: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO visual_signatures (
            asset_id, phash, phash_band0, phash_band1, phash_band2, phash_band3,
            width, height, file_size, preview_fingerprint, algorithm_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET
            phash = excluded.phash,
            phash_band0 = excluded.phash_band0,
            phash_band1 = excluded.phash_band1,
            phash_band2 = excluded.phash_band2,
            phash_band3 = excluded.phash_band3,
            width = excluded.width,
            height = excluded.height,
            file_size = excluded.file_size,
            preview_fingerprint = excluded.preview_fingerprint,
            algorithm_version = excluded.algorithm_version,
            indexed_at = CURRENT_TIMESTAMP
        """,
        (
            asset_id,
            phash,
            *bands,
            width,
            height,
            file_size,
            preview_fingerprint,
            algorithm_version,
        ),
    )
    if regions is not None:
        connection.execute(
            "DELETE FROM visual_region_signatures WHERE asset_id = ?",
            (asset_id,),
        )
        for region in regions:
            region_bands = tuple(int(value) for value in region["bands"])
            connection.execute(
                """
                INSERT INTO visual_region_signatures (
                    asset_id, region_kind, normalized_rect_json, phash,
                    phash_band0, phash_band1, phash_band2, phash_band3,
                    algorithm_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    str(region["region_kind"]),
                    _json(region["normalized_rect"]),
                    str(region["phash"]),
                    *region_bands,
                    algorithm_version,
                ),
            )
    if commit:
        connection.commit()


def replace_similarity_group(
    connection: sqlite3.Connection,
    *,
    kind: str,
    representative_asset_id: str,
    members: list[dict[str, object]],
    probe_root_id: str | None,
    algorithm_version: str = VISUAL_ALGORITHM_VERSION,
    commit: bool = True,
) -> str:
    asset_ids = [str(member["asset_id"]) for member in members]
    group_id = similarity_group_id(kind, asset_ids, algorithm_version)
    total_bytes = sum(int(member.get("file_size") or 0) for member in members)
    connection.execute(
        """
        INSERT INTO similarity_groups (
            group_id, kind, representative_asset_id, total_bytes, status,
            probe_root_id, algorithm_version
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            kind = excluded.kind,
            representative_asset_id = excluded.representative_asset_id,
            total_bytes = excluded.total_bytes,
            probe_root_id = excluded.probe_root_id,
            algorithm_version = excluded.algorithm_version
        """,
        (
            group_id,
            kind,
            representative_asset_id,
            total_bytes,
            probe_root_id,
            algorithm_version,
        ),
    )
    connection.execute("DELETE FROM similarity_group_members WHERE group_id = ?", (group_id,))
    for member in members:
        connection.execute(
            """
            INSERT INTO similarity_group_members (
                group_id, asset_id, relation, parent_asset_id, score, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                str(member["asset_id"]),
                str(member["relation"]),
                member.get("parent_asset_id"),
                float(member.get("score") or 0),
                _json(member.get("evidence") or {}),
            ),
        )
    if commit:
        connection.commit()
    return group_id


def set_similarity_group_status(
    connection: sqlite3.Connection,
    group_id: str,
    status: str,
    *,
    commit: bool = True,
) -> bool:
    if status not in {"pending", "reviewed", "dismissed", "partial"}:
        raise ValueError(f"unsupported similarity status: {status}")
    changed = connection.execute(
        """
        UPDATE similarity_groups
        SET status = ?, reviewed_at = CASE
            WHEN ? IN ('reviewed', 'dismissed') THEN CURRENT_TIMESTAMP
            ELSE reviewed_at
        END
        WHERE group_id = ?
        """,
        (status, status, group_id),
    ).rowcount
    if commit:
        connection.commit()
    return bool(changed)


def get_similarity_group(connection: sqlite3.Connection, group_id: str) -> dict[str, object] | None:
    group = connection.execute(
        "SELECT * FROM similarity_groups WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    if group is None:
        return None
    members = connection.execute(
        """
        SELECT members.asset_id, members.relation, members.parent_asset_id,
               members.score, members.evidence_json,
               assets.canonical_path, assets.stem, assets.file_size,
               assets.metadata_json, assets.exists_on_disk,
               preview.relative_path AS preview_relative_path
        FROM similarity_group_members members
        JOIN assets ON assets.asset_id = members.asset_id
        LEFT JOIN preview_entries preview
          ON preview.asset_id = assets.asset_id AND preview.kind = 'preview'
        WHERE members.group_id = ?
        ORDER BY members.score DESC, members.asset_id
        """,
        (group_id,),
    ).fetchall()
    return {
        **dict(group),
        "members": [
            {
                **dict(member),
                "evidence": json.loads(member["evidence_json"] or "{}"),
                "metadata": json.loads(member["metadata_json"] or "{}"),
            }
            for member in members
        ],
    }


def list_similarity_groups(
    connection: sqlite3.Connection,
    *,
    status: str = "pending",
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    clauses = ["status = ?"]
    params: list[object] = [status]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    params.append(max(1, min(int(limit), 1000)))
    rows = connection.execute(
        f"""
        SELECT group_id
        FROM similarity_groups
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC, group_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        group
        for row in rows
        if (group := get_similarity_group(connection, str(row["group_id"]))) is not None
    ]


def prune_empty_similarity_groups(
    connection: sqlite3.Connection,
    *,
    commit: bool = True,
) -> int:
    changed = connection.execute(
        """
        DELETE FROM similarity_groups
        WHERE NOT EXISTS (
            SELECT 1 FROM similarity_group_members members
            WHERE members.group_id = similarity_groups.group_id
        )
        """
    ).rowcount
    if commit:
        connection.commit()
    return int(changed)


def confirm_similarity_group(
    connection: sqlite3.Connection,
    group_id: str,
    *,
    keeper_asset_id: str | None = None,
    commit: bool = True,
) -> dict[str, object]:
    group = get_similarity_group(connection, group_id)
    if group is None:
        raise ValueError(f"unknown similarity group: {group_id}")
    kind = str(group["kind"])
    if kind not in {"exact", "compressed_family", "crop_family"}:
        raise ValueError(f"{kind} groups cannot be attached as one image family")
    member_ids = [str(member["asset_id"]) for member in group["members"]]
    keeper_id = keeper_asset_id or str(group["representative_asset_id"])
    if keeper_id not in member_ids:
        raise ValueError("keeper must be a member of the similarity group")
    relation_type = {
        "exact": "duplicate_of",
        "compressed_family": "compressed_of",
        "crop_family": "crop_of",
    }[kind]
    try:
        set_id = merge_resource_sets(
            connection,
            keeper_asset_id=keeper_id,
            member_asset_ids=[asset_id for asset_id in member_ids if asset_id != keeper_id],
            version_kind={
                "exact": "duplicate",
                "compressed_family": "compressed",
                "crop_family": "crop",
            }[kind],
            commit=False,
        )
        for member in group["members"]:
            asset_id = str(member["asset_id"])
            if asset_id == keeper_id:
                continue
            link_assets(
                connection,
                parent_asset_id=keeper_id,
                child_asset_id=asset_id,
                relation_type=relation_type,
                confidence=float(member["score"]),
                confirmed_by="user",
                recipe_json=member["evidence"],
            )
        set_similarity_group_status(connection, group_id, "reviewed", commit=False)
        connection.execute(
            "UPDATE similarity_groups SET representative_asset_id = ? WHERE group_id = ?",
            (keeper_id, group_id),
        )
        if commit:
            connection.commit()
        return {
            "group_id": group_id,
            "status": "reviewed",
            "keeper_asset_id": keeper_id,
            "resource_set_id": set_id,
            "relation_type": relation_type,
        }
    except Exception:
        if commit:
            connection.rollback()
        raise


def confirm_raw_similarity_proposal(
    connection: sqlite3.Connection,
    group_id: str,
    *,
    raw_asset_id: str | None = None,
    commit: bool = True,
) -> dict[str, object]:
    group = get_similarity_group(connection, group_id)
    if group is None:
        raise ValueError(f"unknown similarity group: {group_id}")
    if str(group["kind"]) != "raw_proposal":
        raise ValueError("only raw_proposal groups can confirm a RAW source")
    image = next(
        (member for member in group["members"] if member["relation"] == "source"),
        None,
    )
    raw_candidates = [
        member for member in group["members"] if member["relation"] == "raw_candidate"
    ]
    if image is None or not raw_candidates:
        raise ValueError("RAW proposal is missing its image or RAW candidate")
    selected_raw = raw_asset_id or str(max(raw_candidates, key=lambda item: item["score"])["asset_id"])
    if selected_raw not in {str(member["asset_id"]) for member in raw_candidates}:
        raise ValueError("selected RAW is not a member of the proposal")
    try:
        confirm_match(
            connection,
            Path(str(image["canonical_path"])),
            selected_raw,
            commit=False,
        )
        set_similarity_group_status(connection, group_id, "reviewed", commit=False)
        if commit:
            connection.commit()
        return {
            "group_id": group_id,
            "status": "reviewed",
            "image_asset_id": str(image["asset_id"]),
            "raw_asset_id": selected_raw,
        }
    except Exception:
        if commit:
            connection.rollback()
        raise
