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
DATA_DIR = ROOT / "data"
REFRESH_TTL_SECONDS = int(os.environ.get("RERUN_REFRESH_TTL_SECONDS", "1800"))
RETRY_BACKOFF_SECONDS = int(os.environ.get("RERUN_REFRESH_RETRY_SECONDS", "600"))
FEED_POLICY_VERSION = 13
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
    existing = read_json("screenings.json", [])
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

    # A transient outage must not wipe the last known-good records. If at least
    # one source returned events, replace the LA slice and mark the feed
    # degraded so the UI can be honest about partial coverage.
    wrote_events = bool(events) or (
        required_source_count > 0
        and successful_required_sources == required_source_count
        and not source_errors
    )
    if wrote_events:
        merged = sorted(existing_non_la + events, key=lambda item: item.get("startAt", ""))
        write_json("screenings.json", merged)
        write_json("venues.json", official_venues())
    else:
        merged = existing

    checked_at = result.get("checkedAt") or utc_now().isoformat().replace("+00:00", "Z")
    if (
        required_source_count > 0
        and successful_required_sources == required_source_count
        and not source_errors
    ):
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
        "mode": "live" if wrote_events else "stale",
        "stale": status != "ok",
        "sourceCount": source_count,
        "requiredSourceCount": required_source_count,
        "verifiedSourceCount": successful_sources,
        "verifiedRequiredSourceCount": successful_required_sources,
        "eventCount": len(events) if wrote_events else len([item for item in merged if item.get("city") == "los-angeles"]),
        "errors": source_errors[:10],
        "optionalErrors": optional_errors[:10],
        "networkRequests": result.get("networkRequests", 0),
        "cacheHits": result.get("cacheHits", 0),
    }
    if status != "ok":
        meta["nextRetryAt"] = (utc_now() + timedelta(seconds=RETRY_BACKOFF_SECONDS)).isoformat().replace("+00:00", "Z")
    write_json("sources.json", result.get("sources", []))
    write_json("last-refresh.json", meta)
    write_json(
        "snapshot.json",
        {
            "schemaVersion": 1,
            "catalogVersion": checked_at,
            "generatedAt": utc_now().isoformat().replace("+00:00", "Z"),
            "verified": status == "ok",
            "screenings": merged,
            "venues": official_venues(),
            "sources": result.get("sources", []),
            "meta": meta,
        },
    )
    return {**result, "meta": meta, "wroteEvents": wrote_events}


def refresh_los_angeles(now: datetime | None = None, force: bool = False) -> dict:
    with refresh_file_lock():
        return _refresh_los_angeles(now=now, force=force)


def main() -> None:
    result = refresh_los_angeles(force=True)
    print(json.dumps({"meta": result["meta"], "sources": result.get("sources", [])}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
