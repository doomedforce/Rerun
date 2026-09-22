#!/usr/bin/env python3
"""Refresh Rerun's official Los Angeles screening feed.

This module is deliberately importable by ``server.py`` and executable from a
terminal.  A failed refresh never deletes the last known-good catalog.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scraper import LA_TZ, SOURCE_REGISTRY, VENUE_CATALOG, scrape_los_angeles


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RERUN_DATA_DIR") or ROOT / "data").expanduser().resolve()
REFRESH_TTL_SECONDS = int(os.environ.get("RERUN_REFRESH_TTL_SECONDS", "1800"))
RETRY_BACKOFF_SECONDS = int(os.environ.get("RERUN_REFRESH_RETRY_SECONDS", "600"))
FEED_POLICY_VERSION = 19
LOCK_PATH = DATA_DIR / ".refresh.lock"


def read_json(filename: str, fallback):
    try:
        return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def write_json(filename: str, value) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / filename
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(target)


@contextmanager
def refresh_file_lock():
    """Serialize refreshes across worker, server, and manual CLI processes."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover - Windows fallback
            yield


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_refresh_meta() -> dict:
    return read_json("last-refresh.json", {})


def last_verified_meta() -> dict:
    """Return the newest genuinely verified catalog metadata, if any.

    The packaged seed data is deliberately not considered a verified catalog.
    That distinction prevents a fresh deploy from quietly presenting an old
    bundled feed as if it had been checked against the live official sources.
    """

    verified_snapshot = read_json("last-verified-snapshot.json", {})
    current_snapshot = read_json("snapshot.json", {})
    candidates = [
        verified_snapshot.get("meta", {}) if isinstance(verified_snapshot, dict) else {},
        current_snapshot.get("meta", {}) if isinstance(current_snapshot, dict) else {},
        read_json("last-refresh.json", {}),
    ]
    for meta in candidates:
        if not isinstance(meta, dict):
            continue
        if meta.get("lastVerifiedAt"):
            return meta
        if meta.get("status") == "ok" and meta.get("lastRefresh"):
            return meta
    return {}


def refresh_is_stale(meta: dict | None = None, now: datetime | None = None, force: bool = False) -> bool:
    if force:
        return True
    meta = meta if meta is not None else load_refresh_meta()
    if not meta:
        return True
    if meta.get("feedPolicyVersion") != FEED_POLICY_VERSION:
        return True
    retry_at = parse_timestamp(meta.get("nextRetryAt"))
    current = now or utc_now()
    if retry_at and current < retry_at:
        return False
    if meta.get("mode") == "seed" or meta.get("status") in {"seed", "unknown"}:
        return True
    checked = parse_timestamp(meta.get("lastRefresh") or meta.get("checkedAt"))
    if not checked:
        return True
    return (current - checked).total_seconds() >= REFRESH_TTL_SECONDS


def official_venues() -> list[dict]:
    venues: list[dict] = []
    seen: set[str] = set()
    for info in VENUE_CATALOG.values():
        name = info["name"]
        if name in seen:
            continue
        seen.add(name)
        venues.append(
            {
                "id": name.lower().replace(" ", "-"),
                "name": name,
                "city": "los-angeles",
                "neighborhood": info["neighborhood"],
                "address": info["address"],
                "source": "Official venue program pages",
            }
        )
    return venues


