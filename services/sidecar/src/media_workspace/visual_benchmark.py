from __future__ import annotations

import time
import tracemalloc
from pathlib import Path
from sqlite3 import Connection

from .catalog import CatalogPaths, ensure_catalog
from .config import VisualThresholds
from .db import list_user_catalog_roots
from .visual_cleanup import run_visual_cleanup
from .visual_index import index_visual_signatures


def _round_elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 4)


def benchmark_visual_cleanup(
    connection: Connection,
    catalog: CatalogPaths | Path,
    *,
    probe_root_id: str,
    gallery_scope: str = "catalog_except_probe",
    thresholds: VisualThresholds | None = None,
    include_raw_proposals: bool = False,
    track_memory: bool = False,
) -> dict[str, object]:
    """Measure visual signature indexing and bounded cleanup on a catalog fixture."""
    thresholds = thresholds or VisualThresholds()
    catalog_paths = ensure_catalog(catalog) if not isinstance(catalog, CatalogPaths) else catalog
    memory_peak_kb: int | None = None
    if track_memory:
        tracemalloc.start()

    root_ids = [str(row["root_id"]) for row in list_user_catalog_roots(connection)]
    index_start = time.perf_counter()
    index_report = index_visual_signatures(
        connection,
        catalog_paths,
        root_ids=root_ids,
        include_raw=include_raw_proposals,
    )
    index_elapsed = _round_elapsed(index_start)

    cleanup_start = time.perf_counter()
    cleanup_report = run_visual_cleanup(
        connection,
        catalog_paths,
        probe_root_id=probe_root_id,
        gallery_scope=gallery_scope,
        thresholds=thresholds,
        include_raw_proposals=include_raw_proposals,
    )
    cleanup_elapsed = _round_elapsed(cleanup_start)

    if track_memory:
        _, peak = tracemalloc.get_traced_memory()
        memory_peak_kb = int(peak / 1024)
        tracemalloc.stop()

    processed = int(cleanup_report["processed"])
    candidate_count = int(cleanup_report["candidate_count"])
    recall_limit = int(cleanup_report["recall_limit"])
    comparison_budget = processed * recall_limit

    return {
        "probe_root_id": probe_root_id,
        "gallery_scope": gallery_scope,
        "thresholds": {
            "recall_limit": recall_limit,
            "metadata_channel_limit": thresholds.metadata_channel_limit,
            "hash_channel_limit": thresholds.hash_channel_limit,
        },
        "stages": {
            "visual_index": {
                "elapsed_seconds": index_elapsed,
                **index_report,
            },
            "visual_match": {
                "elapsed_seconds": cleanup_elapsed,
                "processed": processed,
                "candidate_count": candidate_count,
                "groups": cleanup_report["groups"],
            },
        },
        "invariants": {
            "comparison_budget": comparison_budget,
            "candidate_count_within_budget": candidate_count <= comparison_budget,
            "recall_per_probe_max": recall_limit,
        },
        "memory_peak_kb": memory_peak_kb,
        "algorithm_version": cleanup_report["algorithm_version"],
    }
