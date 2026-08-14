"""
Minimal FastAPI face-service contract for AfterFrame remote people indexing.

Run on your GPU machine:
  pip install fastapi uvicorn
  uvicorn RESOURCES.face_service_example:app --host 0.0.0.0 --port 8000

AfterFrame Settings → People → Remote FastAPI:
  Service URL: http://<host>:8000
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="AfterFrame Face Service", version="0.1.0")


class AnalyzeRequest(BaseModel):
    id: str
    asset_path: str | None = None
    filename: str | None = None
    known_input_hash: str | None = None
    image_base64: str
    probe: bool | None = None


def _input_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _fake_embedding(seed: bytes) -> list[float]:
    # Replace this with your real detector + ArcFace pipeline.
    digest = hashlib.sha256(seed).digest()
  values = [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(512)]
  norm = sum(value * value for value in values) ** 0.5 or 1.0
  return [value / norm for value in values]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/analyze")
def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    if request.probe:
        return {"ok": True}
    try:
        image_bytes = base64.b64decode(request.image_base64)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid image_base64: {error}") from error
    input_hash = _input_hash(image_bytes)
    if request.known_input_hash and request.known_input_hash == input_hash:
        return {
            "id": request.id,
            "ok": True,
            "skipped": True,
            "input_hash": input_hash,
            "image_size": {"width": 0, "height": 0},
            "faces": [],
            "error": None,
        }
    embedding = _fake_embedding(image_bytes)
    return {
        "id": request.id,
        "ok": True,
        "skipped": False,
        "input_hash": input_hash,
        "image_size": {"width": 640, "height": 480},
        "faces": [{
            "bounding_box": [0.2, 0.2, 0.4, 0.4],
            "landmarks": [0.25, 0.3, 0.35, 0.3, 0.3, 0.38, 0.27, 0.45, 0.33, 0.45],
            "confidence": 0.99,
            "quality": "standard",
            "embedding": embedding,
        }],
        "error": None,
    }