def _refresh_los_angeles(now: datetime | None = None, force: bool = False) -> dict:
    started = utc_now()
    result = scrape_los_angeles(now=now or started.astimezone(LA_TZ), force=force)
    prior_verified_snapshot = read_json("last-verified-snapshot.json", {})
    if not isinstance(prior_verified_snapshot, dict):
        prior_verified_snapshot = {}
    legacy_snapshot = read_json("snapshot.json", {})
    legacy_meta = legacy_snapshot.get("meta", {}) if isinstance(legacy_snapshot, dict) else {}
    if (
        not isinstance(prior_verified_snapshot.get("screenings"), list)
        and isinstance(legacy_snapshot, dict)
        and isinstance(legacy_snapshot.get("screenings"), list)
        and isinstance(legacy_meta, dict)
        and legacy_meta.get("status") == "ok"
    ):
        # One-time in-place migration for catalogs created before the dedicated
        # last-verified snapshot existed. Seed data never qualifies here.
        prior_verified_snapshot = legacy_snapshot
    prior_verified_meta = last_verified_meta()
    prior_verified_screenings = prior_verified_snapshot.get("screenings", [])
    existing = read_json("screenings.json", prior_verified_screenings)
    if not isinstance(existing, list):
        existing = prior_verified_screenings if isinstance(prior_verified_screenings, list) else []
    existing_non_la = [item for item in existing if item.get("city") != "los-angeles"]
    events = result.get("screenings", [])
    sources = result.get("sources", [])
    source_count = len(sources)
    required_source_count = sum(1 for source in sources if source.get("required", True))
    successful_sources = result.get("successfulSources", 0)
    successful_required_sources = result.get(
        "successfulRequiredSources",
        sum(1 for source in sources if source.get("required", True) and source.get("status") == "verified"),
    )
    source_errors = list(result.get("blockingErrors", result.get("errors", [])))
    optional_errors = list(result.get("optionalErrors", []))

    verified = (
        required_source_count > 0
        and successful_required_sources == required_source_count
        and not source_errors
    )

    # Never replace a verified catalog with a partial required-source crawl.
    # On an outage, preserve the separately stored last verified catalog and
    # expose current source diagnostics in the snapshot metadata instead.
    if verified:
        merged = sorted(existing_non_la + events, key=lambda item: item.get("startAt", ""))
        write_json("screenings.json", merged)
        write_json("venues.json", official_venues())
    else:
        merged = prior_verified_screenings if isinstance(prior_verified_screenings, list) else existing

    checked_at = result.get("checkedAt") or utc_now().isoformat().replace("+00:00", "Z")
    if verified:
        status = "ok"
    elif events:
        status = "degraded"
    else:
        status = "error"
    meta = {
        "lastRefresh": checked_at,
        "checkedAt": checked_at,
        "startedAt": started.isoformat().replace("+00:00", "Z"),
        "durationMs": int((utc_now() - started).total_seconds() * 1000),
        "status": status,
        "feedPolicyVersion": FEED_POLICY_VERSION,
        "mode": "live" if verified else ("last-verified" if prior_verified_meta else "unverified"),
        "stale": status != "ok",
        "sourceCount": source_count,
        "requiredSourceCount": required_source_count,
        "verifiedSourceCount": successful_sources,
        "verifiedRequiredSourceCount": successful_required_sources,
        "eventCount": len(events) if verified else len([item for item in merged if item.get("city") == "los-angeles"]),
        "errors": source_errors[:10],
        "optionalErrors": optional_errors[:10],
        "networkRequests": result.get("networkRequests", 0),
        "cacheHits": result.get("cacheHits", 0),
    }
    if verified:
        meta["lastVerifiedAt"] = checked_at
    elif prior_verified_meta.get("lastVerifiedAt"):
        meta["lastVerifiedAt"] = prior_verified_meta["lastVerifiedAt"]
    elif prior_verified_meta.get("status") == "ok" and prior_verified_meta.get("lastRefresh"):
        meta["lastVerifiedAt"] = prior_verified_meta["lastRefresh"]
    if status != "ok":
        meta["nextRetryAt"] = (utc_now() + timedelta(seconds=RETRY_BACKOFF_SECONDS)).isoformat().replace("+00:00", "Z")
    write_json("sources.json", result.get("sources", []))
    write_json("last-refresh.json", meta)
    snapshot = {
        "schemaVersion": 2,
        "catalogVersion": checked_at,
        "generatedAt": utc_now().isoformat().replace("+00:00", "Z"),
        "verified": verified,
        "screenings": merged,
        "venues": official_venues() if verified else prior_verified_snapshot.get("venues", official_venues()),
        "sources": result.get("sources", []),
        "meta": meta,
    }
    write_json("snapshot.json", snapshot)
    if verified:
        write_json("last-verified-snapshot.json", snapshot)
    return {**result, "meta": meta, "wroteEvents": verified}


def refresh_los_angeles(now: datetime | None = None, force: bool = False) -> dict:
    with refresh_file_lock():
        return _refresh_los_angeles(now=now, force=force)


def main() -> None:
    result = refresh_los_angeles(force=True)
    print(json.dumps({"meta": result["meta"], "sources": result.get("sources", [])}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
