#!/usr/bin/env python3
"""Build the static Cloudflare Pages site into ``public/``.

Runs one official-source refresh, then writes the exact ``/api/screenings``
payload the Python server would return as ``public/api/screenings.json`` and
mirrors every poster to ``public/img/<key>.jpg``. Mirroring means tiles never
depend on a venue CDN, hotlink rules, or certificate chains at view time.

    python build_static.py                 # refresh + build
    python build_static.py --skip-refresh  # rebuild from the current data dir

Exits non-zero (and the workflow skips deploying) when there is no verified
catalog to publish, so a venue outage never replaces the live site.
"""

import hashlib
import io
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import refresh
import server

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "public"
IMAGE_CACHE = server.DATA_DIR / "image-cache"
DEPLOY_STATE = server.DATA_DIR / "last-deploy.json"
STATIC_FILES = [
    "index.html", "calendar.html", "app.js", "styles.css", "service-worker.js", "manifest.json",
    "favicon.svg", "app-icon.svg", "robots.txt", "sitemap.xml", "_redirects", "_headers", "assets",
]
POSTER_MAX = (720, 1080)
# The server's 4 MB proxy cap protects a live process; the offline build
# downscales everything, so it can accept full-size venue originals.
server.IMAGE_MAX_BYTES = 20 * 1024 * 1024
# Redeploy at least this often even when listings are unchanged, so the
# "verified … ago" status on the site never drifts far from the real check.
MAX_DEPLOY_AGE = timedelta(hours=3)


def image_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def mirror_image(url: str) -> str:
    """Return the site path for a mirrored poster, or "" if it can't be fetched."""

    key = image_key(url)
    cached = IMAGE_CACHE / f"{key}.jpg"
    if not cached.exists():
        from PIL import Image  # only the build needs Pillow

        try:
            _, body = server.fetch_remote_image(url)  # https + allow-list + size cap
            with Image.open(io.BytesIO(body)) as picture:
                picture = picture.convert("RGB")
                picture.thumbnail(POSTER_MAX)
                IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
                picture.save(cached, "JPEG", quality=82, optimize=True, progressive=True)
        except Exception as error:  # one bad poster must not fail the build
            print(f"[image] skipped {url}: {error}", flush=True)
            return ""
    target = OUT / "img" / cached.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, target)
    return f"/img/{cached.name}"


def build_payload() -> dict | None:
    catalog = server.read_catalog()
    meta = catalog.get("meta") or {}
    sources = catalog.get("sources", [])
    complete = server.feed_is_complete(meta, sources)
    serving_last_verified = not complete and server.has_last_verified_snapshot(catalog)
    if not complete and not serving_last_verified:
        return None
    now = datetime.now(timezone.utc)
    data = sorted(
        (
            item for item in catalog.get("screenings", [])
            if server.normalized(item.get("city")) == "los-angeles" and server.iso_sort_value(item) >= now
        ),
        key=server.iso_sort_value,
    )
    return {
        "data": data,
        "meta": server.feed_meta("los-angeles", data, catalog.get("venues", []), sources, meta, serving_last_verified),
    }


def write_site(payload: dict) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    for name in STATIC_FILES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, OUT / name)
        elif source.exists():
            shutil.copy2(source, OUT / name)

    mirrored = {}
    for item in payload["data"]:
        original = item.get("image") or ""
        if original and original not in mirrored:
            mirrored[original] = mirror_image(original)
        item["image"] = mirrored.get(original, "")
        if not item["image"]:
            item.pop("imageAlt", None)
    used = set(path.rsplit("/", 1)[-1] for path in mirrored.values() if path)
    for stale in IMAGE_CACHE.glob("*.jpg") if IMAGE_CACHE.exists() else []:
        if stale.name not in used:
            stale.unlink()

    (OUT / "api").mkdir()
    (OUT / "api" / "screenings.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # Self-check: every tile either has a local poster or the designed fallback.
    for item in payload["data"]:
        image = item["image"]
        assert image == "" or (image.startswith("/img/") and (OUT / image.lstrip("/")).is_file()), image
    found = sum(1 for item in payload["data"] if item["image"])
    print(f"[build] {len(payload['data'])} screenings, {found} with posters, {len(used)} unique images", flush=True)


def deploy_needed(payload: dict) -> bool:
    """Compare listings (not timestamps) with the last successful deploy.

    The new state goes to pending-deploy.json; the workflow renames it to
    last-deploy.json only after Cloudflare accepts the upload.
    """

    digest = hashlib.sha1(json.dumps(payload["data"], sort_keys=True).encode("utf-8")).hexdigest()
    previous = server.read_json(DEPLOY_STATE.name, {})
    now = datetime.now(timezone.utc)
    try:
        age = now - datetime.fromisoformat(previous.get("at", "").replace("Z", "+00:00"))
    except ValueError:
        age = MAX_DEPLOY_AGE
    needed = previous.get("hash") != digest or age >= MAX_DEPLOY_AGE
    if needed:
        server.write_json("pending-deploy.json", {"hash": digest, "at": now.isoformat()})
    return needed


def main() -> int:
    if "--skip-refresh" not in sys.argv:
        try:
            refresh.refresh_los_angeles(force=True)
        except Exception as error:  # keep the last verified catalog on a crash
            print(f"[refresh] failed: {error}", flush=True)
    payload = build_payload()
    if payload is None:
        print("[build] no verified catalog yet; leaving the live site as-is", flush=True)
        return 1
    write_site(payload)
    needed = deploy_needed(payload)
    print(f"[build] deploy needed: {needed}", flush=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"deploy={'true' if needed else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
