from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .visual_similarity import aligned_error, hamming_distance, perceptual_hash, phash_hex


SAME_FRAME_RELATIONS = {"exact", "compressed_of"}


def evaluate_visual_truth(truth_csv: Path, *, max_hamming: int = 16) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    distances: Counter[str] = Counter()
    with truth_csv.open("r", encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            probe = Path(record["probe_path"]).resolve()
            gallery = Path(record["gallery_path"]).resolve()
            relation = (record.get("relation") or "negative").strip()
            probe_hash = perceptual_hash(probe)
            gallery_hash = perceptual_hash(gallery)
            distance = hamming_distance(probe_hash, gallery_hash)
            distances[relation] += 1
            rows.append(
                {
                    "probe_path": str(probe),
                    "gallery_path": str(gallery),
                    "relation": relation,
                    "probe_phash": phash_hex(probe_hash),
                    "gallery_phash": phash_hex(gallery_hash),
                    "hamming": distance,
                    "aligned_error": aligned_error(probe, gallery),
                    "notes": (record.get("notes") or "").strip(),
                }
            )

    threshold_metrics: list[dict[str, object]] = []
    for threshold in range(max_hamming + 1):
        true_positive = false_positive = false_negative = true_negative = 0
        for row in rows:
            expected = row["relation"] in SAME_FRAME_RELATIONS
            predicted = int(row["hamming"]) <= threshold
            if expected and predicted:
                true_positive += 1
            elif expected:
                false_negative += 1
            elif predicted:
                false_positive += 1
            else:
                true_negative += 1
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        threshold_metrics.append(
            {
                "threshold": threshold,
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "true_negative": true_negative,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
            }
        )

    return {
        "summary": {
            "total": len(rows),
            "relations": dict(sorted(distances.items())),
        },
        "threshold_metrics": threshold_metrics,
        "rows": rows,
    }
