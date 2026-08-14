from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable

from .catalog import CatalogPaths
from .config import VisualThresholds
from .db import (
    list_assets_in_roots,
    list_user_catalog_roots,
    replace_similarity_group,
)
from .metadata import extract_image_candidate
from .reverse_lookup import shortlist_candidates
from .visual_index import index_visual_signatures
from .visual_matcher import asset_with_signature, classify_visual_candidate, recall_visual_candidates
from .visual_similarity import VISUAL_ALGORITHM_VERSION, hamming_distance


def _keeper_key(row: dict[str, object]) -> tuple[int, int]:
    width = int(row.get("meta_width") or 0)
    height = int(row.get("meta_height") or 0)
    return (width * height, int(row.get("file_size") or 0))


def _pair_members(
    probe: dict[str, object],
    candidate: dict[str, object],
    classification: dict[str, object],
) -> tuple[str, list[dict[str, object]]]:
    kind = str(classification["kind"])
    relation = str(classification["relation"])
    if kind == "crop_family":
        keeper, child = candidate, probe
    else:
        keeper, child = max((probe, candidate), key=_keeper_key), min((probe, candidate), key=_keeper_key)
    keeper_id = str(keeper["asset_id"])
    return keeper_id, [
        {
            "asset_id": keeper_id,
            "relation": "source",
            "score": 1.0,
            "file_size": int(keeper.get("file_size") or 0),
        },
        {
            "asset_id": str(child["asset_id"]),
            "relation": relation,
            "parent_asset_id": keeper_id,
            "score": float(classification["score"]),
            "file_size": int(child.get("file_size") or 0),
            "evidence": classification["evidence"],
        },
    ]


def _raw_proposals(
    connection,
    probe: dict[str, object],
    *,
    limit: int = 5,
    max_hamming: int = 14,
) -> list[tuple[dict[str, object], dict[str, object]]]:
    try:
        image_candidate = extract_image_candidate(Path(str(probe["canonical_path"])))
    except Exception:
        return []
    proposals: list[tuple[dict[str, object], dict[str, object]]] = []
    for raw in shortlist_candidates(connection, image_candidate)[:limit]:
        raw_asset_id = str(raw["raw_asset_id"])
        raw_row = asset_with_signature(connection, raw_asset_id)
        if raw_row is None:
            continue
        distance = hamming_distance(probe["phash"], raw_row["phash"])
        if distance > max_hamming:
            continue
        proposals.append(
            (
                raw_row,
                {
                    "kind": "raw_proposal",
                    "relation": "raw_candidate",
                    "score": max(0.0, 1.0 - distance / 64.0),
                    "evidence": {
                        "hamming": distance,
                        "raw_path": raw["path"],
                        "stem_key": raw["stem_key"],
                    },
                },
            )
        )
    proposals.sort(key=lambda item: -float(item[1]["score"]))
    return proposals


def run_visual_cleanup(
    connection,
    catalog: CatalogPaths,
    *,
    probe_root_id: str,
    gallery_scope: str = "catalog_except_probe",
    thresholds: VisualThresholds | None = None,
    include_raw_proposals: bool = True,
    progress_callback: Callable[..., None] | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, object]:
    thresholds = thresholds or VisualThresholds()
    roots = list_user_catalog_roots(connection)
    root_ids = [str(row["root_id"]) for row in roots]
    if probe_root_id not in root_ids:
        raise ValueError(f"probe root is not an active user-declared root: {probe_root_id}")
    index_report = index_visual_signatures(
        connection,
        catalog,
        root_ids=root_ids,
        include_raw=include_raw_proposals,
        progress_callback=progress_callback,
        cancel_callback=cancel_callback,
    )
    connection.execute(
        """
        DELETE FROM similarity_groups
        WHERE probe_root_id = ? AND status IN ('pending', 'partial')
        """,
        (probe_root_id,),
    )
    connection.commit()

    probe_rows = list_assets_in_roots(connection, [probe_root_id], asset_type="image")
    created: defaultdict[str, int] = defaultdict(int)
    candidate_count = 0
    for position, probe_row in enumerate(probe_rows, start=1):
        if cancel_callback:
            cancel_callback()
        probe = asset_with_signature(connection, str(probe_row["asset_id"]))
        if probe is None:
            continue
        candidates = recall_visual_candidates(
            connection,
            str(probe["asset_id"]),
            probe_root_id=probe_root_id,
            scope=gallery_scope,
            thresholds=thresholds,
        )
        candidate_count += len(candidates)
        for candidate in candidates:
            classification = classify_visual_candidate(
                probe,
                candidate,
                catalog,
                thresholds=thresholds,
            )
            if classification is None:
                continue
            keeper_id, members = _pair_members(probe, candidate, classification)
            replace_similarity_group(
                connection,
                kind=str(classification["kind"]),
                representative_asset_id=keeper_id,
                members=members,
                probe_root_id=probe_root_id,
                commit=False,
            )
            created[str(classification["kind"])] += 1

        if include_raw_proposals:
            for raw, classification in _raw_proposals(
                connection,
                probe,
                max_hamming=thresholds.hamming_near,
            ):
                replace_similarity_group(
                    connection,
                    kind="raw_proposal",
                    representative_asset_id=str(probe["asset_id"]),
                    members=[
                        {
                            "asset_id": str(probe["asset_id"]),
                            "relation": "source",
                            "score": 1.0,
                            "file_size": int(probe.get("file_size") or 0),
                        },
                        {
                            "asset_id": str(raw["asset_id"]),
                            "relation": "raw_candidate",
                            "parent_asset_id": str(probe["asset_id"]),
                            "score": classification["score"],
                            "file_size": int(raw.get("file_size") or 0),
                            "evidence": classification["evidence"],
                        },
                    ],
                    probe_root_id=probe_root_id,
                    commit=False,
                )
                created["raw_proposal"] += 1
        connection.commit()
        if progress_callback:
            progress_callback(
                phase="visual_match",
                processed=position,
                total=len(probe_rows),
                groups=sum(created.values()),
                candidates=candidate_count,
            )

    return {
        "probe_root_id": probe_root_id,
        "gallery_scope": gallery_scope,
        "processed": len(probe_rows),
        "candidate_count": candidate_count,
        "groups": dict(sorted(created.items())),
        "index": index_report,
        "algorithm_version": VISUAL_ALGORITHM_VERSION,
        "recall_limit": thresholds.recall_limit,
    }
