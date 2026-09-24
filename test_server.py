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
    def test_tmdb_image_host_is_approved_for_the_artwork_proxy(self):
        self.assertTrue(server.image_host_allowed("image.tmdb.org"))
        self.assertFalse(server.image_host_allowed("image.tmdb.org.example.test"))

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
            "artwork": {"enabled": True, "attempted": 1, "matched": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(refresh, "DATA_DIR", data_dir), patch.object(refresh, "LOCK_PATH", data_dir / ".refresh.lock"), patch.object(refresh, "scrape_los_angeles", return_value=result):
                refresh.refresh_los_angeles(force=True)
            snapshot = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
            self.assertTrue(snapshot["verified"])
            self.assertEqual(snapshot["screenings"], [event])
            self.assertEqual(snapshot["meta"]["status"], "ok")
            self.assertEqual(snapshot["meta"]["lastVerifiedAt"], "2026-09-09T00:00:00Z")
            self.assertEqual(snapshot["meta"]["artwork"]["matched"], 1)
            self.assertTrue((data_dir / "last-verified-snapshot.json").exists())

    def test_last_verified_snapshot_serves_during_required_source_outage(self):
        event = {
            "id": "verified-event",
            "city": "los-angeles",
            "title": "Verified Film",
            "startAt": "2099-09-10T20:00:00-07:00",
        }
        source = {"id": "test-source", "name": "Test source", "required": True, "status": "error", "errors": ["timeout"]}
        meta = {
            "status": "error",
            "stale": True,
            "lastRefresh": "2026-09-10T00:00:00Z",
            "lastVerifiedAt": "2026-09-09T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "snapshot.json").write_text(json.dumps({"screenings": [event], "venues": [], "sources": [source], "meta": meta}), encoding="utf-8")
            (data_dir / "sources.json").write_text(json.dumps([source]), encoding="utf-8")
            (data_dir / "last-refresh.json").write_text(json.dumps(meta), encoding="utf-8")
            original_data_dir = server.DATA_DIR
            server.DATA_DIR = data_dir
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RerunHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{httpd.server_port}/api/screenings?city=los-angeles"
                response = json.loads(urlopen(url, timeout=2).read().decode("utf-8"))
                self.assertEqual(response["data"], [event])
                self.assertTrue(response["meta"]["servingLastVerifiedSnapshot"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                server.DATA_DIR = original_data_dir

    def test_failed_refresh_keeps_the_prior_verified_catalog(self):
        verified_event = {
            "id": "verified-event",
            "city": "los-angeles",
            "title": "Verified Film",
            "startAt": "2026-09-10T20:00:00-07:00",
        }
        verified_result = {
            "screenings": [verified_event],
            "sources": [{"id": "test-source", "name": "Test", "required": True, "status": "verified"}],
            "successfulSources": 1,
            "successfulRequiredSources": 1,
            "blockingErrors": [],
            "optionalErrors": [],
            "checkedAt": "2026-09-09T00:00:00Z",
        }
        failed_result = {
            "screenings": [{**verified_event, "id": "partial-event", "title": "Partial Film"}],
            "sources": [{"id": "test-source", "name": "Test", "required": True, "status": "error", "errors": ["timeout"]}],
            "successfulSources": 0,
            "successfulRequiredSources": 0,
            "blockingErrors": ["Test: timeout"],
            "optionalErrors": [],
            "checkedAt": "2026-09-09T01:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(refresh, "DATA_DIR", data_dir), patch.object(refresh, "LOCK_PATH", data_dir / ".refresh.lock"), patch.object(refresh, "scrape_los_angeles", side_effect=[verified_result, failed_result]):
                refresh.refresh_los_angeles(force=True)
                refresh.refresh_los_angeles(force=True)
            snapshot = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
            self.assertFalse(snapshot["verified"])
            self.assertEqual(snapshot["screenings"], [verified_event])
            self.assertEqual(snapshot["meta"]["lastVerifiedAt"], "2026-09-09T00:00:00Z")
            self.assertEqual(snapshot["sources"][0]["status"], "error")

    def test_required_source_outage_still_surfaces_a_verified_optional_source_update(self):
        prior_event = {
            "id": "american-cinematheque-prior-film-aero-theatre-20260910-1900",
            "sourceId": "american-cinematheque",
            "city": "los-angeles",
            "title": "Prior Film",
            "startAt": "2026-09-10T19:00:00-07:00",
        }
        fresh_academy_event = {
            "id": "academy-museum-fresh-film-academy-museum-20261003-1930",
            "sourceId": "academy-museum",
            "city": "los-angeles",
            "title": "Fresh Academy Film",
            "startAt": "2026-10-03T19:30:00-07:00",
        }
        verified_result = {
            "screenings": [prior_event],
            "sources": [
                {"id": "american-cinematheque", "name": "American Cinematheque", "required": True, "status": "verified", "eventCount": 1},
                {"id": "academy-museum", "name": "Academy Museum of Motion Pictures", "required": False, "status": "degraded", "eventCount": 0},
            ],
            "successfulSources": 1,
            "successfulRequiredSources": 1,
            "blockingErrors": [],
            "optionalErrors": [],
            "checkedAt": "2026-09-09T00:00:00Z",
        }
        partial_result = {
            "screenings": [fresh_academy_event],
            "sources": [
                {"id": "american-cinematheque", "name": "American Cinematheque", "required": True, "status": "error", "eventCount": 0, "errors": ["timeout"]},
                {"id": "academy-museum", "name": "Academy Museum of Motion Pictures", "required": False, "status": "verified", "eventCount": 1},
            ],
            "successfulSources": 1,
            "successfulRequiredSources": 0,
            "blockingErrors": ["American Cinematheque: timeout"],
            "optionalErrors": [],
            "checkedAt": "2026-09-09T01:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(refresh, "DATA_DIR", data_dir), patch.object(refresh, "LOCK_PATH", data_dir / ".refresh.lock"), patch.object(refresh, "scrape_los_angeles", side_effect=[verified_result, partial_result]):
                refresh.refresh_los_angeles(force=True)
                refresh.refresh_los_angeles(force=True)
            snapshot = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
            last_verified = json.loads((data_dir / "last-verified-snapshot.json").read_text(encoding="utf-8"))
        self.assertFalse(snapshot["verified"])
        self.assertEqual({event["title"] for event in snapshot["screenings"]}, {"Prior Film", "Fresh Academy Film"})
        self.assertEqual(snapshot["meta"]["partialRefreshSourceIds"], ["academy-museum"])
        self.assertEqual(last_verified["screenings"], [prior_event])

    def test_raw_catalog_files_are_not_public_static_assets(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RerunHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{httpd.server_port}/data/screenings.json"
            with self.assertRaises(HTTPError) as raised:
                urlopen(url, timeout=2)
            self.assertEqual(raised.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_alert_store_is_idempotent_and_uses_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.object(alerts, "DATA_DIR", data_dir), patch.object(alerts, "DB_PATH", data_dir / "rerun.sqlite3"):
                self.assertEqual(alerts.add_subscriber("FilmFan@example.com"), "created")
                self.assertEqual(alerts.add_subscriber("filmfan@example.com"), "duplicate")
                self.assertTrue((data_dir / "rerun.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
