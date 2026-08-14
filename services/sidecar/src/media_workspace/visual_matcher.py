from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image

from .catalog import CatalogPaths
from .config import VisualThresholds
from .visual_similarity import aligned_error, band_neighbors, bounded_crop_match, hamming_distance, parse_phash


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _candidate_select() -> str:
    return """
        SELECT assets.asset_id, assets.asset_type, assets.canonical_path,
               assets.stem, assets.stem_key, assets.fingerprint, assets.file_size,
               assets.metadata_json, assets.meta_capture_time, assets.meta_camera_model,
               assets.meta_width, assets.meta_height,
               signatures.phash, signatures.phash_band0, signatures.phash_band1,
               signatures.phash_band2, signatures.phash_band3,
               preview.relative_path AS preview_relative_path
        FROM assets
        JOIN visual_signatures signatures ON signatures.asset_id = assets.asset_id
        LEFT JOIN preview_entries preview
          ON preview.asset_id = assets.asset_id AND preview.kind = 'preview'
    """


def _scope_sql(scope: str, probe_root_id: str) -> tuple[str, list[object]]:
    if scope == "same_root":
        return (
            """
            EXISTS (
                SELECT 1 FROM asset_root_memberships scoped
                WHERE scoped.asset_id = assets.asset_id AND scoped.root_id = ?
            )
            """,
            [probe_root_id],
        )
    if scope == "catalog_except_probe":
        return (
            """
            NOT EXISTS (
                SELECT 1 FROM asset_root_memberships scoped
                WHERE scoped.asset_id = assets.asset_id AND scoped.root_id = ?
            )
            """,
            [probe_root_id],
        )
    raise ValueError(f"unsupported visual gallery scope: {scope}")


def _add_candidates(
    candidates: dict[str, dict[str, object]],
    rows: Iterable,
    channel: str,
    *,
    region: bool = False,
) -> None:
    for row in rows:
        asset_id = str(row["asset_id"])
        entry = candidates.setdefault(
            asset_id,
            {
                **dict(row),
                "channels": set(),
                "region_matches": [],
            },
        )
        entry["channels"].add(channel)
        if region:
            entry["region_matches"].append(
                {
                    "region_kind": row["region_kind"],
                    "normalized_rect": json.loads(row["normalized_rect_json"]),
                    "phash": row["region_phash"],
                }
            )


