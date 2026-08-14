from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

REMOTE_PROVIDER_TOKEN_KEY = "people_remote_token"
DEFAULT_REMOTE_ANALYZE_PATH = "/v1/analyze"
DEFAULT_REMOTE_HEALTH_PATH = "/health"
HTTP_TIMEOUT_SECONDS = 120.0


def _authorization_header(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    token = str(api_key).strip()
    if not token:
        return {}
    if token.lower().startswith("bearer "):
        return {"Authorization": token}
    return {"Authorization": f"Bearer {token}"}


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        **_authorization_header(api_key),
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"remote people service returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"remote people service is unreachable: {error.reason}") from error
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError as error:
        raise RuntimeError("remote people service returned invalid JSON") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("remote people service returned a non-object JSON payload")
    return parsed


def normalize_base_url(base_url: str) -> str:
    value = str(base_url or "").strip()
    if not value:
        raise ValueError("remote people service base URL is required")
    return value.rstrip("/")


def test_remote_connection(
    *,
    base_url: str,
    api_key: str | None = None,
    health_path: str = DEFAULT_REMOTE_HEALTH_PATH,
    timeout: float = 15.0,
) -> dict[str, object]:
    root = normalize_base_url(base_url)
    health_url = urljoin(f"{root}/", health_path.lstrip("/"))
    try:
        payload = _request_json(method="GET", url=health_url, api_key=api_key, timeout=timeout)
        return {"ok": True, "base_url": root, "health": payload}
    except RuntimeError:
        probe_url = urljoin(f"{root}/", DEFAULT_REMOTE_ANALYZE_PATH.lstrip("/"))
        payload = _request_json(
            method="POST",
            url=probe_url,
            payload={"probe": True},
            api_key=api_key,
            timeout=timeout,
        )
        if payload.get("ok") is False and payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return {"ok": True, "base_url": root, "health": {"status": "analyze_endpoint_reachable"}}


def analyze_asset_remote(
    *,
    base_url: str,
    asset_id: str,
    asset_path: Path | str,
    known_input_hash: str | None = None,
    api_key: str | None = None,
    analyze_path: str = DEFAULT_REMOTE_ANALYZE_PATH,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    path = Path(asset_path)
    if not path.is_file():
        raise FileNotFoundError(f"asset file is missing: {path}")
    image_bytes = path.read_bytes()
    payload: dict[str, Any] = {
        "id": str(asset_id),
        "asset_path": str(path.resolve()),
        "filename": path.name,
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
    }
    if known_input_hash:
        payload["known_input_hash"] = known_input_hash
    root = normalize_base_url(base_url)
    analyze_url = urljoin(f"{root}/", analyze_path.lstrip("/"))
    response = _request_json(method="POST", url=analyze_url, payload=payload, api_key=api_key, timeout=timeout)
    if str(response.get("id", "")) != str(asset_id):
        raise RuntimeError("remote people service returned a response for an unexpected asset")
    return response


class LocalWorkerSession:
    """Long-lived NDJSON session against the packaged people-worker binary."""

    def __init__(self, worker_path: Path, model_path: Path) -> None:
        if not worker_path.is_file():
            raise FileNotFoundError(f"People Worker is missing: {worker_path}")
        if not model_path.exists():
            raise FileNotFoundError(f"People model is missing: {model_path}")
        self._worker = subprocess.Popen(
            [str(worker_path), "--model", str(model_path), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self._worker.stdin is None or self._worker.stdout is None:
            raise RuntimeError("People Worker did not provide stdio pipes")

    def analyze(self, request: dict[str, Any]) -> dict[str, Any]:
        self._worker.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self._worker.stdin.flush()
        raw_response = self._worker.stdout.readline()
        if not raw_response:
            detail = self._worker.stderr.read().strip() if self._worker.stderr else ""
            raise RuntimeError(detail or "People Worker stopped before returning a response")
        return json.loads(raw_response)

    def close(self) -> None:
        if self._worker.stdin is not None and not self._worker.stdin.closed:
            self._worker.stdin.close()
        if self._worker.poll() is None:
            try:
                self._worker.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._worker.kill()
