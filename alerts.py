#!/usr/bin/env python3
"""Small SQLite-backed alert subscription store shared by both servers."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RERUN_DATA_DIR") or ROOT / "data").expanduser().resolve()
DB_PATH = DATA_DIR / "rerun.sqlite3"
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def initialize() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                email TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'rerun-web'
            )
            """
        )
        # One-time migration for the prototype's JSON store. It is intentionally
        # idempotent so a restart cannot duplicate or lose prior signups.
        legacy_path = DATA_DIR / "subscribers.json"
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            legacy = []
        for item in legacy if isinstance(legacy, list) else []:
            email = str(item.get("email", "")).strip().lower()
            if not EMAIL_PATTERN.fullmatch(email):
                continue
            connection.execute(
                "INSERT OR IGNORE INTO subscribers(email, created_at, source) VALUES (?, ?, ?)",
                (email, item.get("createdAt") or now_iso(), item.get("source") or "legacy-json"),
            )


def add_subscriber(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid email address.")
    initialize()
    with sqlite3.connect(DB_PATH, timeout=10) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO subscribers(email, created_at, source) VALUES (?, ?, ?)",
            (normalized, now_iso(), "rerun-web"),
        )
        return "created" if cursor.rowcount else "duplicate"


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "add":
        raise SystemExit("usage: alerts.py add EMAIL")
    try:
        result = add_subscriber(sys.argv[2])
        print(json.dumps({"status": result}))
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
