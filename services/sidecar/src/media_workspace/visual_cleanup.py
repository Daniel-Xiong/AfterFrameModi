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


def _bounded_edges(
    edges: list[dict[str, object]],
    *,
    max_neighbors: int,
) -> list[dict[str, object]]:
    deduped: dict[tuple[str, str, str], dict[str, object]] = {}
    for edge in edges:
        left, right = sorted((str(edge["left"]["asset_id"]), str(edge["right"]["asset_id"])))
        key = (left, right, str(edge["classification"]["kind"]))
        if key not in deduped or float(edge["classification"]["score"]) > float(
            deduped[key]["classification"]["score"]
        ):
            deduped[key] = edge
    per_node: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in deduped.values():
        per_node[str(edge["left"]["asset_id"])].append(edge)
        per_node[str(edge["right"]["asset_id"])].append(edge)
    allowed: set[int] = set()
    for node_edges in per_node.values():
        node_edges.sort(key=lambda item: -float(item["classification"]["score"]))
        allowed.update(id(edge) for edge in node_edges[:max_neighbors])
    return [edge for edge in deduped.values() if id(edge) in allowed]


def _same_family_components(
    edges: list[dict[str, object]],
    *,
    max_members: int,
) -> list[list[dict[str, object]]]:
    family_kinds = {"exact", "compressed_family", "crop_family"}
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    relevant = [edge for edge in edges if edge["classification"]["kind"] in family_kinds]
    for edge in relevant:
        union(str(edge["left"]["asset_id"]), str(edge["right"]["asset_id"]))
    grouped: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in relevant:
        grouped[find(str(edge["left"]["asset_id"]))].append(edge)
    output: list[list[dict[str, object]]] = []
    for component_edges in grouped.values():
        nodes = {
            str(edge[side]["asset_id"])
            for edge in component_edges
            for side in ("left", "right")
        }
        if len(nodes) <= max_members:
            output.append(component_edges)
    return output


def _complete_link_components(
    edges: list[dict[str, object]],
    *,
    max_members: int,
) -> list[list[dict[str, object]]]:
    relevant = [
        edge
        for edge in edges
        if edge["classification"]["kind"] in {"burst", "near_duplicate"}
    ]
    lookup = {
        frozenset((str(edge["left"]["asset_id"]), str(edge["right"]["asset_id"]))): edge
        for edge in relevant
    }
    unassigned = {
        str(edge[side]["asset_id"])
        for edge in relevant
        for side in ("left", "right")
    }
    output: list[list[dict[str, object]]] = []
    while unassigned:
        seed = min(unassigned)
        cluster = [seed]
        candidates = sorted(
            {
                next(iter(pair - {seed}))
                for pair in lookup
                if seed in pair and len(pair - {seed}) == 1
            },
            key=lambda candidate: -float(lookup[frozenset((seed, candidate))]["classification"]["score"]),
        )
        for candidate in candidates:
            if candidate not in unassigned or len(cluster) >= max_members:
                continue
            if all(frozenset((candidate, member)) in lookup for member in cluster):
                cluster.append(candidate)
        unassigned.difference_update(cluster)
        if len(cluster) < 2:
            continue
        component_edges = [
            edge
            for pair, edge in lookup.items()
            if pair.issubset(set(cluster))
        ]
        output.append(component_edges)
    return output


def persist_clustered_edges(
    connection,
    edges: list[dict[str, object]],
    *,
    probe_root_id: str,
    thresholds: VisualThresholds,
) -> dict[str, int]:
    bounded = _bounded_edges(edges, max_neighbors=thresholds.max_neighbors_per_asset)
    groups: defaultdict[str, int] = defaultdict(int)
    components = [
        *_same_family_components(bounded, max_members=thresholds.max_cluster_members),
        *_complete_link_components(bounded, max_members=thresholds.max_cluster_members),
    ]
    for component in components:
        nodes: dict[str, dict[str, object]] = {}
        for edge in component:
            nodes[str(edge["left"]["asset_id"])] = edge["left"]
            nodes[str(edge["right"]["asset_id"])] = edge["right"]
        keeper = max(nodes.values(), key=_keeper_key)
        keeper_id = str(keeper["asset_id"])
        kinds = {str(edge["classification"]["kind"]) for edge in component}
        if kinds <= {"exact", "compressed_family", "crop_family"}:
            kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        else:
            kind = "burst" if kinds == {"burst"} else "near_duplicate"
        members: list[dict[str, object]] = [
            {
                "asset_id": keeper_id,
                "relation": "source",
                "score": 1.0,
                "file_size": int(keeper.get("file_size") or 0),
            }
        ]
        for asset_id, row in nodes.items():
            if asset_id == keeper_id:
                continue
            incident = [
                edge
                for edge in component
                if asset_id in {
                    str(edge["left"]["asset_id"]),
                    str(edge["right"]["asset_id"]),
                }
            ]
            best = max(incident, key=lambda edge: float(edge["classification"]["score"]))
            relation = str(best["classification"]["relation"])
            if kind == "mixed" and relation not in {"duplicate", "compressed_of", "crop_of"}:
                relation = "visually_similar"
            members.append(
                {
                    "asset_id": asset_id,
                    "relation": relation,
                    "parent_asset_id": keeper_id,
                    "score": float(best["classification"]["score"]),
                    "file_size": int(row.get("file_size") or 0),
                    "evidence": best["classification"]["evidence"],
                }
            )
        replace_similarity_group(
            connection,
            kind=kind,
            representative_asset_id=keeper_id,
            members=members,
            probe_root_id=probe_root_id,
            commit=False,
        )
        groups[kind] += 1
    return dict(groups)


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
    classified_edges: list[dict[str, object]] = []
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
            classified_edges.append(
                {
                    "left": probe,
                    "right": candidate,
                    "classification": classification,
                }
            )

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

    clustered = persist_clustered_edges(
        connection,
        classified_edges,
        probe_root_id=probe_root_id,
        thresholds=thresholds,
    )
    for kind, count in clustered.items():
        created[kind] += count
    connection.commit()

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