def recall_visual_candidates(
    connection,
    probe_asset_id: str,
    *,
    probe_root_id: str,
    scope: str = "catalog_except_probe",
    thresholds: VisualThresholds | None = None,
    asset_type: str = "image",
) -> list[dict[str, object]]:
    thresholds = thresholds or VisualThresholds()
    probe = connection.execute(
        _candidate_select() + " WHERE assets.asset_id = ?",
        (probe_asset_id,),
    ).fetchone()
    if probe is None:
        return []
    scope_clause, scope_params = _scope_sql(scope, probe_root_id)
    common = (
        " assets.asset_id != ? AND assets.asset_type = ? "
        "AND assets.status = 'active' AND assets.exists_on_disk = 1 "
        f"AND {scope_clause} "
    )
    common_params: list[object] = [probe_asset_id, asset_type, *scope_params]
    candidates: dict[str, dict[str, object]] = {}
    metadata_limit = thresholds.metadata_channel_limit

    stem_key = str(probe["stem_key"] or "")
    if stem_key:
        rows = connection.execute(
            _candidate_select()
            + f" WHERE {common} AND assets.stem_key = ? ORDER BY assets.asset_id LIMIT ?",
            [*common_params, stem_key, metadata_limit],
        ).fetchall()
        _add_candidates(candidates, rows, "stem")

    capture_time = probe["meta_capture_time"]
    camera = probe["meta_camera_model"]
    if capture_time and camera:
        rows = connection.execute(
            _candidate_select()
            + f"""
              WHERE {common}
                AND assets.meta_camera_model = ?
                AND assets.meta_capture_time IS NOT NULL
                AND ABS((julianday(assets.meta_capture_time) - julianday(?)) * 86400.0) <= 2.0
              ORDER BY ABS(julianday(assets.meta_capture_time) - julianday(?)), assets.asset_id
              LIMIT ?
            """,
            [*common_params, camera, capture_time, capture_time, metadata_limit],
        ).fetchall()
        _add_candidates(candidates, rows, "capture_camera")

    rows = connection.execute(
        _candidate_select()
        + f" WHERE {common} AND signatures.phash = ? ORDER BY assets.asset_id LIMIT ?",
        [*common_params, probe["phash"], thresholds.hash_channel_limit],
    ).fetchall()
    _add_candidates(candidates, rows, "exact_phash")

    probe_bands = [int(probe[f"phash_band{index}"]) for index in range(4)]
    for band_index, band in enumerate(probe_bands):
        neighbors = band_neighbors(band, radius=2)
        placeholders = ",".join("?" for _ in neighbors)
        rows = connection.execute(
            _candidate_select()
            + f"""
              WHERE {common}
                AND signatures.phash_band{band_index} IN ({placeholders})
              ORDER BY assets.asset_id
              LIMIT ?
            """,
            [*common_params, *neighbors, thresholds.hash_channel_limit],
        ).fetchall()
        _add_candidates(candidates, rows, f"phash_band{band_index}")

    # Crop recall compares the probe's whole-frame hash to a fixed, bounded set
    # of indexed parent regions.
    for band_index, band in enumerate(probe_bands):
        neighbors = band_neighbors(band, radius=2)
        placeholders = ",".join("?" for _ in neighbors)
        rows = connection.execute(
            _candidate_select().replace(
                "FROM assets",
                """
                , regions.region_kind, regions.normalized_rect_json,
                  regions.phash AS region_phash
                FROM assets
                JOIN visual_region_signatures regions ON regions.asset_id = assets.asset_id
                """,
                1,
            )
            + f"""
              WHERE {common}
                AND regions.phash_band{band_index} IN ({placeholders})
              ORDER BY assets.asset_id, regions.region_kind
              LIMIT ?
            """,
            [*common_params, *neighbors, thresholds.hash_channel_limit],
        ).fetchall()
        _add_candidates(candidates, rows, f"crop_band{band_index}", region=True)

    priority = {
        "exact_phash": 50,
        "stem": 40,
        "capture_camera": 35,
    }
    probe_hash = parse_phash(probe["phash"])
    ranked: list[dict[str, object]] = []
    for entry in candidates.values():
        channels = set(entry.pop("channels"))
        distance = hamming_distance(probe_hash, entry["phash"])
        channel_score = max([priority.get(channel, 20) for channel in channels] or [0])
        entry["channels"] = sorted(channels)
        entry["hamming"] = distance
        entry["_recall_rank"] = channel_score * 100 - distance
        ranked.append(entry)
    ranked.sort(key=lambda item: (-int(item["_recall_rank"]), str(item["asset_id"])))
    for item in ranked:
        item.pop("_recall_rank", None)
    return ranked[: thresholds.recall_limit]


def _preview_path(catalog: CatalogPaths, row: dict[str, object]) -> Path | None:
    relative = row.get("preview_relative_path")
    if not relative:
        return None
    path = (catalog.root / str(relative)).resolve()
    return path if path.exists() else None


def _aspect(row: dict[str, object]) -> float | None:
    width = int(row.get("meta_width") or 0)
    height = int(row.get("meta_height") or 0)
    return width / height if width and height else None


