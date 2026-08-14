from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterable

from .catalog import CatalogPaths
from .db import get_visual_signature, upsert_preview_entry, upsert_visual_signature
from .preview_service import PreviewService
from .visual_similarity import (
    VISUAL_ALGORITHM_VERSION,
    perceptual_hash,
    phash_bands,
    phash_hex,
    region_hashes,
)


ProgressCallback = Callable[..., None]


def _preview_fingerprint(path: Path) -> str:
    stat = path.stat()
    payload = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _assets_for_roots(
    connection,
    root_ids: Iterable[str],
    *,
    include_raw: bool,
) -> list:
    roots = sorted(set(root_ids))
    if not roots:
        return []
    placeholders = ",".join("?" for _ in roots)
    asset_types = ("image", "raw") if include_raw else ("image",)
    type_placeholders = ",".join("?" for _ in asset_types)
    return connection.execute(
        f"""
        SELECT DISTINCT assets.*,
               preview.relative_path AS preview_relative_path,
               preview.status AS preview_status
        FROM asset_root_memberships membership
        JOIN assets ON assets.asset_id = membership.asset_id
        LEFT JOIN preview_entries preview
          ON preview.asset_id = assets.asset_id AND preview.kind = 'preview'
        WHERE membership.root_id IN ({placeholders})
          AND assets.asset_type IN ({type_placeholders})
          AND assets.status = 'active'
          AND assets.exists_on_disk = 1
        ORDER BY assets.asset_id
        """,
        [*roots, *asset_types],
    ).fetchall()


def index_visual_signatures(
    connection,
    catalog: CatalogPaths,
    *,
    root_ids: Iterable[str],
    include_raw: bool = True,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, object]:
    rows = _assets_for_roots(connection, root_ids, include_raw=include_raw)
    preview_service = PreviewService(catalog)
    indexed = skipped = failed = 0
    failures: list[dict[str, str]] = []
    total = len(rows)
    for position, row in enumerate(rows, start=1):
        if cancel_callback:
            cancel_callback()
        asset_id = str(row["asset_id"])
        try:
            preview_path: Path | None = None
            relative = row["preview_relative_path"]
            if relative and row["preview_status"] == "ready":
                candidate = (catalog.root / str(relative)).resolve()
                if candidate.exists() and candidate.stat().st_size:
                    preview_path = candidate
            if preview_path is None:
                result = preview_service.generate_for_row(row, "preview")
                preview_path = (catalog.root / result.relative_path).resolve()
                upsert_preview_entry(
                    connection,
                    asset_id,
                    kind="preview",
                    relative_path=result.relative_path,
                    width=result.width,
                    height=result.height,
                    status=result.status,
                    commit=False,
                )

            fingerprint = _preview_fingerprint(preview_path)
            existing = get_visual_signature(connection, asset_id)
            if (
                not force
                and existing is not None
                and str(existing["preview_fingerprint"]) == fingerprint
                and str(existing["algorithm_version"]) == VISUAL_ALGORITHM_VERSION
            ):
                skipped += 1
            else:
                value = perceptual_hash(preview_path)
                width = int(row["meta_width"] or 0)
                height = int(row["meta_height"] or 0)
                if not width or not height:
                    from PIL import Image

                    with Image.open(preview_path) as image:
                        width, height = image.size
                upsert_visual_signature(
                    connection,
                    asset_id=asset_id,
                    phash=phash_hex(value),
                    bands=phash_bands(value),
                    width=width,
                    height=height,
                    file_size=int(row["file_size"]),
                    preview_fingerprint=fingerprint,
                    regions=region_hashes(preview_path),
                    commit=False,
                )
                indexed += 1
            if position % 25 == 0:
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - one unreadable asset must not abort indexing
            failed += 1
            failures.append({"asset_id": asset_id, "error": str(exc)})
        if progress_callback:
            progress_callback(
                phase="visual_index",
                processed=position,
                total=total,
                indexed=indexed,
                skipped=skipped,
                failed=failed,
            )
    connection.commit()
    return {
        "processed": total,
        "indexed": indexed,
        "skipped": skipped,
        "failed": failed,
        "failures": failures[:100],
        "algorithm_version": VISUAL_ALGORITHM_VERSION,
    }
