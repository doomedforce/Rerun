#!/usr/bin/env python3
"""Publish one daily Today in LA post through Instagram's Content Publishing API.

The publisher is intentionally dormant until both RERUN_INSTAGRAM_ACCESS_TOKEN
and RERUN_PUBLIC_BASE_URL are configured in the deployment environment.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RERUN_DATA_DIR") or ROOT / "data").expanduser().resolve()
STATE_PATH = DATA_DIR / "instagram-post-state.json"
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
API_VERSION = os.environ.get("RERUN_INSTAGRAM_API_VERSION", "v23.0")
API_MODE = os.environ.get("RERUN_INSTAGRAM_API_MODE", "instagram-login").strip().lower()
if API_MODE in {"facebook", "facebook-login"}:
    API_ROOT = f"https://graph.facebook.com/{API_VERSION}"
    API_MODE = "facebook-login"
else:
    API_ROOT = f"https://graph.instagram.com/{API_VERSION}"
    API_MODE = "instagram-login"
RETRY_MINUTES = 15


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOS_ANGELES)


def local_today() -> str:
    return now_local().date().isoformat()


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, value: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def enabled() -> bool:
    return bool(os.environ.get("RERUN_INSTAGRAM_ACCESS_TOKEN") and os.environ.get("RERUN_PUBLIC_BASE_URL"))


def load_catalog() -> list[dict]:
    snapshot = read_json(DATA_DIR / "snapshot.json", {})
    if isinstance(snapshot, dict) and isinstance(snapshot.get("screenings"), list):
        return snapshot["screenings"]
    return read_json(DATA_DIR / "screenings.json", [])


def parse_start(item: dict) -> datetime | None:
    value = str(item.get("startAt") or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def selected_screenings(items: list[dict]) -> list[dict]:
    today = local_today()
    future = []
    for item in items:
        if item.get("city") != "los-angeles" or str(item.get("startAt") or "")[:10] != today:
            continue
        if item.get("sourceStatus") == "partner-fallback":
            continue
        start = parse_start(item)
        if start and start < datetime.now(timezone.utc):
            continue
        future.append(item)

    preferred_ids = [value.strip() for value in os.environ.get("RERUN_INSTAGRAM_FEATURED_IDS", "").split(",") if value.strip()]
    by_id = {str(item.get("id")): item for item in future}
    chosen = [by_id[item_id] for item_id in preferred_ids if item_id in by_id]
    seen_venues = {str(item.get("venue") or "").strip().lower() for item in chosen}
    for item in sorted(future, key=lambda entry: parse_start(entry) or datetime.max.replace(tzinfo=timezone.utc)):
        venue = str(item.get("venue") or "").strip().lower()
        if venue in seen_venues:
            continue
        chosen.append(item)
        seen_venues.add(venue)
        if len(chosen) >= max(1, int(os.environ.get("RERUN_INSTAGRAM_SELECTION_LIMIT", "3"))):
            break
    return chosen[:max(1, int(os.environ.get("RERUN_INSTAGRAM_SELECTION_LIMIT", "3")))]


def public_image_url(item: dict) -> str:
    base = os.environ["RERUN_PUBLIC_BASE_URL"].rstrip("/")
    return f"{base}/api/image?url={quote(str(item.get('image') or ''), safe='')}"


def caption_for(items: list[dict]) -> str:
    noun = "screening" if len(items) == 1 else "screenings"
    lines = ["TODAY IN LA 🎞️", "", f"{len(items)} {noun} worth leaving the house for:"]
    for item in items:
        lines.append(f"• {item.get('title', 'Untitled')} — {item.get('time', 'Time TBA')} · {item.get('venue', 'Venue TBA')}")
    base = os.environ["RERUN_PUBLIC_BASE_URL"].rstrip("/")
    lines.extend(["", f"Find more verified repertory screenings at {base}", "", "#Rerun #LosAngelesMovies #RepertoryCinema"])
    return "\n".join(lines)


def api_request(path: str, values: dict) -> dict:
    body = urlencode(values).encode("utf-8")
    request = Request(f"{API_ROOT}/{path.lstrip('/')}", data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"Instagram API request failed: {format_http_error(error)}") from error
    except Exception as error:
        raise RuntimeError(f"Instagram API request failed: {error}") from error


def format_http_error(error: HTTPError) -> str:
    """Preserve Meta's useful error message without exposing the access token."""

    try:
        raw = error.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        details = payload.get("error")
        if isinstance(details, dict):
            message = details.get("message") or details.get("error_user_msg")
            code = details.get("code")
            if message:
                return f"HTTP {error.code}: {message}" + (f" (code {code})" if code else "")
    return f"HTTP {error.code}: {raw[:500] or error.reason}"


