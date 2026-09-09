#!/usr/bin/env python3
"""Single-instance scheduled refresh worker for hosted and local deployments."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import refresh


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
POLL_SECONDS = max(15, int(os.environ.get("RERUN_WORKER_POLL_SECONDS", "60")))
STATE_PATH = DATA_DIR / "worker-state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = STATE_PATH
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(target)


def wait_seconds(meta: dict) -> int:
    retry_at = refresh.parse_timestamp(meta.get("nextRetryAt"))
    if retry_at:
        remaining = int((retry_at - refresh.utc_now()).total_seconds())
        if remaining > 0:
            return min(900, max(POLL_SECONDS, remaining))
    checked = refresh.parse_timestamp(meta.get("lastRefresh") or meta.get("checkedAt"))
    if checked:
        remaining = int(refresh.REFRESH_TTL_SECONDS - (refresh.utc_now() - checked).total_seconds())
        if remaining > 0:
            return min(900, max(POLL_SECONDS, remaining))
    return POLL_SECONDS


def run_worker(stop_event=None) -> None:
    lock_path = DATA_DIR / ".worker.lock"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError):
            if stop_event is None:
                return
        state = {"status": "idle", "pid": os.getpid(), "startedAt": now_iso()}
        write_state(state)
        while stop_event is None or not stop_event.is_set():
            meta = refresh.load_refresh_meta()
            if refresh.refresh_is_stale(meta):
                started = now_iso()
                write_state({**state, "status": "refreshing", "lastStartedAt": started})
                try:
                    result = refresh.refresh_los_angeles(force=False)
                    meta = result.get("meta", refresh.load_refresh_meta())
                    state = {
                        **state,
                        "status": "idle" if meta.get("status") == "ok" else "degraded",
                        "lastStartedAt": started,
                        "lastFinishedAt": now_iso(),
                        "lastStatus": meta.get("status"),
                        "lastError": None,
                    }
                except Exception as error:  # keep the worker alive after a bad source
                    meta = refresh.load_refresh_meta()
                    state = {
                        **state,
                        "status": "error",
                        "lastStartedAt": started,
                        "lastFinishedAt": now_iso(),
                        "lastStatus": "error",
                        "lastError": str(error),
                    }
                state["nextRunAt"] = (refresh.utc_now().timestamp() + wait_seconds(meta))
                state["nextRunAt"] = datetime.fromtimestamp(state["nextRunAt"], timezone.utc).isoformat().replace("+00:00", "Z")
                write_state(state)
            delay = wait_seconds(meta)
            if stop_event is not None:
                stop_event.wait(delay)
            else:
                time.sleep(delay)


if __name__ == "__main__":
    run_worker()
