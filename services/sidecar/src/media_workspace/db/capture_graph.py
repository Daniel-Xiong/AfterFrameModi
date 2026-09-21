"""Capture units (layer 1), version-set guarantee (layer 2), burst groups (layer 3).

See docs/asset-unit-model.md. File rows stay in `assets`; this module is the
only writer for the grouping tables.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from hashlib import sha1
from pathlib import Path

from .resource_sets import attach_asset_to_resource_set, get_resource_set_for_asset

BURST_GAP_SECONDS = 2.0
PAIR_TIME_SECONDS = 5.0
SIMILAR_TIME_SECONDS = 60.0

_DISPLAY_TYPES = {"image", "video"}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_delta_seconds(left: str | None, right: str | None) -> float | None:
    a = _parse_time(left)
    b = _parse_time(right)
    if a is None or b is None:
        return None
    if a.tzinfo is None and b.tzinfo is not None:
        b = b.replace(tzinfo=None)
    elif a.tzinfo is not None and b.tzinfo is None:
        a = a.replace(tzinfo=None)
    return abs((a - b).total_seconds())


def _camera_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    return left.strip().lower() == right.strip().lower()


def _meta(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _unit_id_for_set(resource_set_id: str) -> str:
    return f"cu_{sha1(resource_set_id.encode('utf-8')).hexdigest()[:20]}"


def _burst_id_for_units(unit_ids: list[str]) -> str:
    digest = sha1("|".join(sorted(unit_ids)).encode("utf-8")).hexdigest()[:20]
    return f"burst_{digest}"


def _sidecar_paths(canonical_path: str) -> list[str]:
    path = Path(canonical_path)
    candidates = [path.with_suffix(".xmp"), Path(str(path) + ".xmp")]
    found: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            if candidate.is_file():
                resolved = str(candidate.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
        except OSError:
            continue
    return found


def _ensure_browseable_resource_sets(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT assets.asset_id
        FROM assets
        JOIN image_lookup_registry AS registry
            ON registry.image_asset_id = assets.asset_id
        LEFT JOIN resource_set_items AS rsi
            ON rsi.asset_id = assets.asset_id
        WHERE assets.asset_type IN ('image', 'video', 'raw')
          AND rsi.set_id IS NULL
        ORDER BY assets.created_at, assets.canonical_path
        """
    ).fetchall()
    for row in rows:
        attach_asset_to_resource_set(
            connection, str(row["asset_id"]), version_kind="import", commit=False,
        )


