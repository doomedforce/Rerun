import unittest
import json
import tempfile
import threading
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
import refresh
import alerts


class ServerTests(unittest.TestCase):
    def test_optional_source_failure_does_not_block_verified_required_feed(self):
        sources = [
            {"name": "American Cinematheque", "status": "verified", "required": True},
            {"name": "Vista Theater Hollywood", "status": "verified", "required": True},
            {"id": "new-beverly", "name": "New Beverly Cinema", "status": "degraded"},
            {"name": "Vidiots", "status": "degraded", "required": False},
        ]
        meta = {"status": "ok", "verifiedRequiredSourceCount": 2}
        self.assertTrue(server.feed_is_complete(meta, sources))

    def test_required_source_failure_still_blocks_feed(self):
        sources = [
            {"name": "American Cinematheque", "status": "verified", "required": True},
            {"name": "Vista Theater Hollywood", "status": "degraded", "required": True},
            {"name": "New Beverly Cinema", "status": "degraded", "required": False},
        ]
        meta = {"status": "ok", "verifiedRequiredSourceCount": 1}
        self.assertFalse(server.feed_is_complete(meta, sources))

    def test_browse_refresh_query_is_rejected_without_running_a_crawl(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "snapshot.json").write_text(json.dumps({
                "screenings": [], "venues": [], "sources": [],
                "meta": {"status": "unknown"}
            }), encoding="utf-8")
            original_data_dir = server.DATA_DIR
            server.DATA_DIR = data_dir
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RerunHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{httpd.server_port}/api/screenings?city=los-angeles&refresh=1"
                with self.assertRaises(HTTPError) as raised:
                    urlopen(url, timeout=2)
                self.assertEqual(raised.exception.code, 403)
            finally:
                httpd.shutdown()
                httpd.server_close()
                server.DATA_DIR = original_data_dir

    def test_refresh_writes_one_catalog_snapshot(self):
        event = {
            "id": "test-event",
            "city": "los-angeles",
            "title": "Test Film",
            "startAt": "2026-09-10T20:00:00-07:00",
        }
        result = {
            "screenings": [event],
            "sources": [{"id": "test-source", "name": "Test", "required": True, "status": "verified"}],
            "successfulSources": 1,
            "successfulRequiredSources": 1,
            "blockingErrors": [],
            "optionalErrors": [],
            "checkedAt": "2026-09-09T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(refresh, "DATA_DIR", data_dir), patch.object(refresh, "LOCK_PATH", data_dir / ".refresh.lock"), patch.object(refresh, "scrape_los_angeles", return_value=result):
                refresh.refresh_los_angeles(force=True)
            snapshot = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
            self.assertTrue(snapshot["verified"])
            self.assertEqual(snapshot["screenings"], [event])
            self.assertEqual(snapshot["meta"]["status"], "ok")

    def test_alert_store_is_idempotent_and_uses_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(alerts, "DATA_DIR", data_dir), patch.object(alerts, "DB_PATH", data_dir / "rerun.sqlite3"):
                self.assertEqual(alerts.add_subscriber("FilmFan@example.com"), "created")
                self.assertEqual(alerts.add_subscriber("filmfan@example.com"), "duplicate")
                self.assertTrue((data_dir / "rerun.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
