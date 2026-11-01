"""Draft this week's Rerun email in Buttondown, from the live listings.

    python weekly_email.py           # print the issue
    python weekly_email.py --draft   # also save it as a Buttondown draft (needs BUTTONDOWN_API_KEY)

It only ever creates a draft: you read it, edit it, and press send in Buttondown.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SITE = "https://1rerun.com"
LA = ZoneInfo("America/Los_Angeles")
ORANGE_COUNTY = {"The Frida Cinema", "Lido Theater"}  # the email covers LA, as the site does by default
SPECIAL_FORMAT = re.compile(r"70mm|35mm|16mm|nitrate", re.I)


def film_key(title):
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\(.*?\)", " ", title.lower())).strip()


def picks(screenings, start):
    """One pick for each of the seven days from start, never the same film twice. Film prints come first."""
    days = {}
    for s in screenings:
        if s.get("venue") in ORANGE_COUNTY or s.get("sourceStatus") != "verified" or not s.get("startAt"):
            continue
        when = datetime.fromisoformat(s["startAt"]).astimezone(LA)
        if start <= when.date() < start + timedelta(days=7):
            days.setdefault(when.date(), []).append((when, s))
    chosen, seen = [], set()
    for day in sorted(days):
        # shortcut: favors film prints only, upgrade when there's a real measure of what people click
        ranked = sorted(days[day], key=lambda w: (not SPECIAL_FORMAT.search(w[1].get("format") or ""), w[0]))
        pick = next((w for w in ranked if film_key(w[1]["title"]) not in seen), None)
        if pick:
            seen.add(film_key(pick[1]["title"]))
            chosen.append(pick)
    return chosen


def issue(screenings, start):
    """(subject, Markdown body) for the seven days from start."""
    chosen = picks(screenings, start)
    if not chosen:
        return "This week in LA repertory", f"[See what's playing]({SITE}/?src=email)"
    lines = ["One screening a day, chosen from LA's repertory calendars. Check the theater's page before you go.", ""]
    for when, s in chosen:
        fmt = (s.get("format") or "").strip()
        clock = f"{when.hour % 12 or 12}" + (f":{when:%M}" if when.minute else "") + f" {when:%p}"
        detail = f"{s['venue']}, {clock}" + (f", {fmt}" if fmt and fmt != "—" else "")
        lines += [f"## {when:%A}", f"**[{s['title']}]({SITE}/?screening={s['id']}&src=email)**", detail, ""]
    lines.append(f"[Everything playing this week]({SITE}/?src=email)")
    first = chosen[0][1]["title"]
    rest = len(chosen) - 1
    subject = f"This week in LA repertory: {first}" + (f" and {rest} more" if rest else "")
    return subject, "\n".join(lines)


def main():
    req = urllib.request.Request(f"{SITE}/api/screenings.json", headers={"User-Agent": "Rerun-weekly/1.0 (+https://1rerun.com)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        screenings = json.load(r)["data"]
    subject, body = issue(screenings, datetime.now(LA).date() + timedelta(days=1))
    print(subject, "\n", body, sep="\n")
    if "--draft" in sys.argv:
        req = urllib.request.Request(
            "https://api.buttondown.com/v1/emails", method="POST",
            data=json.dumps({"subject": subject, "body": body, "status": "draft"}).encode(),
            headers={"Authorization": f"Token {os.environ['BUTTONDOWN_API_KEY']}", "Content-Type": "application/json",
                     "X-API-Version": "2026-04-01"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print("Buttondown draft created:", json.load(r).get("id"))


if __name__ == "__main__":
    main()