def _load_browseable_assets(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
            assets.asset_id,
            assets.asset_type,
            assets.stem,
            assets.canonical_path,
            assets.metadata_json,
            assets.app_rating,
            assets.fingerprint,
            registry.raw_asset_id AS paired_raw_asset_id,
            rsi.set_id AS resource_set_id,
            rsi.role AS resource_role,
            rsi.parent_asset_id,
            rs.primary_asset_id
        FROM assets
        JOIN image_lookup_registry AS registry
            ON registry.image_asset_id = assets.asset_id
        LEFT JOIN resource_set_items AS rsi
            ON rsi.asset_id = assets.asset_id
        LEFT JOIN resource_sets AS rs
            ON rs.set_id = rsi.set_id
        WHERE assets.asset_type IN ('image', 'video', 'raw')
        ORDER BY assets.created_at, assets.canonical_path
        """
    ).fetchall()
    assets = []
    for row in rows:
        meta = _meta(row)
        assets.append({
            "asset_id": str(row["asset_id"]),
            "asset_type": str(row["asset_type"]),
            "stem": str(row["stem"] or ""),
            "canonical_path": str(row["canonical_path"] or ""),
            "capture_time": meta.get("capture_time"),
            "camera_model": meta.get("camera_model"),
            "app_rating": int(row["app_rating"] or 0),
            "fingerprint": str(row["fingerprint"] or ""),
            "paired_raw_asset_id": str(row["paired_raw_asset_id"]) if row["paired_raw_asset_id"] else None,
            "resource_set_id": str(row["resource_set_id"]) if row["resource_set_id"] else None,
            "resource_role": str(row["resource_role"]) if row["resource_role"] else None,
            "parent_asset_id": str(row["parent_asset_id"]) if row["parent_asset_id"] else None,
            "primary_asset_id": str(row["primary_asset_id"]) if row["primary_asset_id"] else None,
        })
    return assets


def _compatible_pair(image: dict, raw: dict) -> bool:
    if image["stem"] and raw["stem"] and image["stem"] == raw["stem"]:
        if not _camera_compatible(image.get("camera_model"), raw.get("camera_model")):
            return False
        delta = _time_delta_seconds(image.get("capture_time"), raw.get("capture_time"))
        return delta is None or delta <= PAIR_TIME_SECONDS
    return False


def _clear_graph(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM burst_group_items")
    connection.execute("DELETE FROM burst_groups")
    connection.execute("DELETE FROM capture_unit_sidecars")
    connection.execute("DELETE FROM capture_unit_members")
    connection.execute("DELETE FROM capture_units")


def detach_asset_from_capture_graph(connection: sqlite3.Connection, asset_id: str) -> None:
    """Drop graph rows that would block deleting this asset, then rebuild later."""
    connection.execute(
        """
        DELETE FROM burst_group_items
        WHERE unit_id IN (SELECT unit_id FROM capture_unit_members WHERE asset_id = ?)
           OR unit_id IN (SELECT unit_id FROM capture_units WHERE display_asset_id = ?)
        """,
        (asset_id, asset_id),
    )
    connection.execute(
        "DELETE FROM burst_groups WHERE display_asset_id = ? OR group_id NOT IN (SELECT group_id FROM burst_group_items)",
        (asset_id,),
    )
    connection.execute(
        """
        DELETE FROM capture_unit_sidecars
        WHERE unit_id IN (SELECT unit_id FROM capture_units WHERE display_asset_id = ?)
           OR unit_id IN (SELECT unit_id FROM capture_unit_members WHERE asset_id = ?)
        """,
        (asset_id, asset_id),
    )
    connection.execute("DELETE FROM capture_unit_members WHERE asset_id = ?", (asset_id,))
    connection.execute(
        "DELETE FROM capture_units WHERE display_asset_id = ? OR unit_id NOT IN (SELECT unit_id FROM capture_unit_members)",
        (asset_id,),
    )


def _drop_singleton_set(connection: sqlite3.Connection, set_id: str | None) -> None:
    if not set_id:
        return
    remaining = connection.execute(
        "SELECT COUNT(*) AS item_count FROM resource_set_items WHERE set_id = ?",
        (set_id,),
    ).fetchone()
    if remaining is not None and int(remaining["item_count"] or 0) == 0:
        connection.execute("DELETE FROM resource_sets WHERE set_id = ?", (set_id,))


def _detach_raw_from_own_set(connection: sqlite3.Connection, raw_asset_id: str) -> None:
    current = get_resource_set_for_asset(connection, raw_asset_id)
    if not current:
        return
    connection.execute(
        "DELETE FROM resource_set_items WHERE set_id = ? AND asset_id = ?",
        (current["set_id"], raw_asset_id),
    )
    _drop_singleton_set(connection, str(current["set_id"]))


def _insert_unit(
    connection: sqlite3.Connection,
    *,
    resource_set_id: str,
    display_asset_id: str,
    capture_time: str | None,
    camera_model: str | None,
    members: list[tuple[str, str]],
    sidecar_paths: list[str],
) -> str:
    unit_id = _unit_id_for_set(resource_set_id)
    connection.execute(
        """
        INSERT INTO capture_units (
            unit_id, display_asset_id, resource_set_id, capture_time, camera_model
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (unit_id, display_asset_id, resource_set_id, capture_time, camera_model),
    )
    for asset_id, role in members:
        connection.execute(
            """
            INSERT OR IGNORE INTO capture_unit_members (unit_id, asset_id, role)
            VALUES (?, ?, ?)
            """,
            (unit_id, asset_id, role),
        )
    for path in sidecar_paths:
        connection.execute(
            """
            INSERT OR IGNORE INTO capture_unit_sidecars (unit_id, path)
            VALUES (?, ?)
            """,
            (unit_id, path),
        )
    return unit_id


def rebuild_capture_graph(
    connection: sqlite3.Connection,
    *,
    burst_gap_seconds: float = BURST_GAP_SECONDS,
    commit: bool = True,
) -> dict[str, int]:
    """Rebuild capture units and bursts from current assets / version sets."""
    _ensure_browseable_resource_sets(connection)
    assets = _load_browseable_assets(connection)
    by_id = {item["asset_id"]: item for item in assets}

    _clear_graph(connection)

    assigned: set[str] = set()
    units_created = 0

    # Index RAW candidates that can join an image unit.
    raws = [item for item in assets if item["asset_type"] == "raw"]
    raws_by_stem: dict[str, list[dict]] = {}
    for raw in raws:
        raws_by_stem.setdefault(raw["stem"], []).append(raw)

    def pick_raw_for(image: dict) -> dict | None:
        paired_id = image.get("paired_raw_asset_id")
        if paired_id and paired_id in by_id and by_id[paired_id]["asset_type"] == "raw":
            return by_id[paired_id]
        # Source RAW may not be browseable — still attach via registry id.
        if paired_id and paired_id not in by_id:
            row = connection.execute(
                "SELECT asset_id, asset_type, stem, canonical_path, metadata_json FROM assets WHERE asset_id = ?",
                (paired_id,),
            ).fetchone()
            if row and str(row["asset_type"]) == "raw":
                meta = _meta(row)
                return {
                    "asset_id": str(row["asset_id"]),
                    "asset_type": "raw",
                    "stem": str(row["stem"] or ""),
                    "canonical_path": str(row["canonical_path"] or ""),
                    "capture_time": meta.get("capture_time"),
                    "camera_model": meta.get("camera_model"),
                    "app_rating": 0,
                    "fingerprint": "",
                    "paired_raw_asset_id": None,
                    "resource_set_id": None,
                    "resource_role": None,
                    "parent_asset_id": None,
                    "primary_asset_id": None,
                    "source_only": True,
                }
        for raw in raws_by_stem.get(image["stem"], []):
            if raw["asset_id"] in assigned:
                continue
            if _compatible_pair(image, raw):
                return raw
        return None

    # One capture unit per version family (resource set), skipping version-only
    # members — they ride along with the set primary.
    processed_sets: set[str] = set()
    for item in assets:
        set_id = item.get("resource_set_id")
        if not set_id or set_id in processed_sets:
            continue
        if item.get("resource_role") == "version":
            continue
        processed_sets.add(set_id)

        members_rows = connection.execute(
            """
            SELECT rsi.asset_id, rsi.role, assets.asset_type, assets.canonical_path,
                   assets.metadata_json, assets.stem, assets.app_rating
            FROM resource_set_items AS rsi
            JOIN assets ON assets.asset_id = rsi.asset_id
            WHERE rsi.set_id = ?
            ORDER BY rsi.sort_order, assets.canonical_path
            """,
            (set_id,),
        ).fetchall()
        if not members_rows:
            continue

        display_id = item.get("primary_asset_id") or item["asset_id"]
        display_row = by_id.get(display_id)
        # Prefer an image/video over a RAW if the set somehow mixed them.
        ranked = []
        for member in members_rows:
            ranked.append((0 if str(member["asset_type"]) in _DISPLAY_TYPES else 1, str(member["asset_id"])))
        ranked.sort()
        if ranked:
            display_id = ranked[0][1]
            display_row = by_id.get(display_id) or display_row

        if display_row and display_row["asset_type"] == "raw":
            # RAW-only set for now; may still merge below if an image claims it.
            pass

        # Skip RAW-only sets that an image will claim as a sibling.
        if display_row and display_row["asset_type"] == "raw":
            claimed = False
            for image in assets:
                if image["asset_type"] not in _DISPLAY_TYPES:
                    continue
                if image["asset_id"] in assigned:
                    continue
                if image.get("paired_raw_asset_id") == display_id or _compatible_pair(image, display_row):
                    claimed = True
                    break
            if claimed:
                continue

        member_pairs: list[tuple[str, str]] = []
        sidecar: list[str] = []
        capture_time = display_row.get("capture_time") if display_row else None
        camera_model = display_row.get("camera_model") if display_row else None

        for member in members_rows:
            asset_id = str(member["asset_id"])
            role = "display" if asset_id == display_id else (
                "raw" if str(member["asset_type"]) == "raw" else "version"
            )
            member_pairs.append((asset_id, role))
            assigned.add(asset_id)
            sidecar.extend(_sidecar_paths(str(member["canonical_path"])))
            if not capture_time:
                capture_time = _meta(member).get("capture_time")
            if not camera_model:
                camera_model = _meta(member).get("camera_model")

        # Attach paired / sibling RAW.
        if display_row and display_row["asset_type"] in _DISPLAY_TYPES:
            raw = pick_raw_for(display_row)
            if raw and raw["asset_id"] not in assigned:
                if not raw.get("source_only"):
                    _detach_raw_from_own_set(connection, raw["asset_id"])
                member_pairs.append((raw["asset_id"], "raw"))
                assigned.add(raw["asset_id"])
                sidecar.extend(_sidecar_paths(raw["canonical_path"]))
                if not capture_time:
                    capture_time = raw.get("capture_time")
                if not camera_model:
                    camera_model = raw.get("camera_model")

        _insert_unit(
            connection,
            resource_set_id=set_id,
            display_asset_id=display_id,
            capture_time=capture_time,
            camera_model=camera_model,
            members=member_pairs,
            sidecar_paths=sidecar,
        )
        units_created += 1

    # Remaining browseable RAWs that no image claimed.
    for raw in raws:
        if raw["asset_id"] in assigned:
            continue
        set_id = raw.get("resource_set_id")
        if not set_id:
            attach_asset_to_resource_set(
                connection, raw["asset_id"], version_kind="import", commit=False,
            )
            current = get_resource_set_for_asset(connection, raw["asset_id"])
            set_id = str(current["set_id"]) if current else None
        if not set_id:
            continue
        _insert_unit(
            connection,
            resource_set_id=set_id,
            display_asset_id=raw["asset_id"],
            capture_time=raw.get("capture_time"),
            camera_model=raw.get("camera_model"),
            members=[(raw["asset_id"], "display")],
            sidecar_paths=_sidecar_paths(raw["canonical_path"]),
        )
        assigned.add(raw["asset_id"])
        units_created += 1

    bursts_created = _rebuild_bursts(connection, burst_gap_seconds=burst_gap_seconds)

    if commit:
        connection.commit()
    return {"capture_units": units_created, "burst_groups": bursts_created}


def _rebuild_bursts(connection: sqlite3.Connection, *, burst_gap_seconds: float) -> int:
    rows = connection.execute(
        """
        SELECT
            cu.unit_id,
            cu.display_asset_id,
            cu.resource_set_id,
            cu.capture_time,
            cu.camera_model,
            assets.asset_type,
            assets.app_rating
        FROM capture_units AS cu
        JOIN assets ON assets.asset_id = cu.display_asset_id
        WHERE cu.capture_time IS NOT NULL AND cu.capture_time != ''
        ORDER BY COALESCE(cu.camera_model, ''), cu.capture_time, cu.unit_id
        """
    ).fetchall()

    def camera_key(row: sqlite3.Row) -> str:
        return str(row["camera_model"] or "").strip().lower()

    clusters: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    for row in rows:
        if str(row["asset_type"]) == "video":
            if len(current) >= 2:
                clusters.append(current)
            current = []
            continue
        if not current:
            current = [row]
            continue
        prev = current[-1]
        same_camera = camera_key(row) == camera_key(prev)
        delta = _time_delta_seconds(prev["capture_time"], row["capture_time"])
        if same_camera and delta is not None and delta <= burst_gap_seconds:
            current.append(row)
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [row]
    if len(current) >= 2:
        clusters.append(current)

    created = 0
    for cluster in clusters:
        unit_ids = [str(row["unit_id"]) for row in cluster]
        group_id = _burst_id_for_units(unit_ids)
        # Highest rating wins; if ratings tie, earliest capture_time.
        top_rating = max(int(row["app_rating"] or 0) for row in cluster)
        rated = [row for row in cluster if int(row["app_rating"] or 0) == top_rating]
        keeper = min(rated, key=lambda row: str(row["capture_time"] or ""))
        connection.execute(
            """
            INSERT INTO burst_groups (group_id, display_asset_id, member_count)
            VALUES (?, ?, ?)
            """,
            (group_id, str(keeper["display_asset_id"]), len(cluster)),
        )
        for index, row in enumerate(cluster):
            connection.execute(
                """
                INSERT INTO burst_group_items (
                    group_id, resource_set_id, unit_id, sort_order, is_keeper
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    str(row["resource_set_id"]),
                    str(row["unit_id"]),
                    index,
                    1 if str(row["unit_id"]) == str(keeper["unit_id"]) else 0,
                ),
            )
        created += 1
    return created


def list_capture_unit_members(connection: sqlite3.Connection, unit_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            cum.asset_id,
            cum.role,
            assets.asset_type,
            assets.stem,
            assets.canonical_path,
            pe.relative_path AS preview_relative_path
        FROM capture_unit_members AS cum
        JOIN assets ON assets.asset_id = cum.asset_id
        LEFT JOIN preview_entries AS pe
            ON pe.asset_id = cum.asset_id AND pe.kind = 'preview' AND pe.status = 'ready'
        WHERE cum.unit_id = ?
        ORDER BY CASE cum.role WHEN 'display' THEN 0 WHEN 'raw' THEN 1 ELSE 2 END, assets.stem
        """,
        (unit_id,),
    ).fetchall()


def list_capture_unit_sidecars(connection: sqlite3.Connection, unit_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT path FROM capture_unit_sidecars WHERE unit_id = ? ORDER BY path",
        (unit_id,),
    ).fetchall()
    return [str(row["path"]) for row in rows]


def get_capture_unit_for_asset(connection: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT cu.unit_id, cu.display_asset_id, cu.resource_set_id,
               cu.capture_time, cu.camera_model, cum.role AS member_role
        FROM capture_unit_members AS cum
        JOIN capture_units AS cu ON cu.unit_id = cum.unit_id
        WHERE cum.asset_id = ?
        """,
        (asset_id,),
    ).fetchone()


def list_burst_siblings(connection: sqlite3.Connection, group_id: str, exclude_asset_id: str | None = None) -> list[sqlite3.Row]:
    params: list[object] = [group_id]
    extra = ""
    if exclude_asset_id:
        extra = "AND cu.display_asset_id != ?"
        params.append(exclude_asset_id)
    return connection.execute(
        f"""
        SELECT
            bgi.unit_id,
            bgi.resource_set_id,
            bgi.is_keeper,
            bgi.sort_order,
            cu.display_asset_id AS asset_id,
            assets.stem,
            assets.canonical_path,
            pe.relative_path AS preview_relative_path
        FROM burst_group_items AS bgi
        JOIN capture_units AS cu ON cu.unit_id = bgi.unit_id
        JOIN assets ON assets.asset_id = cu.display_asset_id
        LEFT JOIN preview_entries AS pe
            ON pe.asset_id = cu.display_asset_id AND pe.kind = 'preview' AND pe.status = 'ready'
        WHERE bgi.group_id = ? {extra}
        ORDER BY bgi.sort_order
        """,
        params,
    ).fetchall()


def get_burst_for_asset(connection: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT bg.group_id, bg.display_asset_id, bg.member_count, bgi.is_keeper, bgi.unit_id
        FROM capture_unit_members AS cum
        JOIN burst_group_items AS bgi ON bgi.unit_id = cum.unit_id
        JOIN burst_groups AS bg ON bg.group_id = bgi.group_id
        WHERE cum.asset_id = ?
        """,
        (asset_id,),
    ).fetchone()


def set_burst_keeper(connection: sqlite3.Connection, group_id: str, asset_id: str, commit: bool = True) -> dict:
    item = connection.execute(
        """
        SELECT bgi.group_id, bgi.unit_id, cu.display_asset_id
        FROM burst_group_items AS bgi
        JOIN capture_units AS cu ON cu.unit_id = bgi.unit_id
        WHERE bgi.group_id = ? AND cu.display_asset_id = ?
        """,
        (group_id, asset_id),
    ).fetchone()
    if item is None:
        raise ValueError(f"asset {asset_id} is not in burst {group_id}")
    connection.execute(
        "UPDATE burst_group_items SET is_keeper = CASE WHEN unit_id = ? THEN 1 ELSE 0 END WHERE group_id = ?",
        (item["unit_id"], group_id),
    )
    connection.execute(
        "UPDATE burst_groups SET display_asset_id = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
        (asset_id, group_id),
    )
    if commit:
        connection.commit()
    return {"group_id": group_id, "display_asset_id": asset_id}


def list_similar_clusters(
    connection: sqlite3.Connection,
    *,
    time_window_seconds: float = SIMILAR_TIME_SECONDS,
) -> list[dict]:
    """Ephemeral clusters of layer-2 representatives. Not stored as parents.

    Fingerprint duplicates always cluster. Same-stem near-duplicates cluster
    only when they are not already together in a burst.
    """
    rows = connection.execute(
        """
        SELECT
            cu.unit_id,
            cu.display_asset_id AS asset_id,
            cu.capture_time,
            cu.camera_model,
            assets.stem,
            assets.fingerprint,
            bgi.group_id AS burst_group_id
        FROM capture_units AS cu
        JOIN assets ON assets.asset_id = cu.display_asset_id
        LEFT JOIN burst_group_items AS bgi ON bgi.unit_id = cu.unit_id
        ORDER BY assets.fingerprint, assets.stem, cu.capture_time
        """
    ).fetchall()

    clusters: list[dict] = []

    by_fp: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        fingerprint = str(row["fingerprint"] or "")
        if fingerprint:
            by_fp.setdefault(fingerprint, []).append(row)
    for fingerprint, members in by_fp.items():
        if len(members) < 2:
            continue
        clusters.append({
            "reason": "fingerprint",
            "asset_ids": [str(row["asset_id"]) for row in members],
            "stems": [str(row["stem"]) for row in members],
        })

    by_stem: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        stem = str(row["stem"] or "")
        if stem:
            by_stem.setdefault(stem, []).append(row)
    seen_pairs: set[tuple[str, ...]] = {
        tuple(sorted(cluster["asset_ids"])) for cluster in clusters
    }
    for stem, members in by_stem.items():
        if len(members) < 2:
            continue
        # Greedy time clusters, skipping members already in the same burst.
        pending = list(members)
        while len(pending) >= 2:
            seed = pending.pop(0)
            group = [seed]
            rest = []
            for other in pending:
                same_burst = (
                    seed["burst_group_id"]
                    and other["burst_group_id"]
                    and seed["burst_group_id"] == other["burst_group_id"]
                )
                if same_burst:
                    rest.append(other)
                    continue
                delta = _time_delta_seconds(seed["capture_time"], other["capture_time"])
                if delta is not None and delta <= time_window_seconds and _camera_compatible(
                    seed["camera_model"], other["camera_model"]
                ):
                    group.append(other)
                else:
                    rest.append(other)
            pending = rest
            if len(group) < 2:
                continue
            key = tuple(sorted(str(row["asset_id"]) for row in group))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            clusters.append({
                "reason": "near_duplicate",
                "asset_ids": list(key),
                "stems": [str(row["stem"]) for row in group],
            })

    for index, cluster in enumerate(clusters):
        cluster["cluster_id"] = f"sim_{index:04d}"
    return clusters


def similar_cluster_for_asset(connection: sqlite3.Connection, asset_id: str) -> dict | None:
    for cluster in list_similar_clusters(connection):
        if asset_id in cluster["asset_ids"]:
            return cluster
    return None