def classify_visual_candidate(
    probe: dict[str, object],
    candidate: dict[str, object],
    catalog: CatalogPaths,
    *,
    thresholds: VisualThresholds | None = None,
) -> dict[str, object] | None:
    thresholds = thresholds or VisualThresholds()
    if (
        probe.get("fingerprint")
        and probe.get("fingerprint") == candidate.get("fingerprint")
        and int(probe.get("file_size") or 0) == int(candidate.get("file_size") or 0)
    ):
        return {"kind": "exact", "relation": "duplicate", "score": 1.0, "evidence": {"byte_match": True}}

    probe_meta = json.loads(str(probe.get("metadata_json") or "{}"))
    candidate_meta = json.loads(str(candidate.get("metadata_json") or "{}"))
    probe_camera = probe_meta.get("camera_model")
    candidate_camera = candidate_meta.get("camera_model")
    if probe_camera and candidate_camera and str(probe_camera).lower() != str(candidate_camera).lower():
        return None

    probe_time = _parse_time(probe_meta.get("capture_time"))
    candidate_time = _parse_time(candidate_meta.get("capture_time"))
    delta_seconds = (
        abs((probe_time - candidate_time).total_seconds())
        if probe_time is not None and candidate_time is not None
        else None
    )
    distance = int(candidate["hamming"])
    probe_preview = _preview_path(catalog, probe)
    candidate_preview = _preview_path(catalog, candidate)
    alignment = (
        aligned_error(probe_preview, candidate_preview)
        if probe_preview is not None and candidate_preview is not None
        else None
    )
    evidence = {
        "hamming": distance,
        "aligned_error": alignment,
        "capture_delta_seconds": delta_seconds,
        "channels": candidate.get("channels", []),
    }

    if (
        delta_seconds is not None
        and 0 < delta_seconds <= thresholds.burst_window_seconds
        and probe_camera
        and candidate_camera
    ):
        return {
            "kind": "burst",
            "relation": "burst_sibling",
            "score": max(0.0, 1.0 - distance / 64.0),
            "evidence": evidence,
        }

    probe_aspect = _aspect(probe)
    candidate_aspect = _aspect(candidate)
    aspect_delta = (
        abs(probe_aspect - candidate_aspect) / max(probe_aspect, candidate_aspect)
        if probe_aspect and candidate_aspect
        else None
    )
    evidence["aspect_delta"] = aspect_delta
    candidate_pixels = int(candidate.get("meta_width") or 0) * int(candidate.get("meta_height") or 0)
    probe_pixels = int(probe.get("meta_width") or 0) * int(probe.get("meta_height") or 0)
    should_try_crop = (
        bool(candidate.get("region_matches"))
        and candidate_pixels >= probe_pixels
        and probe_preview is not None
        and candidate_preview is not None
    )
    if should_try_crop:
        crop = bounded_crop_match(probe_preview, candidate_preview)
        evidence["crop_operations"] = crop["operations"]
        evidence["crop_error"] = crop["error"]
        if crop["normalized_rect"] is not None and float(crop["error"]) <= thresholds.crop_error_max:
            evidence["crop_rect"] = crop["normalized_rect"]
            evidence["crop_angle"] = crop["angle"]
            return {
                "kind": "crop_family",
                "relation": "crop_of",
                "score": max(0.0, 1.0 - float(crop["error"])),
                "evidence": evidence,
            }
    if aspect_delta is not None and aspect_delta > thresholds.aspect_tolerance:
        region_matches = candidate.get("region_matches") or []
        if region_matches:
            best = min(
                region_matches,
                key=lambda region: hamming_distance(probe["phash"], region["phash"]),
            )
            region_distance = hamming_distance(probe["phash"], best["phash"])
            if region_distance <= thresholds.hamming_same_frame:
                evidence["crop_rect"] = best["normalized_rect"]
                evidence["crop_region_kind"] = best["region_kind"]
                evidence["crop_hamming"] = region_distance
                return {
                    "kind": "crop_family",
                    "relation": "crop_of",
                    "score": max(0.0, 1.0 - region_distance / 64.0),
                    "evidence": evidence,
                }
        return None

    if distance <= thresholds.hamming_same_frame and (
        alignment is None or alignment <= thresholds.aligned_error_same_frame
    ):
        return {
            "kind": "compressed_family",
            "relation": "compressed_of",
            "score": max(0.0, 1.0 - distance / 64.0),
            "evidence": evidence,
        }
    if distance <= thresholds.hamming_near and (
        alignment is None or alignment <= thresholds.aligned_error_near
    ):
        return {
            "kind": "near_duplicate",
            "relation": "visually_similar",
            "score": max(0.0, 1.0 - distance / 64.0),
            "evidence": evidence,
        }
    return None


def asset_with_signature(connection, asset_id: str) -> dict[str, object] | None:
    row = connection.execute(
        _candidate_select() + " WHERE assets.asset_id = ?",
        (asset_id,),
    ).fetchone()
    return dict(row) if row else None