def instagram_user_id(token: str) -> str:
    configured = os.environ.get("RERUN_INSTAGRAM_USER_ID")
    if configured:
        return configured
    if API_MODE == "facebook-login":
        raise RuntimeError(
            "RERUN_INSTAGRAM_USER_ID is required for facebook-login mode; "
            "use the Instagram professional account ID."
        )
    query = urlencode({"fields": "user_id,username", "access_token": token})
    request = Request(f"{API_ROOT}/me?{query}", method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(
            f"Could not resolve the Instagram account from the token: {format_http_error(error)}"
        ) from error
    except Exception as error:
        raise RuntimeError(f"Could not resolve the Instagram account from the token: {error}") from error
    if not payload.get("user_id") and not payload.get("id"):
        raise RuntimeError(f"Instagram account lookup returned no id: {payload}")
    return str(payload.get("user_id") or payload["id"])


def publish_today(force: bool = False) -> dict:
    if not enabled():
        return {"status": "disabled", "reason": "Instagram secrets are not configured"}

    today = local_today()
    state = read_json(STATE_PATH, {})
    if state.get("date") == today and state.get("status") in {"posted", "skipped"}:
        return state
    retry_at = state.get("retryAt")
    if not force and state.get("date") == today and retry_at:
        try:
            if datetime.fromisoformat(retry_at.replace("Z", "+00:00")) > datetime.now(timezone.utc):
                return state
        except ValueError:
            pass

    items = selected_screenings(load_catalog())
    if not items:
        state = {"date": today, "status": "skipped", "reason": "No verified future screenings today"}
        write_json(STATE_PATH, state)
        return state
    if not any(item.get("image") for item in items):
        state = {"date": today, "status": "skipped", "reason": "Selected screenings have no publishable artwork"}
        write_json(STATE_PATH, state)
        return state

    token = os.environ["RERUN_INSTAGRAM_ACCESS_TOKEN"]
    try:
        user_id = instagram_user_id(token)
        cover = next(item for item in items if item.get("image"))
        container = api_request(f"{user_id}/media", {
            "image_url": public_image_url(cover),
            "caption": caption_for(items),
            "access_token": token,
        })
        creation_id = container.get("id")
        if not creation_id:
            raise RuntimeError(f"Instagram media container returned no id: {container}")
        published = api_request(f"{user_id}/media_publish", {
            "creation_id": creation_id,
            "access_token": token,
        })
        post_id = published.get("id")
        if not post_id:
            raise RuntimeError(f"Instagram publish returned no id: {published}")
        state = {"date": today, "status": "posted", "mediaId": post_id, "screeningIds": [item.get("id") for item in items], "postedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        write_json(STATE_PATH, state)
        return state
    except Exception as error:
        retry = datetime.now(timezone.utc) + timedelta(minutes=RETRY_MINUTES)
        state = {"date": today, "status": "error", "error": str(error), "retryAt": retry.isoformat().replace("+00:00", "Z")}
        write_json(STATE_PATH, state)
        return state


def seconds_until_next_run() -> int:
    now = now_local()
    state = read_json(STATE_PATH, {})
    if state.get("date") == now.date().isoformat() and state.get("status") == "error" and state.get("retryAt"):
        try:
            remaining = int((datetime.fromisoformat(state["retryAt"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
            if remaining > 0:
                return remaining
        except ValueError:
            pass
    if state.get("date") == now.date().isoformat() and state.get("status") in {"posted", "skipped"}:
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target:
            return 1
    return max(1, int((target - now).total_seconds()))


if __name__ == "__main__":
    print(json.dumps(publish_today(force="--force" in sys.argv[1:]), indent=2, ensure_ascii=False))
