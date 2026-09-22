#!/usr/bin/env python3
"""Dependency-free Rerun server serving an atomic catalog snapshot.

Run with ``python server.py``. A background worker refreshes the Los Angeles
feed on a schedule; browse requests never start a crawl.
"""

import json
import hmac
import math
import mimetypes
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import refresh as refresh_service
    import alerts as alerts_service
    import instagram_publisher as instagram_service
    import refresh_worker
except ImportError:  # pragma: no cover - only relevant when embedded elsewhere
    refresh_service = None
    alerts_service = None
    instagram_service = None
    refresh_worker = None


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RERUN_DATA_DIR") or ROOT / "data").expanduser().resolve()
# Render supplies PORT and requires a public 0.0.0.0 bind. Let that hosted
# configuration win over local-development overrides that may be present in
# the service environment.
HOST = "0.0.0.0" if os.environ.get("PORT") else os.environ.get("RERUN_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("RERUN_PORT") or "4173")
BLOCKED_STATIC = {
    "/server.py",
    "/server.js",
    "/scraper.py",
    "/refresh.py",
    "/package.json",
    "/README.md",
    "/data/subscribers.json",
    "/data/screenings.json",
    "/data/venues.json",
    "/data/rerun.sqlite3",
    "/data/rerun.sqlite3-shm",
    "/data/rerun.sqlite3-wal",
    "/data/sources.json",
    "/data/last-refresh.json",
    "/data/snapshot.json",
    "/data/last-verified-snapshot.json",
    "/data/worker-state.json",
    "/data/instagram-post-state.json",
}
REFRESH_LOCK = threading.Lock()
REFRESH_TOKEN = os.environ.get("RERUN_REFRESH_TOKEN", "")
NOTIFY_WINDOW_SECONDS = max(60, int(os.environ.get("RERUN_NOTIFY_WINDOW_SECONDS", "600")))
NOTIFY_MAX_ATTEMPTS = max(1, int(os.environ.get("RERUN_NOTIFY_MAX_ATTEMPTS", "5")))
NOTIFY_ATTEMPTS = {}
NOTIFY_LOCK = threading.Lock()
IMAGE_CACHE = {}
IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_CACHE_LIMIT = 24
IMAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_ALLOWED_HOSTS = {
    "americancinematheque.com",
    "assets.americancinematheque.com",
    "thenewbev.com",
    "www.thenewbev.com",
    "vistatheaterhollywood.com",
    "www.vistatheaterhollywood.com",
    "vidiotsfoundation.org",
    "www.vidiotsfoundation.org",
    "la.teentix.org",
    "academymuseum.org",
    "www.academymuseum.org",
    "images.ctfassets.net",
    "images.contentful.com",
    "fineartstheatrebh.com",
    "ticketing.uswest.veezi.com",
    "lafilmforum.org",
    "www.lafilmforum.org",
    "oldtownmusichall.org",
    "www.oldtownmusichall.org",
    "indy-systems.imgix.net",
    "cinema.ucla.edu",
    "www.cinema.ucla.edu",
}


def read_json(filename, fallback):
    try:
        return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def write_json(filename, value):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / filename
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(target)


def normalized(value):
    return str(value or "").strip().lower()


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


SERVER_STARTED_AT = now_iso()


def read_catalog():
    current_snapshot = read_json("snapshot.json", {})
    snapshot = current_snapshot if isinstance(current_snapshot, dict) and isinstance(current_snapshot.get("screenings"), list) else read_json("last-verified-snapshot.json", {})
    if isinstance(snapshot, dict) and isinstance(snapshot.get("screenings"), list):
        snapshot_meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
        operational_meta = read_json("last-refresh.json", {})
        operational_meta = operational_meta if isinstance(operational_meta, dict) else {}
        meta = {**snapshot_meta, **operational_meta}
        if snapshot_meta.get("lastVerifiedAt") and not meta.get("lastVerifiedAt"):
            meta["lastVerifiedAt"] = snapshot_meta["lastVerifiedAt"]
        return {
            **snapshot,
            "sources": read_json("sources.json", snapshot.get("sources", [])),
            "meta": meta,
        }
    return {
        "schemaVersion": 0,
        "catalogVersion": None,
        "verified": False,
        "screenings": read_json("screenings.json", []),
        "venues": read_json("venues.json", []),
        "sources": read_json("sources.json", []),
        "meta": read_json("last-refresh.json", {}),
    }


def refresh_authorized(headers):
    if not REFRESH_TOKEN:
        return False
    provided = headers.get("X-Rerun-Refresh-Token", "")
    if not provided.lower().startswith("bearer "):
        provided = headers.get("Authorization", "")
        if provided.lower().startswith("bearer "):
            provided = provided[7:]
    return hmac.compare_digest(str(provided), REFRESH_TOKEN)


def image_host_allowed(hostname):
    host = str(hostname or "").lower().rstrip(".")
    return host in IMAGE_ALLOWED_HOSTS or any(host.endswith(f".{domain}") for domain in IMAGE_ALLOWED_HOSTS)


def fetch_remote_image(image_url):
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.hostname or not image_host_allowed(parsed.hostname):
        raise ValueError("Image source is not an approved official venue domain.")
    with IMAGE_CACHE_LOCK:
        cached = IMAGE_CACHE.get(image_url)
    if cached:
        return cached
    request = urllib.request.Request(
        image_url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": f"https://{parsed.hostname}/",
            "User-Agent": "Rerun/0.4 (+https://rerun.local; official venue artwork proxy)",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as upstream:
        content_type = (upstream.headers.get_content_type() or "").lower()
        if not content_type.startswith("image/"):
            raise ValueError("Approved source did not return an image.")
        body = upstream.read(IMAGE_MAX_BYTES + 1)
    if len(body) > IMAGE_MAX_BYTES:
        raise ValueError("Image exceeds the size limit.")
    result = (content_type, body)
    with IMAGE_CACHE_LOCK:
        if len(IMAGE_CACHE) >= IMAGE_CACHE_LIMIT:
            IMAGE_CACHE.pop(next(iter(IMAGE_CACHE)))
        IMAGE_CACHE[image_url] = result
    return result


def iso_sort_value(item):
    try:
        return datetime.fromisoformat(item.get("startAt", "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)


def current_refresh_meta():
    snapshot = read_catalog()
    return snapshot.get("meta") or read_json("last-refresh.json", {})


def source_is_required(source):
    """Resolve requiredness from current config when persisted data is old."""

    source_id = source.get("id")
    configured = getattr(refresh_service, "SOURCE_REGISTRY", {}).get(source_id, {}) if refresh_service else {}
    return source.get("required", configured.get("required", True)) is not False


def feed_is_complete(refresh_meta, sources):
    """Return true when every required official source is verified."""

    required_sources = [source for source in sources if source_is_required(source)]
    expected = len(required_sources)
    verified = int(refresh_meta.get("verifiedRequiredSourceCount", 0) or 0)
    if not verified:
        verified = sum(1 for source in required_sources if source.get("status") == "verified")
    return bool(
        expected
        and refresh_meta.get("status") == "ok"
        and verified >= expected
        and all(source.get("status") == "verified" for source in required_sources)
    )


def has_last_verified_snapshot(catalog):
    """Only a completed official-source run can unlock outage fallback data."""

    return bool(
        isinstance(catalog, dict)
        and isinstance(catalog.get("screenings"), list)
        and (catalog.get("meta") or {}).get("lastVerifiedAt")
    )


def source_health(sources):
    required = [source for source in sources if source_is_required(source)]
    unavailable = [source.get("name") for source in required if source.get("status") != "verified" and source.get("name")]
    degraded = [source.get("name") for source in sources if source.get("status") in {"degraded", "error"} and source.get("name")]
    return {
        "requiredCount": len(required),
        "verifiedRequiredCount": sum(1 for source in required if source.get("status") == "verified"),
        "unavailableRequiredSources": unavailable,
        "degradedSources": degraded,
    }


def notify_client_key(handler):
    forwarded = str(handler.headers.get("X-Forwarded-For", "")).split(",", 1)[0].strip()
    return forwarded or handler.client_address[0]


def notify_allowed(client_key):
    now = datetime.now(timezone.utc).timestamp()
    with NOTIFY_LOCK:
        window_started, count = NOTIFY_ATTEMPTS.get(client_key, (now, 0))
        if now - window_started >= NOTIFY_WINDOW_SECONDS:
            window_started, count = now, 0
        if count >= NOTIFY_MAX_ATTEMPTS:
            return False
        NOTIFY_ATTEMPTS[client_key] = (window_started, count + 1)
        # Keep this small in long-lived web processes while expiring old keys.
        if len(NOTIFY_ATTEMPTS) > 1024:
            for key, (started, _) in list(NOTIFY_ATTEMPTS.items()):
                if now - started >= NOTIFY_WINDOW_SECONDS:
                    NOTIFY_ATTEMPTS.pop(key, None)
        return True


def retry_after_seconds(refresh_meta):
    """Return a bounded delay that matches the server's next refresh window."""

    retry_at = refresh_meta.get("nextRetryAt")
    if retry_at:
        try:
            remaining = math.ceil(
                (datetime.fromisoformat(retry_at.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
            )
            if remaining > 0:
                return max(5, min(900, remaining))
        except (TypeError, ValueError):
            pass
    return 30


def maybe_refresh(force=False):
    """Refresh once for concurrent requests and return the feed health meta."""

    if refresh_service is None:
        return current_refresh_meta()
    current_meta = current_refresh_meta()
    if not force and not refresh_service.refresh_is_stale(current_meta):
        return current_meta
    with REFRESH_LOCK:
        current_meta = current_refresh_meta()
        if not force and not refresh_service.refresh_is_stale(current_meta):
            return current_meta
        try:
            result = refresh_service.refresh_los_angeles(force=force)
            return result.get("meta", current_refresh_meta())
        except Exception as error:  # preserve the last catalog on unexpected errors
            now = now_iso()
            meta = {
                **current_meta,
                "lastRefresh": now,
                "checkedAt": now,
                "status": "error",
                "stale": True,
                "errors": [str(error)],
            }
            write_json("last-refresh.json", meta)
            return meta


def feed_meta(city, data, venues, sources, refresh_meta, serving_last_verified_snapshot=False):
    return {
        "count": len(data),
        "city": city or None,
        "updatedAt": now_iso(),
        "lastRefresh": refresh_meta.get("lastRefresh"),
        "lastVerifiedAt": refresh_meta.get("lastVerifiedAt"),
        "refreshStatus": refresh_meta.get("status", "unknown"),
        "stale": bool(refresh_meta.get("stale", True)),
        "complete": feed_is_complete(refresh_meta, sources),
        "servingLastVerifiedSnapshot": bool(serving_last_verified_snapshot),
        "nextRetryAt": refresh_meta.get("nextRetryAt"),
        "source": "Official venue feeds",
        "sourceCount": len(sources),
        "requiredSourceCount": sum(1 for source in sources if source_is_required(source)),
        "sources": [
            {
                "id": source.get("id"),
                "name": source.get("name"),
                "url": source.get("url"),
                "partnerUrl": source.get("partnerUrl"),
                "status": source.get("status"),
                "required": source_is_required(source),
                "sourceMode": source.get("sourceMode", ""),
                "eventCount": source.get("eventCount", 0),
                "lastChecked": source.get("lastChecked"),
                "durationMs": source.get("durationMs"),
                "errors": source.get("errors", [])[:2],
                "warnings": source.get("warnings", [])[:2],
            }
            for source in sources
        ],
        "venueCount": len(venues),
    }


class RerunHandler(BaseHTTPRequestHandler):
    server_version = "RerunAPI/0.5"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def end_headers(self):
        """Keep browser-facing responses conservative without blocking PWA use."""

        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; form-action 'self'; "
            "base-uri 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def send_json(self, status, payload, retry_after=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(int(retry_after)))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_image(self, image_url):
        try:
            content_type, body = fetch_remote_image(image_url)
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            self.send_error(502, f"Image unavailable: {error}")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600, stale-while-revalidate=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed)
        else:
            self.handle_static(parsed.path)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.send_json(405, {"error": "Use GET or POST for API routes."})
        else:
            self.handle_static(parsed.path, head_only=True)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            if not refresh_authorized(self.headers):
                self.send_json(403, {"error": "Manual refresh is disabled or requires a valid refresh token."})
                return
            maybe_refresh(force=True)
            with REFRESH_LOCK:
                meta = current_refresh_meta()
                sources = read_catalog().get("sources", [])
            complete = feed_is_complete(meta, sources)
            retry_after = None if complete else retry_after_seconds(meta)
            self.send_json(
                200 if complete else 503,
                {
                    "ok": complete,
                    "completed": complete,
                    "meta": meta,
                    "sources": sources,
                },
                retry_after=retry_after,
            )
            return
        if parsed.path == "/api/instagram/publish":
            if not refresh_authorized(self.headers):
                self.send_json(403, {"error": "Instagram publishing requires a valid refresh token."})
                return
            if instagram_service is None or not instagram_service.enabled():
                self.send_json(503, {"error": "Instagram publisher is not configured."})
                return
            maybe_refresh(force=True)
            result = instagram_service.publish_today(force=True)
            ok = result.get("status") in {"posted", "skipped"}
            self.send_json(200 if ok else 503, {"ok": ok, **result})
            return
        if parsed.path != "/api/notify":
            self.send_json(404, {"error": "API route not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 32_000:
                self.send_json(413, {"error": "Payload too large."})
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Send valid JSON."})
            return
        # A hidden field catches ordinary form bots. Deliberately return the
        # same success shape without touching the subscriber store.
        if normalized(payload.get("website")):
            self.send_json(201, {"ok": True, "message": "You’re on the Rerun list."})
            return
        if not notify_allowed(notify_client_key(self)):
            self.send_json(429, {"error": "Please wait a few minutes before trying again."})
            return
        email = normalized(payload.get("email"))
        if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            self.send_json(422, {"error": "Enter a valid email address."})
            return
        if alerts_service is None:
            self.send_json(503, {"error": "Alert service is unavailable."})
            return
        try:
            result = alerts_service.add_subscriber(email)
        except Exception:
            self.send_json(503, {"error": "Alert service is unavailable."})
            return
        if result == "duplicate":
            self.send_json(409, {"error": "That email is already on the list."})
            return
        self.send_json(201, {"ok": True, "message": "You’re on the Rerun list."})

    def handle_api(self, parsed):
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/health":
            # Never wait on the refresh lock here. Render may use this endpoint
            # as the service health check while a full official-source crawl is
            # running; blocking would make a healthy process look unavailable
            # and can produce a platform-level 502.
            catalog = read_catalog()
            meta = catalog.get("meta") or current_refresh_meta()
            sources = catalog.get("sources", [])
            complete = feed_is_complete(meta, sources)
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "rerun-api",
                    "version": "0.5.0",
                    "updatedAt": now_iso(),
                    "feed": {
                        "status": meta.get("status", "unknown"),
                        "lastRefresh": meta.get("lastRefresh"),
                        "lastVerifiedAt": meta.get("lastVerifiedAt"),
                        "stale": bool(meta.get("stale", True)),
                        "complete": complete,
                        "servingLastVerifiedSnapshot": not complete and has_last_verified_snapshot(catalog),
                        "catalogVersion": catalog.get("catalogVersion"),
                        "sourceHealth": source_health(sources),
                    },
                    "storage": {
                        "persistentStorageConfigured": bool(os.environ.get("RERUN_DATA_DIR")),
                    },
                    "worker": read_json("worker-state.json", {"status": "not_started"}),
                },
            )
            return
        if path == "/api/image":
            image_url = query.get("url", [""])[0]
            if not image_url:
                self.send_error(400, "Missing image URL")
                return
            self.handle_image(image_url)
            return
        if path == "/api/screenings":
            city = normalized(query.get("city", [""])[0])
            if query.get("refresh", [""])[0] in {"1", "true", "yes"}:
                self.send_json(403, {"error": "Browse requests are read-only. Use authenticated POST /api/refresh."})
                return
            with REFRESH_LOCK:
                catalog = read_catalog()
                refresh_meta = catalog.get("meta") or current_refresh_meta()
                screenings = catalog.get("screenings", [])
                venues = catalog.get("venues", [])
                sources = catalog.get("sources", [])
            complete = feed_is_complete(refresh_meta, sources)
            serving_last_verified_snapshot = not complete and has_last_verified_snapshot(catalog)
            if city in {"", "los-angeles"} and not complete and not serving_last_verified_snapshot:
                meta = feed_meta(city, [], venues, sources, refresh_meta)
                retry_after = retry_after_seconds(refresh_meta)
                self.send_json(
                    503,
                    {
                        "error": "complete_live_index_unavailable",
                        "message": "Rerun is waiting for every official Los Angeles source to verify.",
                        "retryAfterSeconds": retry_after,
                        "meta": meta,
                        "sources": meta["sources"],
                    },
                    retry_after=retry_after,
                )
                return
            date = normalized(query.get("date", [""])[0])
            date_key = normalized(query.get("dateKey", [""])[0])
            term = normalized(query.get("q", query.get("query", [""]))[0])
            format_term = normalized(query.get("format", [""])[0])
            try:
                limit = min(max(int(query.get("limit", [500])[0]), 1), 500)
            except (TypeError, ValueError):
                limit = 100
            current_time = datetime.now(timezone.utc)

            def upcoming(item):
                value = iso_sort_value(item)
                return value >= current_time

            def matches(item):
                fields = [item.get("title"), item.get("director"), item.get("venue"), item.get("genre"), item.get("format"), item.get("notes"), item.get("sourceName")]
                return not term or any(term in normalized(field) for field in fields if field)

            data = [
                item
                for item in screenings
                if not city or normalized(item.get("city")) == city
                if upcoming(item)
                if not date_key or str(item.get("startAt", ""))[:10] == date_key
                if not date or date == "all" or normalized(item.get("dateGroup")) == date
                if not format_term or format_term in normalized(item.get("format"))
                if matches(item)
            ]
            data.sort(key=iso_sort_value)
            data = data[:limit]
            self.send_json(
                200,
                {
                    "data": data,
                    "meta": feed_meta(
                        city,
                        data,
                        venues,
                        sources,
                        refresh_meta,
                        serving_last_verified_snapshot=serving_last_verified_snapshot,
                    ),
                },
            )
            return
        if path.startswith("/api/screenings/"):
            screening_id = unquote(path.rsplit("/", 1)[-1])
            with REFRESH_LOCK:
                catalog = read_catalog()
                item = next((item for item in catalog.get("screenings", []) if item.get("id") == screening_id), None)
            if item and normalized(item.get("city")) == "los-angeles" and not feed_is_complete(catalog.get("meta") or {}, catalog.get("sources", [])) and not has_last_verified_snapshot(catalog):
                retry_after = retry_after_seconds(catalog.get("meta") or {})
                self.send_json(503, {"error": "complete_live_index_unavailable", "retryAfterSeconds": retry_after}, retry_after=retry_after)
                return
            self.send_json(200 if item else 404, {"data": item} if item else {"error": "Screening not found"})
            return
        if path == "/api/venues":
            city = normalized(query.get("city", [""])[0])
            with REFRESH_LOCK:
                catalog = read_catalog()
                venues = [venue for venue in catalog.get("venues", []) if not city or normalized(venue.get("city")) == city]
            complete = feed_is_complete(catalog.get("meta") or {}, catalog.get("sources", []))
            if city in {"", "los-angeles"} and not complete and not has_last_verified_snapshot(catalog):
                retry_after = retry_after_seconds(catalog.get("meta") or {})
                self.send_json(503, {"error": "complete_live_index_unavailable", "retryAfterSeconds": retry_after}, retry_after=retry_after)
                return
            self.send_json(200, {"data": venues, "meta": {"count": len(venues), "city": city or None, "updatedAt": now_iso(), "servingLastVerifiedSnapshot": not complete}})
            return
        if path == "/api/sources":
            with REFRESH_LOCK:
                catalog = read_catalog()
                sources = catalog.get("sources", [])
                meta = catalog.get("meta") or current_refresh_meta()
            self.send_json(200, {"data": sources, "meta": meta})
            return
        self.send_json(404, {"error": "API route not found"})

    def handle_static(self, requested_path, head_only=False):
        if requested_path == "/":
            requested_path = "/index.html"
        if requested_path in BLOCKED_STATIC or requested_path.startswith("/data/cache/"):
            self.send_error(404, "Not found")
            return
        relative = Path(unquote(requested_path.lstrip("/")))
        candidate = (ROOT / relative).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        if not candidate.is_file():
            self.send_error(404, "Not found")
            return
        body = candidate.read_bytes()
        self.send_response(200)
        content_type = "application/xml; charset=utf-8" if candidate.suffix.lower() == ".xml" else (mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
        self.send_header("Content-Type", content_type)
        if candidate.suffix.lower() in {".html", ".js", ".css", ".json", ".webmanifest"}:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), RerunHandler)
    print(f"Rerun is live at http://localhost:{PORT} (bound to {HOST})")
    stop_event = threading.Event()
    worker_thread = None
    if refresh_worker is not None and os.environ.get("RERUN_DISABLE_WORKER", "0").lower() not in {"1", "true", "yes"}:
        worker_thread = threading.Thread(target=refresh_worker.run_worker, args=(stop_event,), name="rerun-refresh-worker", daemon=True)
        worker_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRerun stopped.")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
