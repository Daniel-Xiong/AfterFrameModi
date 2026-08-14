from __future__ import annotations

import json
import math
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from media_workspace.catalog import ensure_catalog
from media_workspace.db import connect, create_job, init_db, set_catalog_path
from media_workspace.job_runner import run_people_index_job
from media_workspace.people_inference import analyze_asset_remote, test_remote_connection


class _RemoteFaceHandler(BaseHTTPRequestHandler):
    embedding = [1.0 / math.sqrt(512)] * 512

    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self._write_json(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/analyze":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if payload.get("probe"):
            self._write_json(200, {"ok": True})
            return
        if payload.get("known_input_hash") == "input-hash-a":
            response = {
                "id": payload["id"],
                "ok": True,
                "skipped": True,
                "input_hash": "input-hash-a",
                "image_size": {"width": 100, "height": 100},
                "faces": [],
                "error": None,
            }
        else:
            response = {
                "id": payload["id"],
                "ok": True,
                "skipped": False,
                "input_hash": "input-hash-a",
                "image_size": {"width": 100, "height": 100},
                "faces": [{
                    "bounding_box": [0.1, 0.1, 0.5, 0.5],
                    "landmarks": [0.1, 0.1, 0.2, 0.1, 0.15, 0.2, 0.1, 0.3, 0.2, 0.3],
                    "confidence": 1.0,
                    "quality": "standard",
                    "embedding": self.embedding,
                }],
                "error": None,
            }
        self._write_json(200, response)

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RemotePeopleInferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RemoteFaceHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def test_remote_connection_health(self) -> None:
        payload = test_remote_connection(base_url=self.base_url)
        self.assertTrue(payload["ok"])

    def test_remote_job_indexes_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = ensure_catalog(root / "demo.afcatalog")
            source = root / "portrait.jpg"
            source.write_bytes(b"remote-face-fixture")

            connection = connect(catalog.db_path)
            init_db(connection)
            set_catalog_path(connection, catalog.root)
            connection.execute(
                """
                INSERT INTO assets (
                    asset_id, asset_type, canonical_path, stem, normalized_stem,
                    stem_key, extension, fingerprint, file_size, modified_time
                ) VALUES (?, 'image', ?, 'portrait', 'portrait', 'portrait', '.jpg', 'fingerprint', 1, '2026-07-10T00:00:00Z')
                """,
                ("asset-1", str(source)),
            )
            connection.commit()

            job = create_job(connection, "people_index", priority=100)
            result = run_people_index_job(
                connection,
                job["job_id"],
                model_id="remote-arcface",
                model_version="test-v1",
                model_path=None,
                manifest_hash="remote-manifest",
                inference_backend="remote_http",
                base_url=self.base_url,
            )
            self.assertEqual(result["analyzed"], 1)
            self.assertEqual(result["faces"], 1)

            response = analyze_asset_remote(
                base_url=self.base_url,
                asset_id="asset-1",
                asset_path=source,
                known_input_hash="input-hash-a",
            )
            self.assertTrue(response["skipped"])


if __name__ == "__main__":
    unittest.main()
