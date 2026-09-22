#!/usr/bin/env python3
"""Small, respectful official-source adapters for Rerun's Los Angeles feed.

The adapters intentionally use only the Python standard library.  They fetch
public event pages, cache responses, normalize showtimes into a common shape,
and retain the source URL on every screening so a user can verify the listing.
"""

from __future__ import annotations

import hashlib
import gzip
from html import unescape
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from concurrent.futures import ThreadPoolExecutor, as_completed


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RERUN_DATA_DIR") or ROOT / "data").expanduser().resolve()
CACHE_DIR = DATA_DIR / "cache"


class PacificTimeZone(tzinfo):
    """Dependency-free fallback for Windows Python installs without tzdata.

    ``zoneinfo.ZoneInfo`` reads the IANA timezone database.  Some Windows
    Python distributions do not ship that database (or the optional ``tzdata``
    package), which used to make the server fail during import.  Rerun only
    needs the current US Pacific rules, so this small fallback covers the
    modern US daylight-saving transitions: the second Sunday in March through
    the first Sunday in November.
    """

    @staticmethod
    def _transition(year: int, month: int, weekday: int, occurrence: int) -> datetime:
        first = datetime(year, month, 1)
        days_to_weekday = (weekday - first.weekday()) % 7
        return first + timedelta(days=days_to_weekday + (occurrence - 1) * 7)

    @classmethod
    def _dst_bounds_local(cls, year: int) -> tuple[datetime, datetime]:
        # US rules since 2007: 02:00 local time on the second Sunday in March
        # until 02:00 local time on the first Sunday in November.
        start = cls._transition(year, 3, 6, 2).replace(hour=2)
        end = cls._transition(year, 11, 6, 1).replace(hour=2)
        return start, end

    @classmethod
    def _is_dst_local(cls, value: datetime | None) -> bool:
        if value is None:
            return False
        naive = value.replace(tzinfo=None)
        start, end = cls._dst_bounds_local(naive.year)
        # ``fold`` disambiguates the repeated 01:00 hour when daylight time
        # ends (and mirrors ZoneInfo's handling of the spring-forward gap).
        if end - timedelta(hours=1) <= naive < end:
            return value.fold == 0
        if start <= naive < start + timedelta(hours=1):
            return value.fold == 1
        return start <= naive < end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=-7 if self._is_dst_local(dt) else -8)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1) if self._is_dst_local(dt) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "PDT" if self._is_dst_local(dt) else "PST"

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")

        naive_utc = dt.replace(tzinfo=None)
        start_local, end_local = self._dst_bounds_local(naive_utc.year)
        # The spring transition occurs while standard time is in effect; the
        # fall transition occurs while daylight time is in effect.
        start_utc = start_local + timedelta(hours=8)
        end_utc = end_local + timedelta(hours=7)
        offset = timedelta(hours=-7) if start_utc <= naive_utc < end_utc else timedelta(hours=-8)
        local_naive = naive_utc + offset
        repeated_hour = offset == timedelta(hours=-8) and end_local - timedelta(hours=1) <= local_naive < end_local
        return local_naive.replace(tzinfo=self, fold=1 if repeated_hour else 0)


try:
    LA_TZ = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError:
    # Keep the app runnable on a fresh Windows Python installation.  A real
    # ZoneInfo instance is still preferred whenever the system has tzdata.
    LA_TZ = PacificTimeZone()
USER_AGENT = os.environ.get(
    "RERUN_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
DEFAULT_CACHE_TTL = int(os.environ.get("RERUN_CACHE_TTL_SECONDS", "1800"))
DEFAULT_TIMEOUT = float(os.environ.get("RERUN_HTTP_TIMEOUT", "4"))
VEEZI_TIMEOUT = float(os.environ.get("RERUN_VEEZI_TIMEOUT", "30"))
DEFAULT_MAX_AC_EVENTS = int(os.environ.get("RERUN_MAX_AC_EVENTS", "500"))
DEFAULT_MAX_DETAIL_EVENTS = int(os.environ.get("RERUN_MAX_DETAIL_EVENTS", "60"))
DEFAULT_REQUEST_GAP = float(os.environ.get("RERUN_REQUEST_GAP_SECONDS", "0.08"))
DEFAULT_HTTP_RETRIES = int(os.environ.get("RERUN_HTTP_RETRIES", "1"))
DEFAULT_RETRY_MAX_SECONDS = float(os.environ.get("RERUN_RETRY_MAX_SECONDS", "4"))

TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

AC_NOW_SHOWING_URL = "https://www.americancinematheque.com/now-showing/"
AC_HOME_URL = "https://www.americancinematheque.com/"
NEW_BEV_SCHEDULE_URL = "https://thenewbev.com/schedule/"
NEW_BEV_SESSIONS_URL = "https://ticketing.uswest.veezi.com/sessions/?siteToken=fmtswb0qqbym3de6c4bbsqj89m"
VISTA_HOME_URL = "https://www.vistatheaterhollywood.com/"
VISTA_SCHEDULE_URL = "https://www.vistatheaterhollywood.com/schedule/"
VISTA_SESSIONS_URL = "https://ticketing.uswest.veezi.com/sessions/?siteToken=20xhpa3yt2hhkwt4zjvfcwsaww"
VIDIOTS_HOME_URL = "https://vidiotsfoundation.org/"
VIDIOTS_URL = "https://vidiotsfoundation.org/coming-soon/"
VIDIOTS_PARTNER_URL = "https://la.teentix.org/venues/vidiots/"
ACADEMY_CALENDAR_URL = "https://www.academymuseum.org/en/calendar?programTypes=16i3uOYQwism7sMDhIQr2O"
ACADEMY_FILMS_URL = "https://www.academymuseum.org/en/films?qr=theater"
ACADEMY_HOSTS = {"academymuseum.org", "www.academymuseum.org", "v2.academymuseum.org"}
ACADEMY_SCREENING_TYPE_ID = "16i3uOYQwism7sMDhIQr2O"
ACADEMY_DETAIL_PATH_PREFIXES = ("/programs/detail/", "/en/programs/detail/")
ACADEMY_SITEMAP_URLS = (
    "https://www.academymuseum.org/sitemap.xml",
    "https://www.academymuseum.org/sitemap_index.xml",
    "https://www.academymuseum.org/en/sitemap.xml",
)
FINE_ARTS_SESSIONS_URL = "https://ticketing.uswest.veezi.com/sessions/?siteToken=tez3prscsvfbagchhkxbevjwk8"
FILMFORUM_SCHEDULE_URL = "https://www.lafilmforum.org/schedule/"
OLD_TOWN_HOME_URL = "https://www.oldtownmusichall.org/home/"
UCLA_EVENTS_URL = "https://www.cinema.ucla.edu/events/"
UCLA_SERIES_URL = "https://www.cinema.ucla.edu/series/"
UCLA_SITEMAP_URL = "https://www.cinema.ucla.edu/sitemap.xml"
UCLA_SITEMAP_URLS = (
    UCLA_SITEMAP_URL,
    "https://cinema.ucla.edu/sitemap.xml",
    "https://www.cinema.ucla.edu/sitemap_index.xml",
    "https://www.cinema.ucla.edu/wp-sitemap.xml",
)

SOURCE_REGISTRY = {
    "american-cinematheque": {
        "name": "American Cinematheque",
        "city": "los-angeles",
        "url": AC_NOW_SHOWING_URL,
        "adapter": "american_cinematheque",
    },
    "new-beverly": {
        "name": "New Beverly Cinema",
        "city": "los-angeles",
        "url": NEW_BEV_SESSIONS_URL,
        "scheduleUrl": NEW_BEV_SCHEDULE_URL,
        "sessionsUrl": NEW_BEV_SESSIONS_URL,
        "adapter": "new_beverly",
        "required": False,
    },
    "vista-theater": {
        "name": "Vista Theater Hollywood",
        "city": "los-angeles",
        "url": VISTA_HOME_URL,
        "scheduleUrl": VISTA_SCHEDULE_URL,
        "sessionsUrl": VISTA_SESSIONS_URL,
        "adapter": "vista_theater",
    },
    "vidiots": {
        "name": "Vidiots",
        "city": "los-angeles",
        "url": VIDIOTS_HOME_URL,
        "scheduleUrl": VIDIOTS_URL,
        "partnerUrl": VIDIOTS_PARTNER_URL,
        "adapter": "vidiots",
        "required": False,
    },
    "academy-museum": {
        "name": "Academy Museum of Motion Pictures",
        "city": "los-angeles",
        "url": ACADEMY_CALENDAR_URL,
        "adapter": "academy_museum",
        "required": False,
    },
    "fine-arts-theatre": {
        "name": "Fine Arts Theatre Beverly Hills",
        "city": "los-angeles",
        "url": FINE_ARTS_SESSIONS_URL,
        "adapter": "fine_arts_theatre",
        "required": False,
    },
    "filmforum": {
        "name": "Los Angeles Filmforum",
        "city": "los-angeles",
        "url": FILMFORUM_SCHEDULE_URL,
        "adapter": "filmforum",
        "required": False,
    },
    "old-town-music-hall": {
        "name": "Old Town Music Hall",
        "city": "los-angeles",
        "url": OLD_TOWN_HOME_URL,
        "adapter": "old_town_music_hall",
        "required": False,
    },
    "ucla-film-archive": {
        "name": "UCLA Film & Television Archive",
        "city": "los-angeles",
        "url": UCLA_EVENTS_URL,
        "adapter": "ucla_film_archive",
        "required": False,
    },
}

# The homepage normally exposes these links.  Seeds keep the feed useful when
# a venue switches to client-side rendering or temporarily changes its markup.
AC_SEED_URLS = [
    "https://www.americancinematheque.com/now-showing/cinematic-void-presents-assault-of-the-killer-bimbos-09-07-2026/",
    "https://www.americancinematheque.com/now-showing/pink-flamingos-09-07-2026/",
    "https://www.americancinematheque.com/now-showing/straw-dogs-09-08-2026/",
    "https://www.americancinematheque.com/now-showing/the-best-little-whorehouse-in-texas-09-10-2026/",
    "https://www.americancinematheque.com/now-showing/straw-dogs-09-10-2026/",
    "https://www.americancinematheque.com/now-showing/crash-09-11-2026/",
    "https://www.americancinematheque.com/now-showing/sleeping-dogs-09-12-2026/",
    "https://www.americancinematheque.com/now-showing/mirage-strait-jacket-09-12-2026/",
    "https://www.americancinematheque.com/now-showing/los-angeles-silent-film-festival-presents-first-national-family-of-stars-for-heavens-sake-09-13-2026/",
    "https://www.americancinematheque.com/now-showing/fantastic-planet-the-time-masters-gandahar-09-13-2026/",
    "https://www.americancinematheque.com/now-showing/silent-movie-09-13-2026/",
    "https://www.americancinematheque.com/now-showing/the-sheriffs-baby-so-this-is-paris-09-13-2026/",
]

VENUE_CATALOG = {
    "los feliz 3": {
        "name": "Los Feliz 3",
        "neighborhood": "Los Feliz",
        "address": "1822 N Vermont Ave, Los Angeles, CA 90027",
    },
    "los feliz theatre": {
        "name": "Los Feliz 3",
        "neighborhood": "Los Feliz",
        "address": "1822 N Vermont Ave, Los Angeles, CA 90027",
    },
    "aero theatre": {
        "name": "Aero Theatre",
        "neighborhood": "Santa Monica",
        "address": "1328 Montana Ave, Santa Monica, CA 90403",
    },
    "egyptian theatre": {
        "name": "Egyptian Theatre",
        "neighborhood": "Hollywood",
        "address": "6712 Hollywood Blvd, Los Angeles, CA 90028",
    },
    "new beverly": {
        "name": "New Beverly Cinema",
        "neighborhood": "Fairfax",
        "address": "7165 Beverly Blvd, Los Angeles, CA 90036",
    },
    "new beverly cinema": {
        "name": "New Beverly Cinema",
        "neighborhood": "Fairfax",
        "address": "7165 Beverly Blvd, Los Angeles, CA 90036",
    },
    "vista theater hollywood": {
        "name": "Vista Theater Hollywood",
        "neighborhood": "East Hollywood",
        "address": "4473 Sunset Drive, Los Angeles, CA 90027",
    },
    "vista theater": {
        "name": "Vista Theater Hollywood",
        "neighborhood": "East Hollywood",
        "address": "4473 Sunset Drive, Los Angeles, CA 90027",
    },
    "vista theatre hollywood": {
        "name": "Vista Theater Hollywood",
        "neighborhood": "East Hollywood",
        "address": "4473 Sunset Drive, Los Angeles, CA 90027",
    },
    "vista theatre": {
        "name": "Vista Theater Hollywood",
        "neighborhood": "East Hollywood",
        "address": "4473 Sunset Drive, Los Angeles, CA 90027",
    },
    "vidiots": {
        "name": "Vidiots",
        "neighborhood": "Eagle Rock",
        "address": "4884 Eagle Rock Blvd, Los Angeles, CA 90041",
    },
    "academy museum": {
        "name": "Academy Museum of Motion Pictures",
        "neighborhood": "Mid-Wilshire",
        "address": "6067 Wilshire Boulevard, Los Angeles, CA 90036",
    },
    "fine arts theatre": {
        "name": "Fine Arts Theatre Beverly Hills",
        "neighborhood": "Beverly Hills",
        "address": "8556 Wilshire Boulevard, Beverly Hills, CA 90211",
    },
    "fine arts theater": {
        "name": "Fine Arts Theatre Beverly Hills",
        "neighborhood": "Beverly Hills",
        "address": "8556 Wilshire Boulevard, Beverly Hills, CA 90211",
    },
    "2220 arts + archives": {
        "name": "2220 Arts + Archives",
        "neighborhood": "Historic Filipinotown",
        "address": "2220 Beverly Boulevard, Los Angeles, CA 90057",
    },
    "billy wilder theater": {
        "name": "Billy Wilder Theater",
        "neighborhood": "Westwood",
        "address": "10899 Wilshire Boulevard, Los Angeles, CA 90024",
    },
    "old town music hall": {
        "name": "Old Town Music Hall",
        "neighborhood": "El Segundo",
        "address": "140 Richmond Street, El Segundo, CA 90245",
    },
}

COLOR_PALETTE = [
    "#a7462d",
    "#9a743e",
    "#53625b",
    "#7f3f45",
    "#38536c",
    "#b17143",
    "#6d4a68",
    "#8c6a37",
]

MONTHS = {
    month.lower(): index
    for index, month in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
MONTHS.update({key[:3]: value for key, value in list(MONTHS.items())})
DAY_NAMES = "Mon|Tue|Wed|Thu|Fri|Sat|Sun"
MONTH_PATTERN = "January|February|March|April|May|June|July|August|September|October|November|December"
MONTH_PATTERN_SHORT = "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap]\.?m\.?)(?![a-z])", re.I)
AC_DATE_RE = re.compile(
    rf"\b(?:{DAY_NAMES})\.?\s+(?P<month>{MONTH_PATTERN_SHORT})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>20\d{{2}})\s+(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*(?P<meridiem>[ap]\.?m\.?)\b",
    re.I,
)
AC_DAY_DATE_RE = re.compile(
    rf"\b(?:{DAY_NAMES})\.?\s+(?P<month>{MONTH_PATTERN_SHORT})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>20\d{{2}})\b",
    re.I,
)
AC_EVENT_URL_DATE_RE = re.compile(r"-(?P<month>\d{1,2})-(?P<day>\d{1,2})-(?P<year>20\d{2}|\d{2})/?$", re.I)
AC_NON_SCREENING_TITLES = frozenset(
    {
        "about",
        "about the series",
        "about the festival",
        "about the film",
        "about the films",
        "calendar",
        "contact",
        "directions",
        "donate",
        "film programs",
        "get tickets",
        "monthly programming",
        "membership",
        "become a member",
        "locations",
        "now showing",
        "program",
        "programs",
        "related events",
        "related screenings",
        "subscribe for ac updates",
        "tickets",
    }
)
GENERIC_SCREENING_TITLES = frozenset(
    {
        "buy tickets",
        "coming soon",
        "details",
        "double feature",
        "more info",
        "movie listings",
        "movies",
        "read more",
        "show times",
        "showtimes",
        "special event",
        "upcoming movies",
        "upcoming screenings",
    }
)
NEW_BEV_DATE_RE = re.compile(
    rf"\b(?:{DAY_NAMES})\.?\s*,?\s+(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:,?\s*(?P<year>20\d{{2}}))?",
    re.I,
)


def normalize_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", normalize_space(value)).casefold()
    return text.replace("½", "1/2").replace("’", "'")


def screening_title_key(value: object) -> str:
    """Normalize title punctuation so duplicate source cards share one key."""

    text = unicodedata.normalize("NFKD", normalize_space(value)).encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def slugify(value: object) -> str:
    text = unicodedata.normalize("NFKD", normalize_space(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "screening"


def as_la(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=LA_TZ)
    return value.astimezone(LA_TZ)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def display_date(value: datetime, now: datetime) -> str:
    local = as_la(value)
    current = as_la(now)
    if local.date() == current.date():
        return "Tonight"
    return f"{local.strftime('%a, %b')} {local.day}"


def display_time(value: datetime) -> str:
    local = as_la(value)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def date_group(value: datetime, now: datetime) -> str:
    local = as_la(value)
    current = as_la(now)
    if local.date() == current.date():
        return "tonight"
    if local.weekday() >= 5:
        return "weekend"
    return ""


def color_for(title: object, source: object = "") -> str:
    digest = hashlib.sha1(f"{source}:{title}".encode("utf-8")).digest()
    return COLOR_PALETTE[digest[0] % len(COLOR_PALETTE)]


def venue_info(value: object, fallback: str = "Los Angeles") -> dict:
    text = normalize_space(value)
    key = normalize_key(text)
    for known, info in VENUE_CATALOG.items():
        if known in key:
            return dict(info)
    return {
        "name": text or fallback,
        "neighborhood": fallback,
        "address": "",
    }


class PageParser(HTMLParser):
    """Collect visible text and the few semantic elements used by adapters."""

    BLOCK_TAGS = {
        "address",
        "article",
        "br",
        "div",
        "dd",
        "dt",
        "li",
        "p",
        "section",
        "tr",
        "time",
        "ul",
    }
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._text: list[str] = []
        self._anchor: dict | None = None
        self._heading: dict | None = None
        self.links: list[dict[str, str]] = []
        self.headings: list[dict[str, str]] = []
        self.images: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        if tag in self.BLOCK_TAGS:
            self._text.append("\n")
        if tag in self.HEADING_TAGS:
            self._text.append("\n")
            self._heading = {"tag": tag, "parts": []}
        if tag == "a":
            self._anchor = {"href": attrs_map.get("href", ""), "parts": []}
        if tag in {"img", "source"}:
            # Venue pages commonly lazy-load posters into data-* attributes,
            # or expose responsive artwork only through a <source> srcset.
            image_src = (
                attrs_map.get("src")
                or attrs_map.get("data-src")
                or attrs_map.get("data-lazy-src")
                or attrs_map.get("data-image")
                or attrs_map.get("data-background-image")
            )
            srcset = (
                attrs_map.get("srcset")
                or attrs_map.get("data-srcset")
                or attrs_map.get("data-lazy-srcset")
            )
            if srcset:
                candidate = srcset.split(",", 1)[0].strip().split(" ", 1)[0]
                image_src = image_src or candidate
            style_url = re.search(r"url\((?:['\"])?([^)'\" ]+)", attrs_map.get("style", ""), re.I)
            image_src = image_src or (style_url.group(1) if style_url else "")
            if image_src:
                self.images.append(image_src)
        if tag == "meta" and attrs_map.get("content"):
            key = attrs_map.get("property") or attrs_map.get("name")
            if key:
                self.meta[key.lower()] = attrs_map["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            self.links.append(
                {
                    "href": normalize_space(self._anchor.get("href", "")),
                    "text": normalize_space(" ".join(self._anchor.get("parts", []))),
                }
            )
            self._anchor = None
        if tag in self.HEADING_TAGS and self._heading is not None:
            self.headings.append(
                {
                    "tag": self._heading["tag"],
                    "text": normalize_space(" ".join(self._heading.get("parts", []))),
                }
            )
            self._heading = None
            self._text.append("\n")
        if tag in self.BLOCK_TAGS:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self._text.append(data)
        if self._anchor is not None:
            self._anchor.setdefault("parts", []).append(data)
        if self._heading is not None:
            self._heading.setdefault("parts", []).append(data)

    @property
    def text(self) -> str:
        return "\n".join(line for line in (normalize_space(part) for part in "".join(self._text).splitlines()) if line)

    @property
    def lines(self) -> list[str]:
        return [line for line in (normalize_space(part) for part in "".join(self._text).splitlines()) if line]


class ScriptParser(HTMLParser):
    """Collect script payloads used by JavaScript-rendered calendars."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._active = False
        self._parts: list[str] = []
        self.scripts: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        self._active = True
        self._parts = []
        self._script_type = attrs_map.get("type", "").lower()
        self._script_id = attrs_map.get("id", "")

    def handle_data(self, data: str) -> None:
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._active:
            self.scripts.append({
                "type": self._script_type,
                "id": self._script_id,
                "text": "".join(self._parts),
            })
            self._active = False
            self._parts = []


def parse_scripts(html: str) -> list[dict[str, str]]:
    parser = ScriptParser()
    parser.feed(html)
    parser.close()
    return parser.scripts


def academy_json_documents(html: str) -> list[object]:
    """Read JSON-LD/Next/Contentful payloads without depending on a CMS SDK."""

    documents: list[object] = []
    decoder = json.JSONDecoder()
    for script in parse_scripts(html):
        text = script.get("text", "").strip().lstrip("\ufeff")
        if not text:
            continue
        candidates = [text]
        if not (text.startswith("{") or text.startswith("[")):
            candidates = []
            for match in re.finditer(r"[\[{]", text):
                try:
                    value, _ = decoder.raw_decode(text[match.start() :])
                except json.JSONDecodeError:
                    continue
                candidates.append(json.dumps(value, ensure_ascii=False))
                if len(candidates) >= 12:
                    break
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                documents.append(value)
    return documents


def walk_json(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


ACADEMY_ISO_DATETIME_RE = re.compile(
    r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
ACADEMY_TEXT_DATE_RE = re.compile(
    rf"\b(?:{DAY_NAMES})\.?[,]?\s+(?P<month>{MONTH_PATTERN_SHORT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(?P<year>20\d{{2}})\b",
    re.I,
)
ACADEMY_DATE_ONLY_RE = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN_SHORT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(?P<year>20\d{{2}})\b",
    re.I,
)
ACADEMY_ISO_DATE_ONLY_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")


def parse_academy_iso_datetime(value: object) -> datetime | None:
    text = normalize_space(value)
    if not text or not ACADEMY_ISO_DATETIME_RE.search(text):
        return None
    candidate = ACADEMY_ISO_DATETIME_RE.search(text).group(0)
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=LA_TZ) if parsed.tzinfo is None else parsed.astimezone(LA_TZ)


def parse_academy_text_datetime(value: str) -> datetime | None:
    date_match = ACADEMY_TEXT_DATE_RE.search(value) or ACADEMY_DATE_ONLY_RE.search(value)
    time_match = TIME_RE.search(value)
    if not date_match or not time_match:
        return None
    date_values = date_match.groupdict()
    try:
        hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
        return datetime(
            int(date_values["year"]),
            MONTHS[date_values["month"].lower()[:3]],
            int(date_values["day"]),
            hour,
            minute,
            tzinfo=LA_TZ,
        )
    except (KeyError, TypeError, ValueError):
        return None


def academy_date_parts(value: object) -> tuple[int, int, int] | None:
    text = normalize_space(value)
    match = ACADEMY_DATE_ONLY_RE.search(text)
    if match:
        values = match.groupdict()
        try:
            return int(values["year"]), MONTHS[values["month"].lower()[:3]], int(values["day"])
        except (KeyError, TypeError, ValueError):
            return None
    match = ACADEMY_ISO_DATE_ONLY_RE.search(text)
    if match:
        try:
            parsed = datetime.fromisoformat(match.group(0))
            return parsed.year, parsed.month, parsed.day
        except ValueError:
            return None
    return None


def _academy_compact_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_key(value))


def _academy_record_title_match(record: dict, title: str) -> bool:
    wanted = screening_title_key(title)
    if not wanted:
        return False
    for key, value in record.items():
        if _academy_compact_key(key) in {"title", "name", "eventname", "programtitle", "filmtitle"}:
            if wanted == screening_title_key(value) or wanted in screening_title_key(value):
                return True
    return False


def _academy_record_datetime(record: dict) -> datetime | None:
    """Extract one record's primary start, never an end or related date."""

    values: dict[str, object] = {
        _academy_compact_key(key): value
        for key, value in record.items()
        if isinstance(value, (str, int, float))
    }
    ignored = ("created", "updated", "published", "modified", "end", "finish")

    # Prefer full start datetime fields.  This prevents a record with both
    # startDate and endDate from generating four cross-product combinations.
    full_keys = (
        "startdatetime", "startat", "startsat", "startdate", "eventstart",
        "eventstartdate", "eventstartdatetime", "eventdate", "datetime", "dateandtime",
    )
    for key in full_keys:
        value = values.get(key)
        if value is not None and not any(marker in key for marker in ignored):
            parsed = parse_academy_iso_datetime(value)
            if parsed:
                return parsed

    date_keys = (
        "startdate", "eventdate", "date", "screeningdate", "showdate",
    )
    time_keys = ("starttime", "time", "screeningtime", "showtime")
    date_value = next((values[key] for key in date_keys if key in values), None)
    time_value = next((values[key] for key in time_keys if key in values), None)
    date_parts = academy_date_parts(date_value) if date_value is not None else None
    if date_parts and time_value is not None:
        time_match = TIME_RE.search(normalize_space(time_value)) or re.search(
            r"\b(\d{1,2}):(\d{2})\b", normalize_space(time_value)
        )
        if time_match:
            try:
                if time_match.groupdict():
                    hour, minute = parse_time(
                        time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem")
                    )
                else:
                    hour, minute = int(time_match.group(1)), int(time_match.group(2))
                year, month, day = date_parts
                return datetime(year, month, day, hour, minute, tzinfo=LA_TZ)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
    return None


def _academy_visible_datetime_candidates(parser: PageParser) -> list[datetime]:
    candidates: list[datetime] = []
    for index, line in enumerate(parser.lines):
        parsed = parse_academy_text_datetime(line)
        if parsed:
            candidates.append(parsed)
            continue
        date_parts = academy_date_parts(line)
        if not date_parts:
            continue
        # The Academy often renders the date and time as adjacent text nodes.
        # Keep the window tight so a related event cannot supply this event's
        # time accidentally.
        for nearby in parser.lines[index + 1:index + 4]:
            time_match = TIME_RE.search(nearby) or re.search(r"\b(\d{1,2}):(\d{2})\b", nearby)
            if not time_match:
                continue
            try:
                if time_match.groupdict():
                    hour, minute = parse_time(
                        time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem")
                    )
                else:
                    hour, minute = int(time_match.group(1)), int(time_match.group(2))
                year, month, day = date_parts
                candidates.append(datetime(year, month, day, hour, minute, tzinfo=LA_TZ))
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
            break
    return candidates


def academy_datetime_candidates(
    html: str,
    parser: PageParser,
    now: datetime,
    title: str = "",
) -> list[datetime]:
    """Return dates for the detail page's own event, not its related events."""

    current = as_la(now)
    matched_records: list[datetime] = []
    start_records: list[datetime] = []
    for document in academy_json_documents(html):
        for record in walk_json(document):
            parsed = _academy_record_datetime(record)
            if not parsed:
                continue
            start_records.append(parsed)
            if _academy_record_title_match(record, title):
                matched_records.append(parsed)

    # A record carrying this page's title is the strongest signal. Otherwise,
    # use the first primary start record in document order, which is how the
    # Academy payload presents the current event before related events.
    candidates = matched_records or start_records[:1]
    if not candidates:
        candidates = _academy_visible_datetime_candidates(parser)
    unique: dict[str, datetime] = {}
    for candidate in candidates:
        local = as_la(candidate)
        if local >= current:
            unique[local.isoformat()] = local
    return sorted(unique.values())


def academy_labelled_screening(html: str) -> bool:
    parser = parse_html(html)
    labels = {normalize_key(line).rstrip(":") for line in parser.lines}
    return "screenings" in labels or "screening from series" in labels


def discover_academy_event_urls(html: str, base_url: str = ACADEMY_CALENDAR_URL) -> list[str]:
    """Find Academy detail pages from HTML links and embedded CMS payloads."""

    parser = parse_html(html)
    candidates = [urljoin(base_url, link.get("href", "")) for link in parser.links]
    embedded_html = html.replace("\\/", "/")
    candidates.extend(
        urljoin(base_url, match.group(0))
        for match in re.finditer(
            r"(?:https?://[^\"'<> ]+)?/(?:en/)?programs/detail/[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=-]+",
            embedded_html,
        )
    )
    urls: list[str] = []
    for candidate in candidates:
        candidate = candidate.rstrip(".,);]}")
        parsed = urlsplit(candidate.split("#", 1)[0])
        if parsed.scheme != "https" or parsed.netloc.lower() not in ACADEMY_HOSTS:
            continue
        if not parsed.path.rstrip("/").startswith(ACADEMY_DETAIL_PATH_PREFIXES):
            continue
        normalized = urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/") + "/", "", ""))
        if normalized not in urls:
            urls.append(normalized)
    return urls


def discover_academy_sitemap_urls(html: str, base_url: str) -> list[str]:
    """Return child sitemap URLs when the Academy publishes a sitemap index."""

    urls: list[str] = []
    for match in re.finditer(r"<loc[^>]*>(.*?)</loc>", html, re.I | re.S):
        candidate = urljoin(base_url, unescape(match.group(1)).strip()).split("#", 1)[0]
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.netloc.lower() not in ACADEMY_HOSTS:
            continue
        if "sitemap" not in parsed.path.lower() and not parsed.path.lower().endswith((".xml", ".xml.gz")):
            continue
        normalized = urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))
        if normalized not in urls:
            urls.append(normalized)
    return urls


def discover_academy_traversal_urls(html: str, base_url: str = ACADEMY_CALENDAR_URL) -> list[str]:
    """Find same-site calendar pages used by pagination or load-more controls."""

    parser = parse_html(html)
    candidates = [urljoin(base_url, link.get("href", "")) for link in parser.links]
    candidates.extend(
        urljoin(base_url, match.group(0))
        for match in re.finditer(r"(?:https?://[^\"'<> ]+)?/[^\"'<> ]*calendar[^\"'<> ]*", html, re.I)
    )
    urls: list[str] = []
    for candidate in candidates:
        parsed = urlsplit(candidate.split("#", 1)[0])
        query = normalize_key(parsed.query)
        if parsed.scheme != "https" or parsed.netloc.lower() not in ACADEMY_HOSTS:
            continue
        if parsed.path.rstrip("/").casefold() not in {"/calendar", "/en/calendar"}:
            continue
        if "programtypes" not in query and not re.search(r"(?:^|&)(?:page|offset|start|cursor|loadmore)=", query):
            continue
        normalized = urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/") or "/", parsed.query, ""))
        if normalized != base_url and normalized not in urls:
            urls.append(normalized)
    return urls


def discover_academy_series_urls(html: str, base_url: str = ACADEMY_FILMS_URL) -> list[str]:
    parser = parse_html(html)
    urls: list[str] = []
    for link in parser.links:
        candidate = urljoin(base_url, link.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.netloc.lower() not in ACADEMY_HOSTS:
            continue
        if not parsed.path.rstrip("/").startswith(("/programs/series/", "/en/programs/series/")):
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def academy_value(document: object, keys: tuple[str, ...]) -> str:
    wanted = {normalize_key(key) for key in keys}
    for record in walk_json(document):
        for key, value in record.items():
            if normalize_key(key) in wanted and isinstance(value, (str, int, float)):
                text = normalize_space(value)
                if text:
                    return text
    return ""


def parse_academy_event(html: str, page_url: str, now: datetime | None = None) -> dict | None:
    """Parse one Academy detail page, accepting only explicitly labelled screenings."""

    current = as_la(now)
    if not urlsplit(page_url).path.rstrip("/").startswith(ACADEMY_DETAIL_PATH_PREFIXES) or not academy_labelled_screening(html):
        return None
    parser = parse_html(html)
    title = next(
        (
            heading["text"] for heading in parser.headings
            if heading.get("tag") == "h1" and heading.get("text") and normalize_key(heading["text"]) != "#"
        ),
        "",
    )
    if not title:
        title = normalize_space(parser.meta.get("og:title", "")).split("|", 1)[0]
    if not title:
        return None
    dates = academy_datetime_candidates(html, parser, current, title=title)
    if not dates:
        return None
    documents = academy_json_documents(html)
    synopsis = academy_value(documents, ("description", "shortDescription", "summary", "synopsis"))
    format_name = academy_value(documents, ("format", "presentationFormat", "filmFormat")) or next(
        (line for line in parser.lines if re.search(r"\b(?:35mm|70mm|16mm|DCP|4K DCP|3D DCP)\b", line, re.I)),
        "",
    )
    year = academy_value(documents, ("releaseYear", "year", "releaseDate"))
    if not year:
        year_match = re.search(r"\b((?:19|20)\d{2})\b", " ".join(parser.lines))
        year = year_match.group(1) if year_match else ""
    note_lines = []
    for line in parser.lines:
        lowered = normalize_key(line)
        if any(marker in lowered for marker in ("in person", "selected by", "accessible screening", "premiere")):
            note_lines.append(line)
    notes = " · ".join(dict.fromkeys(note_lines))[:220]
    image = (absolute_images(parser, page_url) or [""])[0]
    return make_event(
        source_id="academy-museum",
        source_name="Academy Museum of Motion Pictures",
        title=title,
        start_at=dates[0],
        now=current,
        page_url=page_url,
        ticket_url=pick_ticket_url(parser, page_url),
        venue="Academy Museum of Motion Pictures",
        year=year,
        format_name=format_name,
        synopsis=synopsis,
        notes=notes,
        image=image,
        source_page_url=ACADEMY_CALENDAR_URL,
        detail_status="verified-academy-calendar",
    )


class CachedFetcher:
    """GET HTML with a disk cache and a small inter-request delay."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl_seconds: int = DEFAULT_CACHE_TTL,
        timeout: float = DEFAULT_TIMEOUT,
        request_gap: float = DEFAULT_REQUEST_GAP,
        retries: int = DEFAULT_HTTP_RETRIES,
        retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
    ) -> None:
        self.cache_dir = cache_dir or CACHE_DIR
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self.request_gap = max(0.0, request_gap)
        self.retries = max(0, retries)
        self.retry_max_seconds = max(0.0, retry_max_seconds)
        self._host_state: dict[str, tuple[threading.Lock, list[float]]] = {}
        self._host_state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.cache_hits = 0
        self.network_requests = 0

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.html"

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    def _wait_for_host(self, host: str) -> None:
        with self._host_state_lock:
            state = self._host_state.get(host)
            if state is None:
                state = (threading.Lock(), [0.0])
                self._host_state[host] = state
        host_lock, last_request_state = state
        with host_lock:
            last_request = last_request_state[0]
            now = time.monotonic()
            wait = self.request_gap - (now - last_request)
            last_request_state[0] = max(now, last_request) + self.request_gap
        if wait > 0:
            time.sleep(wait)

    def fetch(self, url: str, force: bool = False) -> tuple[str, str, bool]:
        cache_path = self._path_for(url)
        if not force and cache_path.is_file():
            age = time.time() - cache_path.stat().st_mtime
            if age <= self.ttl_seconds:
                with self._stats_lock:
                    self.cache_hits += 1
                return cache_path.read_text(encoding="utf-8", errors="replace"), iso_utc(datetime.fromtimestamp(cache_path.stat().st_mtime, timezone.utc)), True

        host = urlsplit(url).netloc.lower()
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait_for_host(host)
            request_headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
                "Accept-Encoding": "gzip",
                "Connection": "close",
            }
            if host.endswith(".veezi.com") or host == "veezi.com":
                if "fmtswb0qqbym3de6c4bbsqj89m" in url:
                    request_headers["Referer"] = "https://thenewbev.com/"
                elif "20xhpa3yt2hhkwt4zjvfcwsaww" in url:
                    request_headers["Referer"] = "https://www.vistatheaterhollywood.com/"
                request_headers.update(
                    {
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "cross-site",
                        "Upgrade-Insecure-Requests": "1",
                    }
                )
            request = Request(
                url,
                headers=request_headers,
            )
            if force:
                request.add_header("Cache-Control", "no-cache")
                request.add_header("Pragma", "no-cache")
            with self._stats_lock:
                self.network_requests += 1
            try:
                request_timeout = self.timeout
                if host.endswith(".veezi.com") or host == "veezi.com":
                    request_timeout = max(request_timeout, VEEZI_TIMEOUT)
                with urlopen(request, timeout=request_timeout) as response:
                    raw = response.read(8_000_000)
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                text = raw.decode("utf-8", errors="replace")
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
                return text, iso_utc(), False
            except HTTPError as error:
                last_error = error
                if error.code not in TRANSIENT_HTTP_STATUS or attempt >= self.retries:
                    raise
                retry_after = self._retry_after_seconds(error.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else min(0.25 * (2**attempt), self.retry_max_seconds)
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt >= self.retries:
                    raise
                delay = min(0.25 * (2**attempt), self.retry_max_seconds)
            if delay > 0:
                time.sleep(min(delay, self.retry_max_seconds))
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Unable to fetch {url}")


def parallel_fetch(fetcher: CachedFetcher, urls: Iterable[str], force: bool = False, max_workers: int = 8) -> dict[str, tuple[str, str, bool] | Exception]:
    """Fetch independent detail pages concurrently while retaining one cache.

    Index pages are still fetched serially, but their event/detail pages do not
    depend on one another. Limiting workers and keeping the fetcher's global
    request gap avoids an uncontrolled burst against venue sites.
    """

    unique_urls = list(dict.fromkeys(urls))
    if not unique_urls:
        return {}
    results: dict[str, tuple[str, str, bool] | Exception] = {}
    worker_count = min(max_workers, len(unique_urls))
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rerun-fetch")
    futures = {executor.submit(fetcher.fetch, url, force): url for url in unique_urls}
    try:
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as error:  # keep other source pages useful
                results[url] = error
    finally:
        executor.shutdown(wait=True)
    return results


def parse_html(html: str) -> PageParser:
    parser = PageParser()
    parser.feed(html)
    parser.close()
    return parser


def absolute_images(parser: PageParser, base_url: str) -> list[str]:
    candidates = [parser.meta.get("og:image", ""), parser.meta.get("twitter:image", ""), *parser.images]
    results = []
    for candidate in candidates:
        if not normalize_space(candidate):
            continue
        absolute = urljoin(base_url, candidate)
        if not absolute.startswith(("http://", "https://")):
            continue
        if any(token in absolute.lower() for token in ("logo", "icon", "avatar", "facebook")):
            continue
        if absolute not in results:
            results.append(absolute)
    return results


def pick_ticket_url(parser: PageParser, page_url: str) -> str:
    for link in parser.links:
        href = urljoin(page_url, link.get("href", ""))
        label = normalize_key(link.get("text", ""))
        if "ticket" in label or "buy" in label or "veezi" in href.lower():
            return href
    return page_url


def first_label_value(lines: list[str], labels: Iterable[str]) -> str:
    wanted = {normalize_key(label).rstrip(":") for label in labels}
    for index, line in enumerate(lines):
        key = normalize_key(line).rstrip(":")
        for label in wanted:
            if key.startswith(f"{label}:"):
                value = normalize_space(line.split(":", 1)[1])
                if value:
                    return value
            if key.startswith(f"{label} "):
                value = normalize_space(line[len(label) :].lstrip(" :"))
                if value:
                    return value
        if key in wanted:
            if ":" in line and line.split(":", 1)[0].strip().casefold() in wanted:
                value = normalize_space(line.split(":", 1)[1])
                if value:
                    return value
            for candidate in lines[index + 1 : index + 5]:
                if candidate and normalize_key(candidate).rstrip(":") not in wanted:
                    return candidate
    return ""


def parse_ac_datetime(text: str) -> datetime | None:
    match = AC_DATE_RE.search(text)
    if not match:
        return None
    values = match.groupdict()
    try:
        hour = int(values["hour"]) % 12
        if values["meridiem"].lower().replace(".", "") == "pm":
            hour += 12
        return datetime(
            int(values["year"]),
            MONTHS[values["month"].lower()[:3]],
            int(values["day"]),
            hour,
            int(values["minute"]),
            tzinfo=LA_TZ,
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_ac_datetimes(text: str) -> list[datetime]:
    values: list[datetime] = []
    for match in AC_DATE_RE.finditer(text):
        parsed = parse_ac_datetime(match.group(0))
        if parsed:
            values.append(parsed)
    return values


def parse_ac_series_datetime(date_text: str, time_text: str) -> datetime | None:
    date_match = AC_DAY_DATE_RE.search(date_text)
    time_match = TIME_RE.search(time_text)
    if not date_match or not time_match:
        return None
    values = date_match.groupdict()
    time_values = time_match.groupdict()
    try:
        hour, minute = parse_time(time_values["hour"], time_values.get("minute") or "0", time_values["meridiem"])
        return datetime(
            int(values["year"]),
            MONTHS[values["month"].lower()[:3]],
            int(values["day"]),
            hour,
            minute,
            tzinfo=LA_TZ,
        )
    except (KeyError, TypeError, ValueError):
        return None


def is_ac_screening_title(title: str) -> bool:
    key = normalize_key(title).rstrip(":")
    return bool(key) and key not in AC_NON_SCREENING_TITLES and key not in GENERIC_SCREENING_TITLES and not key.startswith(("about the ", "programmed by "))


def is_probable_screening_title(title: object) -> bool:
    """Reject generic navigation/card labels before they become events."""

    key = screening_title_key(title)
    return bool(key) and key not in {screening_title_key(value) for value in (*AC_NON_SCREENING_TITLES, *GENERIC_SCREENING_TITLES)}


def screening_identity(event: dict) -> tuple[str, str, str, str]:
    return (
        normalize_key(event.get("city", "")),
        screening_title_key(event.get("venue", "")),
        screening_title_key(event.get("title", "")),
        normalize_space(event.get("startAt", "")),
    )


def event_quality(event: dict) -> tuple[int, int]:
    status = normalize_key(event.get("sourceStatus", ""))
    verified = 2 if status.startswith("verified") else 1 if status else 0
    metadata = sum(bool(normalize_space(event.get(field, ""))) for field in ("director", "year", "format", "synopsis", "notes", "image"))
    return verified, metadata


def deduplicate_events(events: Iterable[dict]) -> list[dict]:
    """Keep one best record for each venue/title/start tuple."""

    unique: dict[tuple[str, str, str, str], dict] = {}
    for event in events:
        if not is_probable_screening_title(event.get("title", "")):
            continue
        key = screening_identity(event)
        current = unique.get(key)
        if current is None or event_quality(event) > event_quality(current):
            unique[key] = event
    return sorted(unique.values(), key=lambda item: item.get("startAt", ""))


def ac_event_url_date(page_url: str) -> tuple[int, int, int] | None:
    """Read the optional month/day/year suffix from an AC event URL."""

    slug = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    match = AC_EVENT_URL_DATE_RE.search(slug)
    if not match:
        return None
    try:
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        return year, int(match.group("month")), int(match.group("day"))
    except (TypeError, ValueError):
        return None


def ac_event_title_from_url(page_url: str) -> str:
    """Create a conservative fallback title from an AC event URL slug."""

    slug = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = AC_EVENT_URL_DATE_RE.sub("", slug)
    return normalize_space(slug.replace("-", " ").replace("_", " ")).title()


def parse_new_bev_date(text: str, default_year: int) -> tuple[int, int, int] | None:
    match = NEW_BEV_DATE_RE.search(text)
    if not match:
        return None
    values = match.groupdict()
    try:
        return int(values.get("year") or default_year), MONTHS[values["month"].lower()[:3]], int(values["day"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_time(hour: str, minute: str, meridiem: str) -> tuple[int, int]:
    value = int(hour) % 12
    if meridiem.lower().replace(".", "") == "pm":
        value += 12
    return value, int(minute or 0)


def infer_genre(title: str, notes: str = "", synopsis: str = "", source: str = "") -> str:
    text = normalize_key(" ".join((title, notes, synopsis, source)))
    if "silent" in text or "live score" in text:
        return "Silent film"
    if "midnight" in text or "11:59" in text or "10:00 pm" in text:
        return "Midnight"
    if "festival" in text:
        return "Festival"
    if "double feature" in text or " / " in title:
        return "Double feature"
    if "cartoon" in text or "animation" in text:
        return "Animation"
    return "Repertory"


def canonical_venue_from_lines(lines: list[str], source_default: str) -> dict:
    known_venues = tuple(sorted(VENUE_CATALOG, key=len, reverse=True))
    # American Cinematheque puts the event's venue in the metadata row directly
    # after the event date/time, e.g. ``Los Feliz 3 | ...``. Restrict the first
    # pass to that local block: the document can also contain global theatre
    # navigation and other program/related-event text mentioning Aero.
    date_index = next((index for index, line in enumerate(lines) if AC_DATE_RE.search(line)), -1)
    metadata_lines = lines[date_index + 1 : date_index + 18] if date_index >= 0 else []
    for line in metadata_lines:
        if "|" not in line:
            continue
        venue_label = line.split("|", 1)[0]
        if any(known in normalize_key(venue_label) for known in known_venues):
            return venue_info(venue_label, source_default)

    # If the metadata row is absent, constrain the fallback to the primary
    # Locations section instead of scanning the entire document.
    locations_index = next((index for index, line in enumerate(lines) if normalize_key(line).rstrip(":") == "locations"), len(lines))
    related_index = next((index for index in range(locations_index + 1, len(lines)) if normalize_key(lines[index]).rstrip(":") in {"related events", "related screenings"}), len(lines))
    for line in lines[locations_index + 1 : related_index]:
        key = normalize_key(line)
        if any(known in key for known in known_venues):
            return venue_info(line, source_default)
    return venue_info(source_default, source_default)


def event_notes(lines: list[str], venue_name: str) -> str:
    for line in lines:
        if normalize_key(venue_name) in normalize_key(line) and "|" in line:
            note = normalize_space(line.split("|", 1)[1])
            if note:
                return note[:220]
    return ""


def synopsis_from_lines(lines: list[str], markers: tuple[str, ...] = ("ABOUT THE FILM:", "ABOUT THE FILMS:")) -> str:
    start = -1
    for index, line in enumerate(lines):
        if normalize_key(line).rstrip(":") in {normalize_key(marker).rstrip(":") for marker in markers}:
            start = index + 1
            break
    if start < 0:
        return ""
    collected: list[str] = []
    for line in lines[start:]:
        if normalize_key(line).startswith(("format:", "distributor:", "country:", "locations", "related events")):
            break
        if line.startswith("###") or line.startswith("##"):
            break
        collected.append(line)
    return normalize_space(" ".join(collected))[:520]


def credit_values(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Read inline credits used by American Cinematheque double features."""
    text = normalize_space(" ".join(lines))
    # AC uses entries such as ``MIRAGE, 1965, Dir. Edward Dmytryk, 109 Mins``.
    matches = re.findall(r"\b((?:19|20)\d{2}),\s*(?:Dir\.?|Directed by)\s+([^,.;]+)", text, flags=re.I)
    years = [match[0] for match in matches]
    directors = [normalize_space(match[1]) for match in matches]
    formats = [normalize_space(match) for match in re.findall(r"^FORMAT:\s*([^\n]+)", "\n".join(lines), flags=re.I | re.M)]
    return years, directors, formats


def make_event(
    *,
    source_id: str,
    source_name: str,
    title: str,
    start_at: datetime,
    now: datetime,
    page_url: str,
    ticket_url: str,
    venue: str,
    director: str = "",
    year: str = "",
    format_name: str = "",
    synopsis: str = "",
    notes: str = "",
    image: str = "",
    source_page_url: str = "",
    detail_status: str = "verified",
) -> dict:
    venue_data = venue_info(venue, source_name)
    local_start = as_la(start_at)
    clean_title = normalize_space(title)
    event_id = f"{source_id}-{slugify(clean_title)}-{slugify(venue_data['name'])}-{local_start.strftime('%Y%m%d-%H%M')}"
    clean_synopsis = synopsis or f"Official listing from {source_name}. Check the venue page for current ticket availability."
    genre = infer_genre(clean_title, notes, clean_synopsis, source_name)
    if local_start.hour >= 22 and genre == "Repertory":
        genre = "Midnight"
    return {
        "id": event_id,
        "city": "los-angeles",
        "title": clean_title,
        "director": normalize_space(director) or "Not listed",
        "year": normalize_space(year) or "—",
        "format": normalize_space(format_name) or "Format not listed",
        "venue": venue_data["name"],
        "neighborhood": venue_data["neighborhood"],
        "address": venue_data["address"],
        "distance": venue_data["neighborhood"],
        "date": display_date(local_start, now),
        "dateGroup": date_group(local_start, now),
        "time": display_time(local_start),
        "startAt": local_start.isoformat(),
        "genre": genre,
        "color": color_for(clean_title, source_id),
        "image": image,
        "imageAlt": f"Film still for {clean_title} from the {source_name} program page" if image else "",
        "imageCredit": f"Image loaded from {source_name}; confirm commercial image rights before launch." if image else "",
        "imageSource": image,
        "synopsis": clean_synopsis,
        "notes": notes,
        "ticketUrl": ticket_url or page_url,
        "sourceUrl": page_url,
        "sourcePageUrl": source_page_url or page_url,
        "sourceName": source_name,
        "sourceLastChecked": iso_utc(),
        "sourceStatus": detail_status,
        "sourceLabel": f"Official · {source_name}",
    }


def parse_american_cinematheque_event(html: str, page_url: str, now: datetime | None = None) -> dict | None:
    if not urlsplit(page_url).path.rstrip("/").startswith("/now-showing/"):
        return None
    parser = parse_html(html)
    lines = parser.lines
    start_at = parse_ac_datetime(parser.text)
    url_date = ac_event_url_date(page_url)
    if url_date:
        matching_dates = [
            candidate
            for candidate in parse_ac_datetimes(parser.text)
            if (candidate.year, candidate.month, candidate.day) == url_date
        ]
        if matching_dates:
            start_at = matching_dates[0]
    title = next((heading["text"] for heading in parser.headings if heading["tag"] == "h1" and heading["text"]), "")
    if not title:
        title = normalize_space(parser.meta.get("og:title", "")).split("|")[0].strip()
    if not start_at or not is_ac_screening_title(title):
        return None
    current = as_la(now)
    if as_la(start_at) <= current:
        return None
    venue_data = canonical_venue_from_lines(lines, "American Cinematheque")
    notes = event_notes(lines, venue_data["name"])
    images = absolute_images(parser, page_url)
    image = images[0] if images else ""
    years, directors, formats = credit_values(lines)
    director = first_label_value(lines, ("DIRECTED BY", "Director")) or " / ".join(dict.fromkeys(directors))
    year = first_label_value(lines, ("RELEASED IN", "Year")) or " / ".join(dict.fromkeys(years))
    format_name = " / ".join(dict.fromkeys(formats)) if len(formats) > 1 else (first_label_value(lines, ("FORMAT", "Format")) or (formats[0] if formats else ""))
    return make_event(
        source_id="american-cinematheque",
        source_name="American Cinematheque",
        title=title,
        start_at=start_at,
        now=current,
        page_url=page_url,
        ticket_url=pick_ticket_url(parser, page_url),
        venue=venue_data["name"],
        director=director,
        year=year,
        format_name=format_name,
        synopsis=synopsis_from_lines(lines),
        notes=notes,
        image=image,
        detail_status="verified",
    )


def discover_american_cinematheque_urls(html: str, base_url: str = AC_NOW_SHOWING_URL) -> list[str]:
    """Find AC event and active-program pages in server-rendered HTML.

    The public ``/now-showing/`` route is currently a client-rendered shell,
    while the homepage exposes only a small featured subset. Its active
    ``/series/`` pages contain the complete event list for a program, so they
    travel through the same bounded discovery graph as event pages. Tracking
    parameters are removed to keep one canonical URL in the queue and cache.
    """

    parser = parse_html(html)
    urls: list[str] = []
    for link in parser.links:
        candidate = urljoin(base_url, link.get("href", ""))
        parts = urlsplit(candidate)
        path = parts.path.rstrip("/")
        is_event = path.startswith("/now-showing/") and path != "/now-showing"
        is_series = path.startswith("/series/") and path != "/series"
        if parts.scheme != "https" or not (is_event or is_series):
            continue
        if parts.netloc.lower() not in {"www.americancinematheque.com", "americancinematheque.com"}:
            continue
        canonical = urlunsplit(("https", "www.americancinematheque.com", parts.path.rstrip("/") + "/", "", ""))
        if canonical not in urls:
            urls.append(canonical)
    return urls


def parse_american_cinematheque_series(html: str, page_url: str, now: datetime | None = None) -> list[dict]:
    """Parse the official series calendar cards as the AC schedule layer.

    Series pages expose each future card in a stable sequence: date, time,
    title, venue metadata, and a link to the detail page. The detail page is
    still useful for artwork and film metadata, but this schedule layer keeps
    a valid screening visible when a single detail page is slow or malformed.
    """

    parser = parse_html(html)
    current = as_la(now)
    event_urls = [
        url
        for url in discover_american_cinematheque_urls(html, page_url)
        if urlsplit(url).path.rstrip("/").startswith("/now-showing/")
    ]
    unused_event_urls = list(event_urls)
    date_indexes = [
        index
        for index, line in enumerate(parser.lines)
        if AC_DAY_DATE_RE.search(line)
    ]
    entries: list[dict] = []
    for card_index, date_index in enumerate(date_indexes):
        next_date_index = date_indexes[card_index + 1] if card_index + 1 < len(date_indexes) else len(parser.lines)
        card_lines = parser.lines[date_index:next_date_index]
        time_line = next((line for line in card_lines[1:5] if TIME_RE.search(line)), "")
        start_at = parse_ac_series_datetime(parser.lines[date_index], time_line)
        if not start_at or start_at <= current:
            continue
        venue_line = next(
            (
                line
                for line in card_lines
                if "|" in line
                and any(known in normalize_key(line.split("|", 1)[0]) for known in VENUE_CATALOG)
            ),
            "",
        )
        if not venue_line:
            continue
        card_keys = {normalize_key(line) for line in card_lines}
        local_titles = [
            heading["text"]
            for heading in parser.headings
            if heading["tag"] in {"h2", "h3", "h4"}
            and is_ac_screening_title(heading["text"])
            and normalize_key(heading["text"]) in card_keys
        ]
        # Pair each card with a URL from the same date before falling back to
        # sequence. This prevents a non-film heading or related link from
        # shifting every subsequent title/link association.
        card_date = start_at.date() if start_at else None
        matching_urls = [
            url
            for url in unused_event_urls
            if card_date and ac_event_url_date(url) == (card_date.year, card_date.month, card_date.day)
        ]
        page = matching_urls[0] if matching_urls else (unused_event_urls[0] if unused_event_urls else page_url)
        if page in unused_event_urls:
            unused_event_urls.remove(page)
        title = local_titles[0] if local_titles else ""
        url_title = ac_event_title_from_url(page) if page != page_url else ""
        if url_title and (not title or (matching_urls and slugify(title) != slugify(url_title))):
            title = url_title
        if not title:
            continue
        entries.append(
            {
                "title": title,
                "startAt": start_at,
                "venue": venue_info(venue_line.split("|", 1)[0], "American Cinematheque")["name"],
                "notes": event_notes(card_lines, venue_line.split("|", 1)[0]),
                "pageUrl": page,
                "sourcePageUrl": page_url,
            }
        )
    return entries


def parse_new_beverly_schedule(html: str, page_url: str = NEW_BEV_SCHEDULE_URL, now: datetime | None = None) -> list[dict]:
    parser = parse_html(html)
    current = as_la(now)
    year_match = re.search(rf"\b(?:{MONTH_PATTERN})\s+(20\d{{2}})\b", parser.text, re.I)
    default_year = int(year_match.group(1)) if year_match else current.year
    candidates: list[tuple[str, str]] = []
    for link in parser.links:
        text = normalize_space(link.get("text", ""))
        href = urljoin(page_url, link.get("href", ""))
        if urlsplit(href).scheme == "https" and urlsplit(href).netloc.lower() in {"thenewbev.com", "www.thenewbev.com"} and "/program/" in urlsplit(href).path and NEW_BEV_DATE_RE.search(text):
            candidates.append((text, href))
    if not candidates:
        candidates = [
            (line, page_url)
            for line in parser.lines
            if NEW_BEV_DATE_RE.search(line) and TIME_RE.search(line)
        ]

    results: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry_text, href in candidates:
        date_match = NEW_BEV_DATE_RE.search(entry_text)
        if not date_match:
            continue
        parsed_date = parse_new_bev_date(entry_text, default_year)
        if not parsed_date:
            continue
        tail = entry_text[date_match.end() :]
        tail = re.split(r"\bMore\s+Info\b", tail, maxsplit=1, flags=re.I)[0]
        times = list(TIME_RE.finditer(tail))
        if not times:
            continue
        title_blob = normalize_space(tail[times[-1].end() :]).strip(" /|·")
        title_parts = [normalize_space(part).strip(" /|·") for part in re.split(r"\s+/\s+", title_blob) if normalize_space(part)]
        if len(times) == 1:
            title_parts = [title_blob]
        elif len(title_parts) != len(times):
            if len(title_parts) > len(times):
                title_parts = title_parts[: len(times) - 1] + [" / ".join(title_parts[len(times) - 1 :])]
            elif title_parts:
                title_parts.extend([title_parts[-1]] * (len(times) - len(title_parts)))
            else:
                # A date/time row without a film title is usually a program
                # header or navigation fragment, not a screening.
                continue
        for index, time_match in enumerate(times):
            title = title_parts[index] if index < len(title_parts) else title_parts[-1]
            if not title or not is_probable_screening_title(title):
                continue
            hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
            try:
                start_at = datetime(parsed_date[0], parsed_date[1], parsed_date[2], hour, minute, tzinfo=LA_TZ)
            except ValueError:
                continue
            if start_at <= current:
                continue
            key = (href, normalize_key(title), start_at.isoformat())
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "title": title,
                    "startAt": start_at,
                    "pageUrl": href,
                    "scheduleUrl": page_url,
                    "notes": "Midnight show" if hour >= 23 else "",
                }
            )
    unique_results: dict[tuple[str, str], dict] = {}
    for result in results:
        key = (screening_title_key(result.get("title", "")), result["startAt"].isoformat())
        unique_results.setdefault(key, result)
    return list(unique_results.values())


def _section_for_title(lines: list[str], title: str, all_titles: list[str]) -> list[str]:
    wanted = normalize_key(title)
    title_keys = {normalize_key(candidate) for candidate in all_titles if candidate}
    possible = [index for index, line in enumerate(lines) if normalize_key(line) == wanted]
    for index in reversed(possible):
        section = lines[index + 1 : index + 70]
        end = len(section)
        for offset, line in enumerate(section):
            if offset > 0 and normalize_key(line) in title_keys:
                end = offset
                break
        section = section[:end]
        if any(normalize_key(line).rstrip(":") in {"director", "year", "format"} for line in section):
            return section
    return []


def _detail_synopsis(section: list[str]) -> str:
    if not section:
        return ""
    stop = len(section)
    labels = {"director", "year", "format", "rated", "country", "running time"}
    for index, line in enumerate(section):
        if normalize_key(line).rstrip(":") in labels:
            stop = index
            break
    noise = {"buy tickets", "view trailer"}
    prose = [line for line in section[:stop] if normalize_key(line) not in noise]
    return normalize_space(" ".join(prose))[:520]


def parse_new_beverly_detail(html: str, page_url: str) -> dict:
    parser = parse_html(html)
    headings = [heading["text"] for heading in parser.headings if heading["text"]]
    film_titles = []
    for heading in headings:
        if heading in {"Special Event", "Double Feature", "Friday Matinee", "Cartoon Club", "Midnight Show"}:
            continue
        if normalize_key(heading).startswith(("upcoming showtimes", "new beverly")):
            continue
        if heading not in film_titles and len(heading) > 2:
            film_titles.append(heading)
    # h2 headings are the film sections; h3 entries are a useful fallback.
    h2_titles = [heading["text"] for heading in parser.headings if heading["tag"] == "h2" and heading["text"]]
    if h2_titles:
        film_titles = []
        for title in h2_titles:
            if normalize_key(title).startswith(("upcoming showtimes", "new beverly")):
                continue
            if title not in film_titles:
                film_titles.append(title)
    films = []
    lines = parser.lines
    for title in film_titles:
        section = _section_for_title(lines, title, film_titles)
        films.append(
            {
                "title": title,
                "director": first_label_value(section, ("Director", "Directed by")),
                "year": first_label_value(section, ("Year", "Released in")),
                "format": first_label_value(section, ("Format",)),
                "synopsis": _detail_synopsis(section),
            }
        )
    images = absolute_images(parser, page_url)
    ticket_url = pick_ticket_url(parser, page_url)
    return {
        "films": films,
        "ticketUrl": ticket_url,
        "image": images[0] if images else "",
        "images": images,
        "pageUrl": page_url,
    }


VIDIOTS_DATE_RE = re.compile(
    rf"\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|{DAY_NAMES})\.?[,]?\s+)?"
    rf"(?P<month>{MONTH_PATTERN_SHORT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:[,]?\s*(?P<year>20\d{{2}}))?\b",
    re.I,
)
VIDIOTS_NUMERIC_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>20\d{2}))?\b")


def parse_vidiots_date(text: str, current: datetime) -> tuple[int, int, int] | None:
    match = VIDIOTS_DATE_RE.search(text) or VIDIOTS_NUMERIC_DATE_RE.search(text)
    if not match:
        return None
    values = match.groupdict()
    try:
        month_value = values["month"]
        month = int(month_value) if month_value.isdigit() else MONTHS[month_value.lower()[:3]]
        year = int(values.get("year") or (current.year + 1 if month < current.month else current.year))
        return year, month, int(values["day"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _ticket_links_by_time(links: Iterable[dict], base_url: str) -> list[dict]:
    """Keep ticket anchors that expose a matching showtime label."""

    results: list[dict] = []
    for link in links:
        label = normalize_space(link.get("text", ""))
        time_match = TIME_RE.search(label)
        href = urljoin(base_url, link.get("href", ""))
        if not time_match or not href.startswith(("http://", "https://")):
            continue
        hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
        results.append({"signature": (hour, minute), "href": href})
    return results


def parse_vidiots_showtimes(
    lines: list[str],
    now: datetime | None = None,
    links: Iterable[dict] | None = None,
    base_url: str = "",
) -> list[dict]:
    """Parse upcoming Vidiots date/time labels from an official movie page."""

    current = as_la(now)
    active_date: tuple[int, int, int] | None = None
    results: list[dict] = []
    seen: set[str] = set()
    ticket_links = _ticket_links_by_time(links or [], base_url)
    used_ticket_links: set[int] = set()
    for raw_line in lines:
        line = normalize_space(raw_line)
        if not line:
            continue
        parsed_date = parse_vidiots_date(line, current)
        if parsed_date:
            active_date = parsed_date
        if not active_date:
            continue
        notes = ""
        lowered = normalize_key(line)
        for marker, label in (
            ("sold out", "Sold out"),
            ("q&a", "Q&A"),
            ("special guest", "Special guest"),
            ("matinee", "Matinee"),
            ("midnight", "Midnight show"),
        ):
            if marker in lowered:
                notes = label
                break
        for time_match in TIME_RE.finditer(line):
            hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
            try:
                start_at = datetime(*active_date, hour, minute, tzinfo=LA_TZ)
            except ValueError:
                continue
            if start_at <= current:
                continue
            key = start_at.isoformat()
            if key in seen:
                continue
            seen.add(key)
            ticket_url = ""
            signature = (hour, minute)
            for index, ticket_link in enumerate(ticket_links):
                if index in used_ticket_links or ticket_link["signature"] != signature:
                    continue
                used_ticket_links.add(index)
                ticket_url = ticket_link["href"]
                break
            results.append({"startAt": start_at, "notes": notes, "ticketUrl": ticket_url})
    return results


def discover_vidiots_movie_urls(html: str, base_url: str = VIDIOTS_URL) -> list[str]:
    """Find official Vidiots movie pages linked from the coming-soon index."""

    parser = parse_html(html)
    urls: list[str] = []
    allowed_hosts = {"vidiotsfoundation.org", "www.vidiotsfoundation.org"}
    for link in parser.links:
        candidate = urljoin(base_url, link.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(candidate)
        path = parsed.path.rstrip("/")
        if parsed.scheme != "https" or parsed.netloc.lower() not in allowed_hosts:
            continue
        if not path.startswith("/movies/") or len(path.split("/")) < 3:
            continue
        if path.casefold() in {"/movies", "/movies/featured", "/movies/search"}:
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def discover_vidiots_traversal_urls(html: str, base_url: str = VIDIOTS_HOME_URL) -> list[str]:
    """Find official upcoming-listing pages to traverse from the homepage."""

    parser = parse_html(html)
    allowed_hosts = {"vidiotsfoundation.org", "www.vidiotsfoundation.org"}
    allowed_paths = {"/coming-soon", "/upcoming-movies"}
    urls: list[str] = []
    for link in parser.links:
        candidate = urljoin(base_url, link.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.netloc.lower() not in allowed_hosts:
            continue
        if parsed.path.rstrip("/").casefold() not in allowed_paths:
            continue
        canonical = urlunsplit(("https", "vidiotsfoundation.org", parsed.path.rstrip("/") + "/", "", ""))
        if canonical not in urls:
            urls.append(canonical)
    return urls


def _vidiots_title_from_url(page_url: str) -> str:
    slug = urlsplit(page_url).path.rstrip("/").split("/")[-1]
    return normalize_space(slug.replace("-", " ").replace("_", " ")).title() if slug else ""


def parse_vidiots_listing(html: str, base_url: str = VIDIOTS_URL, now: datetime | None = None) -> list[dict]:
    """Parse Vidiots' index cards when dates and times are server-rendered."""

    current = as_la(now)
    parser = parse_html(html)
    links: list[tuple[str, str]] = []
    generic_labels = {"more info", "learn more", "read more", "details", "tickets", "buy tickets"}
    for link in parser.links:
        page_url = urljoin(base_url, link.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(page_url)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"vidiotsfoundation.org", "www.vidiotsfoundation.org"}:
            continue
        if not parsed.path.rstrip("/").startswith("/movies/"):
            continue
        title = normalize_space(link.get("text", ""))
        if not title or normalize_key(title) in generic_labels:
            title = _vidiots_title_from_url(page_url)
        if title and (page_url, title) not in links:
            links.append((page_url, title))
    if not links:
        return []

    title_map = {normalize_key(title): (page_url, title) for page_url, title in links}
    active_title: tuple[str, str] | None = None
    active_date: tuple[int, int, int] | None = None
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    ticket_links = _ticket_links_by_time(parser.links, base_url)
    used_ticket_links: set[int] = set()
    for raw_line in parser.lines:
        line = normalize_space(raw_line)
        if not line:
            continue
        mapped_title = title_map.get(normalize_key(line))
        if mapped_title:
            active_title = mapped_title
        parsed_date = parse_vidiots_date(line, current)
        if parsed_date:
            active_date = parsed_date
        if not active_title or not active_date:
            continue
        for time_match in TIME_RE.finditer(line):
            hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
            try:
                start_at = datetime(*active_date, hour, minute, tzinfo=LA_TZ)
            except ValueError:
                continue
            if start_at <= current:
                continue
            notes = ""
            lowered = normalize_key(line)
            if "sold out" in lowered:
                notes = "Sold out"
            elif "q&a" in lowered:
                notes = "Q&A"
            elif "special guest" in lowered:
                notes = "Special guest"
            elif "matinee" in lowered:
                notes = "Matinee"
            elif "midnight" in lowered:
                notes = "Midnight show"
            page_url, title = active_title
            key = (page_url, start_at.isoformat())
            if key in seen:
                continue
            seen.add(key)
            ticket_url = ""
            signature = (hour, minute)
            for index, ticket_link in enumerate(ticket_links):
                if index in used_ticket_links or ticket_link["signature"] != signature:
                    continue
                used_ticket_links.add(index)
                ticket_url = ticket_link["href"]
                break
            results.append({"title": title, "pageUrl": page_url, "startAt": start_at, "notes": notes, "ticketUrl": ticket_url})
    return results


def parse_vidiots_partner_listing(html: str, base_url: str = VIDIOTS_PARTNER_URL, now: datetime | None = None) -> list[dict]:
    """Parse TeenTix LA's Vidiots partner venue listing as a fallback."""

    current = as_la(now)
    parser = parse_html(html)
    title_map: dict[str, tuple[str, str]] = {}
    for link in parser.links:
        page_url = urljoin(base_url, link.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(page_url)
        title = normalize_space(link.get("text", ""))
        if (
            parsed.scheme == "https"
            and parsed.netloc.lower() in {"la.teentix.org", "www.teentix.org"}
            and parsed.path.startswith("/calendar/event/")
            and title
        ):
            title_map[normalize_key(title)] = (page_url, title)
    if not title_map:
        return []

    active_title: tuple[str, str] | None = None
    active_date: tuple[int, int, int] | None = None
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in parser.lines:
        line = normalize_space(raw_line)
        if not line:
            continue
        mapped_title = title_map.get(normalize_key(line))
        if mapped_title:
            active_title = mapped_title
        parsed_date = parse_vidiots_date(line, current)
        if parsed_date:
            active_date = parsed_date
        if not active_title or not active_date:
            continue
        for time_match in TIME_RE.finditer(line):
            hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
            try:
                start_at = datetime(*active_date, hour, minute, tzinfo=LA_TZ)
            except ValueError:
                continue
            if start_at <= current:
                continue
            notes = ""
            lowered = normalize_key(line)
            if "sold out" in lowered:
                notes = "Sold out"
            elif "q&a" in lowered:
                notes = "Q&A"
            key = (active_title[0], start_at.isoformat())
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "title": active_title[1],
                    "pageUrl": active_title[0],
                    "startAt": start_at,
                    "notes": notes,
                }
            )
    return results


def parse_vidiots_movie_detail(html: str, page_url: str, now: datetime | None = None) -> dict:
    """Extract one Vidiots film page and its upcoming showtimes."""

    parser = parse_html(html)
    lines = parser.lines
    ignored = {"coming soon", "upcoming movies", "vidiots", "movies", "showtimes", "tickets"}
    title = next(
        (
            heading["text"]
            for heading in parser.headings
            if heading.get("tag") == "h1"
            and heading.get("text")
            and normalize_key(heading["text"]) not in ignored
        ),
        "",
    )
    if not title:
        title = normalize_space(parser.meta.get("og:title", "")).split("|", 1)[0].strip()
    if not title:
        title = _vidiots_title_from_url(page_url)
    if not title:
        return {"title": "", "showtimes": [], "pageUrl": page_url}

    year_label = first_label_value(lines, ("Year", "Release year", "Released in"))
    year_matches = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", " ".join(lines))
    section = _section_for_title(lines, title, [title])
    synopsis = synopsis_from_lines(lines, markers=("ABOUT THE FILM:", "SYNOPSIS:", "SUMMARY:"))
    if not synopsis and section:
        synopsis = _detail_synopsis(section)
    note_lines = []
    for line in lines:
        lowered = normalize_key(line)
        if any(marker in lowered for marker in ("q&a", "special guest", "sold out", "matinee", "midnight", "35mm", "16mm", "dcp")):
            note_lines.append(line)
    notes = " · ".join(dict.fromkeys(note_lines))[:220]
    return {
        "title": title,
        "showtimes": parse_vidiots_showtimes(lines, now, parser.links, page_url),
        "director": first_label_value(lines, ("Director", "Directed by")),
        "year": year_label or (year_matches[-1] if year_matches else ""),
        "format": vista_format_from_text(lines),
        "synopsis": synopsis,
        "notes": notes,
        "ticketUrl": pick_ticket_url(parser, page_url),
        "image": (absolute_images(parser, page_url) or [""])[0],
        "pageUrl": page_url,
    }


def discover_vista_movie_urls(html: str, base_url: str = VISTA_SCHEDULE_URL) -> list[str]:
    """Find the official Vista movie detail pages linked from its schedule."""

    parser = parse_html(html)
    urls: list[str] = []
    for link in parser.links:
        candidate = urljoin(base_url, link.get("href", ""))
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {
            "vistatheaterhollywood.com",
            "www.vistatheaterhollywood.com",
        }:
            continue
        if not parsed.path.startswith("/movies/") or parsed.path.rstrip("/") == "/movies":
            continue
        normalized = candidate.split("#", 1)[0]
        if normalized not in urls:
            urls.append(normalized)
    return urls


VISTA_SESSION_DATE_RE = re.compile(
    rf"^(?:[A-Za-z]+\.?\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(?P<month>{MONTH_PATTERN})(?:\s+(?P<year>20\d{{2}}))?$",
    re.I,
)
VISTA_SESSION_DATE_ALT_RE = re.compile(
    rf"^(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[,]?\s*(?P<year>20\d{{2}})?$",
    re.I,
)
VISTA_SESSION_TIME_RE = re.compile(
    r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>[ap]\.?(?:m)\.?)\s*(?P<status>sold\s+out|bookings\s+closed)?$",
    re.I,
)


def parse_vista_session_date(line: str, current: datetime) -> tuple[int, int, int] | None:
    match = VISTA_SESSION_DATE_RE.fullmatch(normalize_space(line)) or VISTA_SESSION_DATE_ALT_RE.fullmatch(normalize_space(line))
    if not match:
        return None
    values = match.groupdict()
    try:
        month = MONTHS[values["month"].lower()[:3]]
        year = int(values.get("year") or (current.year + 1 if month < current.month else current.year))
        return year, month, int(values["day"])
    except (KeyError, TypeError, ValueError):
        return None


def vista_format_from_text(lines: Iterable[str]) -> str:
    tokens: list[str] = []
    for line in lines:
        for token in re.findall(r"\b(?:35|70|16)mm\b|\b(?:DVD|VHS|DCP|IMAX)\b", line, re.I):
            normalized = token.upper() if token.upper() in {"DVD", "VHS", "DCP", "IMAX"} else token.lower()
            if normalized not in tokens:
                tokens.append(normalized)
    return " / ".join(tokens)


def parse_vista_session_feed(html: str, now: datetime | None = None) -> list[dict]:
    """Parse Veezi's canonical Vista session list.

    Veezi exposes each film as a heading followed by one or more date/time
    groups. Unlike the Vista marketing pages, these sessions are the ticketing
    system's current inventory and include sold-out labels and special-format
    descriptions.
    """

    current = as_la(now)
    parser = parse_html(html)
    ignored_titles = {"show times", "sort by date", "sort by film", "double feature"}
    title_map = {
        normalize_key(heading["text"]): heading["text"]
        for heading in parser.headings
        if heading.get("tag") in {"h2", "h3"}
        and heading.get("text")
        and normalize_key(heading["text"]) not in ignored_titles
        and is_probable_screening_title(heading["text"])
        and parse_vista_session_date(heading["text"], current) is None
    }
    current_title = ""
    current_date: tuple[int, int, int] | None = None
    current_notes: list[str] = []
    collecting_notes = False
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for raw_line in parser.lines:
        line = normalize_space(raw_line)
        if not line:
            continue
        date_value = parse_vista_session_date(line, current)
        if date_value:
            current_date = date_value
            collecting_notes = False
            continue
        title = title_map.get(normalize_key(line))
        if title:
            current_title = title
            current_notes = []
            collecting_notes = True
            continue
        time_match = VISTA_SESSION_TIME_RE.fullmatch(line)
        if time_match and current_title and current_date:
            year, month, day = current_date
            hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
            try:
                start_at = datetime(year, month, day, hour, minute, tzinfo=LA_TZ)
            except ValueError:
                continue
            if start_at <= current:
                continue
            status = normalize_key(time_match.group("status"))
            sold_out = status == "sold out"
            notes = [*current_notes]
            if sold_out:
                notes.append("Sold out")
            elif status == "bookings closed":
                notes.append("Bookings closed")
            key = (normalize_key(current_title), start_at.isoformat())
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "title": current_title,
                    "startAt": start_at,
                    "format": vista_format_from_text(current_notes),
                    "notes": " · ".join(dict.fromkeys(notes)),
                    "soldOut": sold_out,
                }
            )
            continue
        if collecting_notes and ("video archives" in normalize_key(line) or vista_format_from_text((line,))):
            current_notes.append(line)

    return results


# Both theaters use the same Veezi session-list structure. Keep the Vista
# name as a compatibility alias for callers and tests while exposing the
# ticketing parser under the provider's generic name for new adapters.
parse_veezi_session_feed = parse_vista_session_feed


FILMFORUM_DATETIME_RE = re.compile(
    rf"(?:date|start date):\s*(?P<month>{MONTH_PATTERN_SHORT})\s+(?P<day>\d{{1,2}}),\s*(?P<year>20\d{{2}})\s+(?P<hour>\d{{1,2}})(?::(?P<minute>\d{{2}}))?\s*(?P<meridiem>[ap]\.?(?:m)\.?)",
    re.I,
)
OLD_TOWN_SHOWTIME_RE = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN_SHORT})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(?P<hour>\d{{1,2}})(?::(?P<minute>\d{{2}}))?\s*(?P<meridiem>[ap]\.?(?:m)\.?)\b",
    re.I,
)


def parse_filmforum_datetime(value: str) -> datetime | None:
    match = FILMFORUM_DATETIME_RE.search(value)
    if not match:
        return None
    values = match.groupdict()
    try:
        hour, minute = parse_time(values["hour"], values.get("minute") or "0", values["meridiem"])
        return datetime(
            int(values["year"]),
            MONTHS[values["month"].lower()[:3]],
            int(values["day"]),
            hour,
            minute,
            tzinfo=LA_TZ,
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_old_town_showtime(value: str, current: datetime, default_year: int | None = None) -> datetime | None:
    match = OLD_TOWN_SHOWTIME_RE.search(value)
    if not match:
        return None
    values = match.groupdict()
    try:
        month = MONTHS[values["month"].lower()[:3]]
        year = default_year or (current.year + 1 if month < current.month else current.year)
        hour, minute = parse_time(values["hour"], values.get("minute") or "0", values["meridiem"])
        return datetime(year, month, int(values["day"]), hour, minute, tzinfo=LA_TZ)
    except (KeyError, TypeError, ValueError):
        return None


def parse_ucla_datetime_candidates(parser: PageParser, current: datetime) -> list[datetime]:
    candidates = _academy_visible_datetime_candidates(parser)
    if candidates:
        return candidates
    return academy_datetime_candidates("", parser, current)


def parse_fine_arts_sessions(html: str, now: datetime | None = None) -> list[dict]:
    """Parse Fine Arts' official Veezi schedule using the shared session parser."""

    return parse_veezi_session_feed(html, now)


def discover_filmforum_entries(html: str, base_url: str = FILMFORUM_SCHEDULE_URL) -> list[dict]:
    """Parse Filmforum's schedule cards into canonical date/title entries."""

    parser = parse_html(html)
    lines = parser.lines
    entries: list[dict] = []
    used: set[tuple[str, str]] = set()
    for index, line in enumerate(lines):
        start_at = parse_filmforum_datetime(line)
        if not start_at:
            continue
        title = ""
        for candidate in reversed(lines[max(0, index - 5):index]):
            if normalize_key(candidate).startswith(("date:", "start date:", "location:", "details", "buy tickets")):
                continue
            if is_probable_screening_title(candidate):
                title = candidate
                break
        if not title:
            continue
        location = ""
        for candidate in lines[index + 1:index + 5]:
            if normalize_key(candidate).startswith("location:"):
                location = normalize_space(candidate.split(":", 1)[1])
                break
        page_url = base_url
        title_key = screening_title_key(title)
        for link in parser.links:
            candidate_url = urljoin(base_url, link.get("href", ""))
            parsed = urlsplit(candidate_url)
            link_key = screening_title_key(link.get("text", ""))
            if parsed.netloc.lower() in {"lafilmforum.org", "www.lafilmforum.org"} and (
                link_key == title_key or title_key in link_key
            ):
                page_url = candidate_url.split("#", 1)[0]
                break
        key = (title_key, start_at.isoformat())
        if key in used:
            continue
        used.add(key)
        entries.append(
            {
                "title": title,
                "startAt": start_at,
                "pageUrl": page_url,
                # The schedule contains many independent Buy Tickets links.
                # Do not choose one globally: the detail page is the only
                # reliable association between this program and its DICE/
                # Eventbrite link.
                "ticketUrl": "",
                "location": location,
            }
        )
    return entries


def parse_filmforum_detail(html: str, page_url: str) -> dict:
    parser = parse_html(html)
    title = next(
        (heading["text"] for heading in parser.headings if heading.get("tag") == "h1" and heading.get("text")),
        "",
    )
    formats = []
    for token in re.findall(r"\b(?:35|70|16)mm\b|\b(?:DCP|digital|video)\b", " ".join(parser.lines), re.I):
        token = token.upper() if token.upper() in {"DCP", "DIGITAL", "VIDEO"} else token.lower()
        if token not in formats:
            formats.append(token)
    notes = " · ".join(
        dict.fromkeys(
            line for line in parser.lines
            if any(marker in normalize_key(line) for marker in ("in person", "premiere", "q&a", "introduced by"))
        )
    )[:220]
    return {
        "title": title,
        "year": next((match for match in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", " ".join(parser.lines)) if int(match) < 2027), ""),
        "format": " / ".join(formats),
        "synopsis": " ".join(parser.lines[1:])[:520],
        "notes": notes,
        "ticketUrl": pick_ticket_url(parser, page_url),
        "image": (absolute_images(parser, page_url) or [""])[0],
    }


def discover_old_town_movie_urls(html: str, base_url: str = OLD_TOWN_HOME_URL) -> list[str]:
    parser = parse_html(html)
    urls: list[str] = []
    for link in parser.links:
        candidate = urljoin(base_url, link.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"oldtownmusichall.org", "www.oldtownmusichall.org"}:
            continue
        if not parsed.path.rstrip("/").startswith("/movie/"):
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def parse_old_town_movie(html: str, page_url: str, now: datetime | None = None) -> dict:
    current = as_la(now)
    parser = parse_html(html)
    title = next(
        (heading["text"] for heading in parser.headings if heading.get("tag") == "h1" and heading.get("text")),
        normalize_space(parser.meta.get("og:title", "")).split("|", 1)[0],
    )
    if not title:
        return {"title": "", "showtimes": []}
    documents = academy_json_documents(html)
    iso_dates = re.findall(r"20\d{2}-\d{2}-\d{2}", html)
    default_year = int(iso_dates[0][:4]) if iso_dates else current.year
    showtimes: list[dict] = []
    seen: set[str] = set()
    for line in parser.lines:
        start_at = parse_old_town_showtime(line, current, default_year)
        if not start_at or start_at <= current or start_at.isoformat() in seen:
            continue
        seen.add(start_at.isoformat())
        ticket_url = page_url
        for link in parser.links:
            if parse_old_town_showtime(link.get("text", ""), current, default_year) == start_at:
                ticket_url = urljoin(page_url, link.get("href", ""))
                break
        showtimes.append({"startAt": start_at, "ticketUrl": ticket_url})
    lines_text = " ".join(parser.lines)
    format_name = " / ".join(
        dict.fromkeys(
            token.upper() if token.upper() in {"35MM", "70MM"} else token
            for token in re.findall(r"\b(?:35mm|70mm|16mm|DCP|Digital|Silent)\b", lines_text, re.I)
        )
    )
    notes = " · ".join(
        dict.fromkeys(
            line for line in parser.lines
            if any(marker in normalize_key(line) for marker in ("live accompaniment", "mighty wurlitzer", "introduced by", "sing-along"))
        )
    )[:220]
    year = next(
        (match for match in re.findall(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", lines_text) if int(match) != default_year),
        "",
    )
    return {
        "title": title,
        "showtimes": showtimes,
        "year": year,
        "format": format_name,
        "synopsis": academy_value(documents, ("description", "shortDescription", "summary")),
        "notes": notes,
        "ticketUrl": pick_ticket_url(parser, page_url),
        "image": (absolute_images(parser, page_url) or [""])[0],
    }


UCLA_HOSTS = {"cinema.ucla.edu", "www.cinema.ucla.edu"}


def _ucla_normalize_url(value: str, base_url: str) -> str:
    parsed = urlsplit(urljoin(base_url, unescape(value).strip()).split("#", 1)[0])
    if parsed.scheme != "https" or parsed.netloc.lower() not in UCLA_HOSTS:
        return ""
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))


def discover_ucla_sitemap_urls(html: str, base_url: str) -> list[str]:
    """Return child sitemap URLs from a sitemap index.

    UCLA's calendar is rendered by JavaScript, and the public sitemap may be
    either a URL set or a sitemap index depending on the site's deployment.
    Keep this parser deliberately small and namespace-independent.
    """

    urls: list[str] = []
    for match in re.finditer(r"<loc[^>]*>(.*?)</loc>", html, re.I | re.S):
        candidate = _ucla_normalize_url(match.group(1), base_url)
        if not candidate or candidate in urls:
            continue
        path = urlsplit(candidate).path.lower()
        if "sitemap" in path or path.endswith((".xml", ".xml.gz")):
            urls.append(candidate)
    return urls


def discover_ucla_event_urls(html: str, base_url: str = UCLA_EVENTS_URL) -> list[str]:
    parser = parse_html(html)
    candidates = [urljoin(base_url, link.get("href", "")) for link in parser.links]
    candidates.extend(
        unescape(match.group(1)).strip()
        for match in re.finditer(r"<loc[^>]*>(.*?)</loc>", html, re.I | re.S)
    )
    candidates.extend(
        urljoin(base_url, match.group(0))
        for match in re.finditer(
            r"(?:https?://[^\"'<> ]+)?/events/[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=-]+",
            html.replace("\\/", "/"),
        )
    )
    urls: list[str] = []
    for candidate in candidates:
        normalized = _ucla_normalize_url(candidate, base_url)
        if not normalized:
            continue
        parsed = urlsplit(normalized)
        if parsed.path.rstrip("/") in {"/events", "/events/"} or not parsed.path.rstrip("/").startswith("/events/"):
            continue
        normalized = urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/") + "/", "", ""))
        if normalized not in urls:
            urls.append(normalized)
    return urls


def ucla_labelled_screening(parser: PageParser) -> bool:
    lines = [normalize_key(line) for line in parser.lines]
    return any(
        "screening" in line
        or "family flicks" in line
        or "film archive" in line
        or "film program" in line
        for line in lines
    )


def parse_ucla_event(html: str, page_url: str, now: datetime | None = None) -> dict | None:
    current = as_la(now)
    parser = parse_html(html)
    if not urlsplit(page_url).path.rstrip("/").startswith("/events/") or not ucla_labelled_screening(parser):
        return None
    title = next(
        (heading["text"] for heading in parser.headings if heading.get("tag") == "h1" and heading.get("text")),
        normalize_space(parser.meta.get("og:title", "")).split("|", 1)[0],
    )
    if not title:
        return None
    dates = parse_ucla_datetime_candidates(parser, current)
    if not dates:
        return None
    venue = next(
        (line for line in parser.lines if "theater" in normalize_key(line) or "theatre" in normalize_key(line)),
        "Billy Wilder Theater",
    )
    lines_text = " ".join(parser.lines)
    formats = []
    for token in re.findall(r"\b(?:35|70|16)mm\b|\b(?:DCP|Digital|Video)\b", lines_text, re.I):
        normalized = token.upper() if token.upper() in {"DCP", "DIGITAL", "VIDEO"} else token.lower()
        if normalized not in formats:
            formats.append(normalized)
    notes = " · ".join(
        dict.fromkeys(
            line for line in parser.lines
            if any(marker in normalize_key(line) for marker in ("in person", "presented by", "free admission", "first come"))
        )
    )[:220]
    return make_event(
        source_id="ucla-film-archive",
        source_name="UCLA Film & Television Archive",
        title=title,
        start_at=dates[0],
        now=current,
        page_url=page_url,
        ticket_url=pick_ticket_url(parser, page_url),
        venue=venue,
        year=first_label_value(parser.lines, ("Year", "Release year")),
        format_name=" / ".join(formats),
        synopsis=" ".join(parser.lines[1:])[:520],
        notes=notes,
        image=(absolute_images(parser, page_url) or [""])[0],
        source_page_url=UCLA_EVENTS_URL,
        detail_status="verified-ucla-archive",
    )


VISTA_MONTH_LINE_RE = re.compile(rf"^(?P<month>{MONTH_PATTERN})(?:\s+(?P<year>20\d{{2}}))?$", re.I)
VISTA_DAY_LINE_RE = re.compile(r"^(?P<day>\d{1,2})(?:st|nd|rd|th)?$", re.I)
VISTA_COMBINED_DATE_RE = re.compile(
    rf"\b(?P<month>{MONTH_PATTERN})\s*,?\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{{2}}))?\b",
    re.I,
)
VISTA_NUMERIC_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>20\d{2}))?\b")
VISTA_TIME_RE = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>[ap]\.?m\.?)", re.I)


def parse_vista_showtimes(lines: list[str], now: datetime | None = None) -> list[dict]:
    """Parse Vista's month/day blocks into timezone-aware showtimes.

    Vista renders each movie's schedule as a sequence of month, ordinal day,
    and one or more linked time labels.  Keeping the parser line-oriented
    makes it resilient to the site's visual layout and its repeated showtime
    links.
    """

    current = as_la(now)
    month_number: int | None = None
    year = current.year
    day: int | None = None
    pending_note = ""
    last_date_key: tuple[int, int, int] | None = None
    results: list[dict] = []
    seen: set[str] = set()

    for index, raw_line in enumerate(lines):
        line = normalize_space(raw_line)
        if not line:
            continue

        combined = VISTA_COMBINED_DATE_RE.search(line)
        numeric = VISTA_NUMERIC_DATE_RE.search(line)
        month_match = VISTA_MONTH_LINE_RE.fullmatch(line)
        day_match = VISTA_DAY_LINE_RE.fullmatch(line)

        if combined:
            month_name = combined.group("month")
            month_number = MONTHS[month_name.lower()[:3]]
            year = int(combined.group("year") or current.year)
            day = int(combined.group("day"))
        elif numeric:
            month_number = int(numeric.group("month"))
            day = int(numeric.group("day"))
            year = int(numeric.group("year") or current.year)
        elif month_match:
            # The schedule page also has month-tab labels near the top. Only
            # treat a month as active when an ordinal day follows immediately.
            lookahead = lines[index + 1 : index + 4]
            if any(VISTA_DAY_LINE_RE.fullmatch(normalize_space(candidate)) for candidate in lookahead):
                month_number = MONTHS[month_match.group("month").lower()[:3]]
                year = int(month_match.group("year") or current.year)
                day = None
        elif day_match and month_number:
            day = int(day_match.group("day"))

        if month_number and day:
            date_key = (year, month_number, day)
            if date_key != last_date_key:
                pending_note = ""
                last_date_key = date_key

        lowered = line.casefold()
        if "midnight" in lowered:
            pending_note = "Midnight show"
        elif "matinee" in lowered:
            pending_note = "Matinee"
        elif "sold out" in lowered:
            pending_note = "Sold out"

        if not month_number or not day:
            continue
        for time_match in VISTA_TIME_RE.finditer(line):
            hour, minute = parse_time(time_match.group("hour"), time_match.group("minute"), time_match.group("meridiem"))
            try:
                start_at = datetime(year, month_number, day, hour, minute, tzinfo=LA_TZ)
            except ValueError:
                continue
            if start_at <= current:
                continue
            key = start_at.isoformat()
            if key in seen:
                continue
            seen.add(key)
            results.append({"startAt": start_at, "notes": pending_note})

    return results


def parse_vista_detail(html: str, page_url: str, now: datetime | None = None) -> dict:
    """Extract one Vista movie's metadata and all upcoming showtimes."""

    parser = parse_html(html)
    headings = [heading["text"] for heading in parser.headings if heading["text"]]
    ignored = {"Monthly Programming", "Showtimes", "Summary", "Matinee", "Midnight"}
    title = next(
        (
            heading
            for heading in headings
            if len(heading) > 2 and heading not in ignored and not normalize_key(heading).startswith("vista theater")
        ),
        "",
    )
    if not title:
        # Current Vista movie pages omit the film name from the heading tree;
        # the canonical /movies/<slug>/ URL is the remaining stable title
        # identifier. Keep this fallback deliberately local to Vista so a
        # layout change cannot turn a generic page heading into a movie title.
        slug = urlsplit(page_url).path.rstrip("/").split("/")[-1]
        if slug and slug.casefold() not in {"movies", "movie"}:
            title = normalize_space(slug.replace("-", " ").replace("_", " ")).title()
            title = re.sub(r"(['’])([A-Z])", lambda match: f"{match.group(1)}{match.group(2).lower()}", title)
    if not title:
        return {"title": "", "showtimes": [], "films": [], "pageUrl": page_url}

    lines = parser.lines
    title_index = next((index for index, line in enumerate(lines) if normalize_key(line) == normalize_key(title)), -1)
    schedule_lines = lines[:title_index] if title_index > 0 else lines
    showtimes = parse_vista_showtimes(schedule_lines, now)
    section = _section_for_title(lines, title, [title])
    if not section:
        # Current pages place the title in the URL and start the metadata at a
        # ``Summary`` marker rather than repeating the title in visible text.
        summary_index = next((index for index, line in enumerate(lines) if normalize_key(line).rstrip(":") == "summary"), -1)
        section = lines[summary_index + 1 :] if summary_index >= 0 else lines
    metadata_line = next((line for line in section if re.search(r"\b(?:19|20)\d{2}\s*\|", line)), "")
    year_match = re.search(r"\b((?:19|20)\d{2})\b", metadata_line)
    format_match = re.search(r"\|\s*([^|]*?(?:mm|presentation|digital)[^|]*)", metadata_line, re.I)
    synopsis = _detail_synopsis(section)
    if metadata_line:
        synopsis = normalize_space(synopsis.replace(metadata_line, ""))
    notes = next(
        (
            line[:220]
            for line in section
            if any(marker in normalize_key(line) for marker in ("q&a", "filmmaker in person", "sold out"))
        ),
        "",
    )
    images = absolute_images(parser, page_url)
    return {
        "title": title,
        "year": year_match.group(1) if year_match else "",
        "format": normalize_space(format_match.group(1)) if format_match else "",
        "director": first_label_value(section, ("Director", "Directed by")),
        "synopsis": synopsis,
        "notes": notes,
        "showtimes": showtimes,
        "ticketUrl": pick_ticket_url(parser, page_url),
        "image": images[0] if images else "",
        "pageUrl": page_url,
    }


def select_film_detail(details: dict, title: str) -> dict:
    wanted = normalize_key(title)
    films = details.get("films") or []
    for film in films:
        if normalize_key(film.get("title")) == wanted or wanted in normalize_key(film.get("title")) or normalize_key(film.get("title")) in wanted:
            return film
    return films[0] if len(films) == 1 else {}


def scrape_los_angeles_sequential(now: datetime | None = None, fetcher: CachedFetcher | None = None, force: bool = False) -> dict:
    """Fetch official LA sources and return normalized events + health."""

    current = as_la(now)
    fetcher = fetcher or CachedFetcher()
    all_events: list[dict] = []
    sources: list[dict] = []
    errors: list[str] = []

    # American Cinematheque: discover event links, then validate each event page.
    ac_events: list[dict] = []
    ac_errors: list[str] = []
    ac_checked_at = iso_utc()
    discovered: list[str] = []
    fetched = 0
    try:
        discovery_html, ac_checked_at, _ = fetcher.fetch(AC_NOW_SHOWING_URL, force=force)
        discovered = discover_american_cinematheque_urls(discovery_html)
    except Exception as error:
        # Keep walking the known seeds even when the index page is blocked or
        # temporarily down; a single 403 should not hide all screenings.
        ac_errors.append(f"Index: {error}")
    queue = list(dict.fromkeys([*AC_SEED_URLS, *discovered]))
    initial_discovered = set(discovered)
    seen_urls: set[str] = set()
    # Revalidate the index on a manual refresh, but reuse fresh detail pages;
    # showtimes come from the index and detail artwork/metadata need not make
    # every click wait on a second full crawl. Series pages and related event
    # pages are expanded in bounded batches for parity with the production
    # concurrent adapter.
    while queue and len(seen_urls) < DEFAULT_MAX_AC_EVENTS:
        batch: list[str] = []
        while queue and len(seen_urls) + len(batch) < DEFAULT_MAX_AC_EVENTS:
            page_url = queue.pop(0)
            if page_url in seen_urls or page_url in batch:
                continue
            batch.append(page_url)
        seen_urls.update(batch)
        page_results = parallel_fetch(fetcher, batch, force=force, max_workers=16)
        for page_url in batch:
            result = page_results.get(page_url)
            if isinstance(result, Exception):
                if not (initial_discovered and page_url in set(AC_SEED_URLS) and page_url not in initial_discovered):
                    ac_errors.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            html, checked_at, _ = result
            fetched += 1
            if urlsplit(page_url).path.rstrip("/").startswith("/series/"):
                continue
            event = parse_american_cinematheque_event(html, page_url, current)
            if event:
                event["sourceLastChecked"] = checked_at or ac_checked_at
                ac_events.append(event)
            for related_url in discover_american_cinematheque_urls(html, page_url):
                if related_url not in seen_urls and related_url not in queue:
                    queue.append(related_url)
    sources.append(
        {
            "id": "american-cinematheque",
            "name": "American Cinematheque",
            "city": "los-angeles",
            "url": AC_NOW_SHOWING_URL,
            "adapter": "american_cinematheque",
            "lastChecked": ac_checked_at,
            "status": "verified" if ac_events else ("degraded" if ac_errors else "verified"),
            "eventCount": len(ac_events),
            "pagesChecked": fetched,
            "errors": ac_errors[:5],
        }
    )
    all_events.extend(ac_events)
    errors.extend(ac_errors[:5])

    # New Beverly: the schedule page is the source of truth for showtimes;
    # detail pages enrich each title with director/year/format/synopsis.
    bev_events: list[dict] = []
    bev_errors: list[str] = []
    bev_checked_at = iso_utc()
    try:
        schedule_html, bev_checked_at, _ = fetcher.fetch(NEW_BEV_SCHEDULE_URL, force=force)
        entries = parse_new_beverly_schedule(schedule_html, NEW_BEV_SCHEDULE_URL, current)
        detail_cache: dict[str, dict] = {}
        detail_results = parallel_fetch(
            fetcher,
            (entry["pageUrl"] for entry in entries),
            force=False,
            max_workers=12,
        )
        for entry in entries:
            page_url = entry["pageUrl"]
            details = detail_cache.get(page_url)
            if details is None:
                result = detail_results.get(page_url)
                if isinstance(result, Exception):
                    bev_errors.append(f"{page_url}: {result}")
                    details = {"films": [], "ticketUrl": page_url, "image": "", "checkedAt": bev_checked_at}
                elif result:
                    detail_html, checked_at, _ = result
                    details = parse_new_beverly_detail(detail_html, page_url)
                    details["checkedAt"] = checked_at
                else:
                    details = {"films": [], "ticketUrl": page_url, "image": "", "checkedAt": bev_checked_at}
                detail_cache[page_url] = details
            film = select_film_detail(details, entry["title"])
            image = details.get("image", "")
            if details.get("images") and len(details["images"]) > 1:
                entry_key = normalize_key(entry["title"])
                index = next((i for i, candidate in enumerate(details.get("films", [])) if normalize_key(candidate.get("title")) in entry_key or entry_key in normalize_key(candidate.get("title"))), 0)
                image = details["images"][min(index, len(details["images"]) - 1)]
            event = make_event(
                source_id="new-beverly",
                source_name="New Beverly Cinema",
                title=entry["title"],
                start_at=entry["startAt"],
                now=current,
                page_url=page_url,
                ticket_url=details.get("ticketUrl", page_url),
                venue="New Beverly Cinema",
                director=film.get("director", ""),
                year=film.get("year", ""),
                format_name=film.get("format", ""),
                synopsis=film.get("synopsis", ""),
                notes=entry.get("notes", ""),
                image=image,
                source_page_url=NEW_BEV_SCHEDULE_URL,
                detail_status="verified" if film else "verified-schedule",
            )
            event["sourceLastChecked"] = details.get("checkedAt", bev_checked_at)
            bev_events.append(event)
        sources.append(
            {
                "id": "new-beverly",
                "name": "New Beverly Cinema",
                "city": "los-angeles",
                "url": NEW_BEV_SCHEDULE_URL,
                "adapter": "new_beverly",
                "lastChecked": bev_checked_at,
                "status": "verified" if not bev_errors else "degraded",
                "eventCount": len(bev_events),
                "pagesChecked": 1 + len(detail_cache),
                "errors": bev_errors[:5],
            }
        )
    except Exception as error:
        message = f"New Beverly Cinema: {error}"
        errors.append(message)
        sources.append(
            {
                "id": "new-beverly",
                "name": "New Beverly Cinema",
                "city": "los-angeles",
                "url": NEW_BEV_SCHEDULE_URL,
                "adapter": "new_beverly",
                "lastChecked": bev_checked_at,
                "status": "error",
                "eventCount": 0,
                "pagesChecked": 0,
                "errors": [message],
            }
        )
    all_events.extend(bev_events)
    errors.extend(bev_errors[:5])

    # Vista Theater Hollywood: its monthly schedule links to one official
    # movie page per title. Those detail pages carry the complete date/time
    # run, film metadata, poster, and ticket link.
    vista_events: list[dict] = []
    vista_errors: list[str] = []
    vista_checked_at = iso_utc()
    vista_detail_cache: dict[str, dict] = {}
    try:
        schedule_html, vista_checked_at, _ = fetcher.fetch(VISTA_SCHEDULE_URL, force=force)
        movie_urls = discover_vista_movie_urls(schedule_html)
        if not movie_urls:
            # Some versions of the site expose the same movie links only on
            # the home page; use it as a conservative discovery fallback.
            home_html, _, _ = fetcher.fetch(VISTA_HOME_URL, force=force)
            movie_urls = discover_vista_movie_urls(home_html, VISTA_HOME_URL)
        movie_urls = movie_urls[: max(1, DEFAULT_MAX_DETAIL_EVENTS)]
        detail_results = parallel_fetch(fetcher, movie_urls, force=False, max_workers=12)
        for page_url in movie_urls:
            result = detail_results.get(page_url)
            if isinstance(result, Exception):
                vista_errors.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            detail_html, checked_at, _ = result
            try:
                details = parse_vista_detail(detail_html, page_url, current)
                details["checkedAt"] = checked_at
                vista_detail_cache[page_url] = details
            except Exception as error:
                vista_errors.append(f"{page_url}: {error}")
                continue
            if not details.get("title") or not details.get("showtimes"):
                continue
            for showtime in details["showtimes"]:
                event = make_event(
                    source_id="vista-theater",
                    source_name="Vista Theater Hollywood",
                    title=details["title"],
                    start_at=showtime["startAt"],
                    now=current,
                    page_url=page_url,
                    ticket_url=details.get("ticketUrl", page_url),
                    venue="Vista Theater Hollywood",
                    director=details.get("director", ""),
                    year=details.get("year", ""),
                    format_name=details.get("format", ""),
                    synopsis=details.get("synopsis", ""),
                    notes=" · ".join(filter(None, (details.get("notes", ""), showtime.get("notes", "")))),
                    image=details.get("image", ""),
                    source_page_url=VISTA_SCHEDULE_URL,
                    detail_status="verified",
                )
                event["sourceLastChecked"] = details.get("checkedAt", vista_checked_at)
                vista_events.append(event)
        sources.append(
            {
                "id": "vista-theater",
                "name": "Vista Theater Hollywood",
                "city": "los-angeles",
                "url": VISTA_HOME_URL,
                "scheduleUrl": VISTA_SCHEDULE_URL,
                "adapter": "vista_theater",
                "lastChecked": vista_checked_at,
                "status": "verified" if not vista_errors else "degraded",
                "eventCount": len(vista_events),
                "pagesChecked": 1 + len(vista_detail_cache),
                "errors": vista_errors[:5],
            }
        )
    except Exception as error:
        message = f"Vista Theater Hollywood: {error}"
        errors.append(message)
        sources.append(
            {
                "id": "vista-theater",
                "name": "Vista Theater Hollywood",
                "city": "los-angeles",
                "url": VISTA_HOME_URL,
                "scheduleUrl": VISTA_SCHEDULE_URL,
                "adapter": "vista_theater",
                "lastChecked": vista_checked_at,
                "status": "error",
                "eventCount": 0,
                "pagesChecked": 0,
                "errors": [message],
            }
        )
    all_events.extend(vista_events)
    errors.extend(vista_errors[:5])

    # A page can be reachable from several related-event sections. Keep one
    # record per venue/source/title/start time and sort chronologically.
    deduped: dict[tuple[str, str, str, str], dict] = {}
    for event in all_events:
        key = (
            event.get("sourceName", ""),
            event.get("venue", ""),
            normalize_key(event.get("title", "")),
            event.get("startAt", ""),
        )
        deduped[key] = event
    normalized_events = sorted(deduped.values(), key=lambda item: item.get("startAt", ""))
    return {
        "screenings": normalized_events,
        "sources": sources,
        "errors": errors,
        "checkedAt": iso_utc(),
        "sourceCount": len(sources),
        "successfulSources": sum(1 for source in sources if source.get("status") == "verified"),
        "networkRequests": getattr(fetcher, "network_requests", 0),
        "cacheHits": getattr(fetcher, "cache_hits", 0),
    }


def _scrape_american_cinematheque(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    """Index American Cinematheque without blocking the other venues."""

    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    discovered: list[str] = []
    discovery_errors: list[str] = []
    discovery_pages = 0
    try:
        discovery_html, checked_at, _ = fetcher.fetch(AC_NOW_SHOWING_URL, force=force)
        discovery_pages += 1
        discovered = discover_american_cinematheque_urls(discovery_html)
    except Exception as error:
        discovery_errors.append(f"Now Showing index: {error}")
    # The public Now Showing route can arrive without its event cards when the
    # site's front-end layer is slow. The homepage carries the same upcoming
    # event links in server-rendered HTML and is a fast, official fallback.
    if not discovered:
        try:
            home_html, home_checked_at, _ = fetcher.fetch(AC_HOME_URL, force=force)
            discovery_pages += 1
            checked_at = home_checked_at or checked_at
            discovered = discover_american_cinematheque_urls(home_html)
        except Exception as error:
            discovery_errors.append(f"Homepage index: {error}")
    if discovery_errors and not discovered:
        errors.extend(discovery_errors)
    if not discovered:
        errors.append("Index: no screening pages discovered")
    pages_checked = 0
    initial_discovered = set(discovered)
    queue = list(dict.fromkeys([*AC_SEED_URLS, *discovered]))
    seen_urls: set[str] = set()
    series_entries: list[dict] = []
    calendar_event_urls: set[str] = set()
    detail_errors: list[tuple[str, str]] = []
    while queue and len(seen_urls) < DEFAULT_MAX_AC_EVENTS:
        batch: list[str] = []
        while queue and len(seen_urls) + len(batch) < DEFAULT_MAX_AC_EVENTS:
            page_url = queue.pop(0)
            if page_url in seen_urls or page_url in batch:
                continue
            batch.append(page_url)
        seen_urls.update(batch)
        page_results = parallel_fetch(fetcher, batch, force=force, max_workers=16)
        for page_url in batch:
            result = page_results.get(page_url)
            if isinstance(result, Exception):
                # Seed URLs are only a fallback for a blocked/changed index.
                # Once the official index yields a URL, an obsolete seed should
                # not invalidate an otherwise complete current source. Every
                # URL discovered from the live index or a related event remains
                # verification-critical because it may contain another show.
                if not (initial_discovered and page_url in set(AC_SEED_URLS) and page_url not in initial_discovered):
                    detail_errors.append((page_url, str(result)))
                continue
            if not result:
                continue
            html, page_checked_at, _ = result
            pages_checked += 1
            is_series_page = urlsplit(page_url).path.rstrip("/").startswith("/series/")
            if is_series_page:
                try:
                    calendar_event_urls.update(
                        url
                        for url in discover_american_cinematheque_urls(html, page_url)
                        if urlsplit(url).path.rstrip("/").startswith("/now-showing/")
                    )
                    series_entries.extend(parse_american_cinematheque_series(html, page_url, current))
                except Exception as error:
                    errors.append(f"{page_url}: series calendar parse failed: {error}")
            event = None if is_series_page else parse_american_cinematheque_event(html, page_url, current)
            if event:
                event["sourceLastChecked"] = page_checked_at or checked_at
                events.append(event)

            # AC event pages expose additional upcoming screenings in a
            # Related Events section. Expand that official graph in bounded
            # batches so an event such as Two-Lane Blacktop is not lost just
            # because the public Now Showing index is client-rendered.
            for related_url in discover_american_cinematheque_urls(html, page_url):
                if related_url not in seen_urls and related_url not in queue:
                    queue.append(related_url)

    # AC's official series cards are the schedule authority: they contain the
    # exact date, time, title, and venue for each calendar entry. Detail pages
    # enrich those records, but are not allowed to erase a valid calendar card
    # when one is slow or structurally changed.
    series_by_key: dict[tuple[str, str, str], dict] = {}
    for entry in series_entries:
        series_by_key[(normalize_key(entry["title"]), entry["venue"], entry["startAt"].isoformat())] = entry
    series_by_page = {entry.get("pageUrl"): entry for entry in series_entries if entry.get("pageUrl")}
    for event in events:
        key = (
            normalize_key(event.get("title", "")),
            event.get("venue", ""),
            event.get("startAt", ""),
        )
        series_entry = series_by_page.get(event.get("sourceUrl")) or series_by_key.get(key)
        if not series_entry:
            continue
        venue_data = venue_info(series_entry["venue"], "American Cinematheque")
        event.update(
            {
                "venue": venue_data["name"],
                "neighborhood": venue_data["neighborhood"],
                "address": venue_data["address"],
                "distance": venue_data["neighborhood"],
                "sourcePageUrl": series_entry["sourcePageUrl"],
            }
        )
    event_keys = {
        (normalize_key(event.get("title", "")), event.get("venue", ""), event.get("startAt", ""))
        for event in events
    }
    for key, entry in series_by_key.items():
        if key in event_keys:
            continue
        events.append(
            make_event(
                source_id="american-cinematheque",
                source_name="American Cinematheque",
                title=entry["title"],
                start_at=entry["startAt"],
                now=current,
                page_url=entry["pageUrl"],
                ticket_url=entry["pageUrl"],
                venue=entry["venue"],
                notes=entry.get("notes", ""),
                source_page_url=entry["sourcePageUrl"],
                detail_status="verified-series-calendar",
            )
        )
    for page_url, message in detail_errors:
        if page_url in calendar_event_urls:
            warnings.append(f"{page_url}: detail enrichment unavailable: {message}")
        else:
            errors.append(f"{page_url}: {message}")
    events = deduplicate_events(events)
    source = {
        "id": "american-cinematheque",
        "name": "American Cinematheque",
        "city": "los-angeles",
        "url": AC_NOW_SHOWING_URL,
        "adapter": "american_cinematheque",
        "lastChecked": checked_at,
        "status": "verified" if not errors else "degraded",
        "eventCount": len(events),
        "pagesChecked": discovery_pages + pages_checked,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_academy_museum(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    """Traverse the Academy calendar and retain only Screening-labelled events."""

    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    event_urls: list[str] = []
    calendar_queue = [ACADEMY_CALENDAR_URL]
    seen_calendar: set[str] = set()
    pages_checked = 0

    # The programTypes query is the official site's Screening filter. Follow
    # its pagination/load-more links so the source is not limited to the first
    # visible batch of cards.
    while calendar_queue and len(seen_calendar) < 40:
        page_url = calendar_queue.pop(0)
        if page_url in seen_calendar:
            continue
        seen_calendar.add(page_url)
        try:
            html, checked_at, _ = fetcher.fetch(page_url, force=force)
        except Exception as error:
            errors.append(f"{page_url}: {error}")
            continue
        pages_checked += 1
        event_urls.extend(discover_academy_event_urls(html, page_url))
        for traversal_url in discover_academy_traversal_urls(html, page_url):
            if traversal_url not in seen_calendar and traversal_url not in calendar_queue:
                calendar_queue.append(traversal_url)

    # The calendar is client-rendered on some deployments. Its public Films
    # index and current series pages expose the same Screening detail graph;
    # use them only when the filtered calendar yielded no detail URLs.
    if not event_urls:
        try:
            films_html, films_checked_at, _ = fetcher.fetch(ACADEMY_FILMS_URL, force=force)
            checked_at = films_checked_at or checked_at
            pages_checked += 1
            series_urls = discover_academy_series_urls(films_html, ACADEMY_FILMS_URL)
            series_results = parallel_fetch(fetcher, series_urls, force=force, max_workers=8)
            pages_checked += len(series_urls)
            for series_url in series_urls:
                result = series_results.get(series_url)
                if isinstance(result, Exception) or not result:
                    if isinstance(result, Exception):
                        warnings.append(f"{series_url}: {result}")
                    continue
                series_html, series_checked_at, _ = result
                checked_at = series_checked_at or checked_at
                event_urls.extend(discover_academy_event_urls(series_html, series_url))
        except Exception as error:
            errors.append(f"{ACADEMY_FILMS_URL}: {error}")

    if not event_urls:
        sitemap_queue = list(ACADEMY_SITEMAP_URLS)
        checked_sitemaps: set[str] = set()
        while sitemap_queue and len(checked_sitemaps) < 12 and not event_urls:
            sitemap_url = sitemap_queue.pop(0)
            if sitemap_url in checked_sitemaps:
                continue
            checked_sitemaps.add(sitemap_url)
            try:
                sitemap_html, sitemap_checked_at, _ = fetcher.fetch(sitemap_url, force=force)
                checked_at = sitemap_checked_at or checked_at
                pages_checked += 1
                event_urls.extend(discover_academy_event_urls(sitemap_html, sitemap_url))
                for child_url in discover_academy_sitemap_urls(sitemap_html, sitemap_url):
                    if child_url not in checked_sitemaps and child_url not in sitemap_queue:
                        sitemap_queue.append(child_url)
            except Exception as error:
                warnings.append(f"{sitemap_url}: {error}")

    unique_event_urls = list(dict.fromkeys(event_urls))[:DEFAULT_MAX_AC_EVENTS]
    detail_results = parallel_fetch(fetcher, unique_event_urls, force=force, max_workers=16)
    events: list[dict] = []
    for page_url in unique_event_urls:
        result = detail_results.get(page_url)
        if isinstance(result, Exception):
            warnings.append(f"{page_url}: {result}")
            continue
        if not result:
            continue
        html, page_checked_at, _ = result
        event = parse_academy_event(html, page_url, current)
        if event:
            event["sourceLastChecked"] = page_checked_at or checked_at
            events.append(event)
        elif academy_labelled_screening(html):
            warnings.append(f"{page_url}: Screening page had no future date/time")

    events = deduplicate_events(events)
    source = {
        "id": "academy-museum",
        "name": "Academy Museum of Motion Pictures",
        "city": "los-angeles",
        "url": ACADEMY_CALENDAR_URL,
        "adapter": "academy_museum",
        "lastChecked": checked_at,
        "status": "verified" if events and not errors else ("degraded" if events else "error"),
        "eventCount": len(events),
        "pagesChecked": pages_checked + len(unique_event_urls),
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_fine_arts(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    try:
        html, checked_at, _ = fetcher.fetch(FINE_ARTS_SESSIONS_URL, force=force)
        entries = parse_fine_arts_sessions(html, current)
        if not entries:
            errors.append("Veezi sessions: no upcoming screenings discovered")
        for entry in entries:
            event = make_event(
                source_id="fine-arts-theatre",
                source_name="Fine Arts Theatre Beverly Hills",
                title=entry["title"],
                start_at=entry["startAt"],
                now=current,
                page_url=FINE_ARTS_SESSIONS_URL,
                ticket_url=FINE_ARTS_SESSIONS_URL,
                venue="Fine Arts Theatre Beverly Hills",
                format_name=entry.get("format", ""),
                notes=entry.get("notes", ""),
                source_page_url=FINE_ARTS_SESSIONS_URL,
                detail_status="verified-fine-arts-veezi",
            )
            event["sourceLastChecked"] = checked_at
            events.append(event)
    except Exception as error:
        errors.append(f"Fine Arts Theatre Beverly Hills: {error}")
    source = {
        "id": "fine-arts-theatre",
        "name": "Fine Arts Theatre Beverly Hills",
        "city": "los-angeles",
        "url": FINE_ARTS_SESSIONS_URL,
        "adapter": "fine_arts_theatre",
        "required": False,
        "lastChecked": checked_at,
        "status": "verified" if events and not errors else ("degraded" if events else "error"),
        "eventCount": len(events),
        "pagesChecked": 1 if events or errors else 0,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_filmforum(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    pages_checked = 0
    try:
        schedule_html, checked_at, _ = fetcher.fetch(FILMFORUM_SCHEDULE_URL, force=force)
        pages_checked += 1
        entries = discover_filmforum_entries(schedule_html)
        detail_urls = list(dict.fromkeys(entry["pageUrl"] for entry in entries if entry.get("pageUrl") != FILMFORUM_SCHEDULE_URL))[:DEFAULT_MAX_DETAIL_EVENTS]
        details = parallel_fetch(fetcher, detail_urls, force=force, max_workers=12)
        detail_cache: dict[str, dict] = {}
        for page_url in detail_urls:
            result = details.get(page_url)
            if isinstance(result, Exception):
                warnings.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            detail_html, page_checked_at, _ = result
            pages_checked += 1
            detail_cache[page_url] = {**parse_filmforum_detail(detail_html, page_url), "checkedAt": page_checked_at}
        for entry in entries:
            detail = detail_cache.get(entry.get("pageUrl", ""), {})
            venue = entry.get("location") or "Los Angeles Filmforum"
            event = make_event(
                source_id="filmforum",
                source_name="Los Angeles Filmforum",
                title=entry["title"],
                start_at=entry["startAt"],
                now=current,
                page_url=entry.get("pageUrl") or FILMFORUM_SCHEDULE_URL,
                ticket_url=detail.get("ticketUrl") or entry.get("ticketUrl") or entry.get("pageUrl") or FILMFORUM_SCHEDULE_URL,
                venue=venue,
                year=detail.get("year", ""),
                format_name=detail.get("format", ""),
                synopsis=detail.get("synopsis", ""),
                notes=detail.get("notes", ""),
                image=detail.get("image", ""),
                source_page_url=FILMFORUM_SCHEDULE_URL,
                detail_status="verified-filmforum",
            )
            event["sourceLastChecked"] = detail.get("checkedAt", checked_at)
            events.append(event)
        if not entries:
            errors.append("Schedule: no dated programs discovered")
    except Exception as error:
        errors.append(f"Los Angeles Filmforum: {error}")
    events = deduplicate_events(events)
    source = {
        "id": "filmforum",
        "name": "Los Angeles Filmforum",
        "city": "los-angeles",
        "url": FILMFORUM_SCHEDULE_URL,
        "adapter": "filmforum",
        "required": False,
        "lastChecked": checked_at,
        "status": "verified" if events and not errors else ("degraded" if events else "error"),
        "eventCount": len(events),
        "pagesChecked": pages_checked,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_old_town_music_hall(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    pages_checked = 0
    try:
        home_html, checked_at, _ = fetcher.fetch(OLD_TOWN_HOME_URL, force=force)
        pages_checked += 1
        movie_urls = discover_old_town_movie_urls(home_html)[:DEFAULT_MAX_DETAIL_EVENTS]
        results = parallel_fetch(fetcher, movie_urls, force=force, max_workers=12)
        for page_url in movie_urls:
            result = results.get(page_url)
            if isinstance(result, Exception):
                warnings.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            html, page_checked_at, _ = result
            pages_checked += 1
            details = parse_old_town_movie(html, page_url, current)
            for showtime in details.get("showtimes", []):
                event = make_event(
                    source_id="old-town-music-hall",
                    source_name="Old Town Music Hall",
                    title=details["title"],
                    start_at=showtime["startAt"],
                    now=current,
                    page_url=page_url,
                    ticket_url=showtime.get("ticketUrl") or details.get("ticketUrl") or page_url,
                    venue="Old Town Music Hall",
                    year=details.get("year", ""),
                    format_name=details.get("format", ""),
                    synopsis=details.get("synopsis", ""),
                    notes=details.get("notes", ""),
                    image=details.get("image", ""),
                    source_page_url=OLD_TOWN_HOME_URL,
                    detail_status="verified-old-town-music-hall",
                )
                event["sourceLastChecked"] = page_checked_at or checked_at
                events.append(event)
        if not movie_urls:
            errors.append("Home page: no movie pages discovered")
    except Exception as error:
        errors.append(f"Old Town Music Hall: {error}")
    events = deduplicate_events(events)
    source = {
        "id": "old-town-music-hall",
        "name": "Old Town Music Hall",
        "city": "los-angeles",
        "url": OLD_TOWN_HOME_URL,
        "adapter": "old_town_music_hall",
        "required": False,
        "lastChecked": checked_at,
        "status": "verified" if events and not errors else ("degraded" if events else "error"),
        "eventCount": len(events),
        "pagesChecked": pages_checked,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_ucla_film_archive(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    pages_checked = 0
    try:
        event_urls: list[str] = []
        for index_url in (UCLA_EVENTS_URL, UCLA_SERIES_URL):
            try:
                index_html, index_checked_at, _ = fetcher.fetch(index_url, force=force)
                checked_at = index_checked_at or checked_at
                pages_checked += 1
                event_urls.extend(discover_ucla_event_urls(index_html, index_url))
            except Exception as error:
                warnings.append(f"{index_url}: {error}")
        if not event_urls:
            # The calendar and series indexes are client-rendered. Walk the
            # site's sitemap variants and any child sitemaps so a deployment
            # change from a URL set to a sitemap index does not make UCLA
            # appear unavailable.
            sitemap_queue = list(UCLA_SITEMAP_URLS)
            checked_sitemaps: set[str] = set()
            while sitemap_queue and len(checked_sitemaps) < 12 and not event_urls:
                sitemap_url = sitemap_queue.pop(0)
                if sitemap_url in checked_sitemaps:
                    continue
                checked_sitemaps.add(sitemap_url)
                try:
                    sitemap_html, sitemap_checked_at, _ = fetcher.fetch(sitemap_url, force=force)
                    checked_at = sitemap_checked_at or checked_at
                    pages_checked += 1
                    event_urls.extend(discover_ucla_event_urls(sitemap_html, UCLA_EVENTS_URL))
                    for child_url in discover_ucla_sitemap_urls(sitemap_html, sitemap_url):
                        if child_url not in checked_sitemaps and child_url not in sitemap_queue:
                            sitemap_queue.append(child_url)
                except Exception as error:
                    warnings.append(f"{sitemap_url}: {error}")
        event_urls = list(dict.fromkeys(event_urls))[:DEFAULT_MAX_AC_EVENTS]
        results = parallel_fetch(fetcher, event_urls, force=force, max_workers=16)
        for page_url in event_urls:
            result = results.get(page_url)
            if isinstance(result, Exception):
                warnings.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            html, page_checked_at, _ = result
            pages_checked += 1
            event = parse_ucla_event(html, page_url, current)
            if event:
                event["sourceLastChecked"] = page_checked_at or checked_at
                events.append(event)
        if not event_urls:
            errors.append("Events index: no event pages discovered")
    except Exception as error:
        errors.append(f"UCLA Film & Television Archive: {error}")
    events = deduplicate_events(events)
    source = {
        "id": "ucla-film-archive",
        "name": "UCLA Film & Television Archive",
        "city": "los-angeles",
        "url": UCLA_EVENTS_URL,
        "adapter": "ucla_film_archive",
        "required": False,
        "lastChecked": checked_at,
        "status": "verified" if events and not errors else ("degraded" if events else "error"),
        "eventCount": len(events),
        "pagesChecked": pages_checked,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_new_beverly(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    """Index New Beverly from Veezi and optionally enrich with venue pages.

    Veezi is the canonical source for dates, times, sold-out status, and
    program notes. The New Beverly schedule and program pages are optional
    enrichment only, so a marketing-site failure cannot invalidate the
    ticketing feed.
    """

    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    detail_cache: dict[str, dict] = {}
    session_entries: list[dict] = []
    source_mode = "veezi"
    try:
        try:
            sessions_html, checked_at, _ = fetcher.fetch(NEW_BEV_SESSIONS_URL, force=force)
            session_entries = parse_veezi_session_feed(sessions_html, current)
            if not session_entries:
                warnings.append("Veezi sessions: no upcoming screenings discovered; using official schedule fallback")
                source_mode = "official-schedule-fallback"
        except Exception as error:
            warnings.append(f"Veezi sessions unavailable: {error}; using official schedule fallback")
            source_mode = "official-schedule-fallback"

        # Veezi remains canonical whenever it returns a usable schedule. The
        # public New Beverly schedule is an official, bounded fallback for a
        # slow or blocked ticketing host, and remains separately identified in
        # source diagnostics.
        entries: list[dict] = []
        if source_mode == "official-schedule-fallback":
            schedule_html, schedule_checked_at, _ = fetcher.fetch(NEW_BEV_SCHEDULE_URL, force=force)
            checked_at = schedule_checked_at or checked_at
            entries = parse_new_beverly_schedule(schedule_html, NEW_BEV_SCHEDULE_URL, current)
            session_entries = entries
            if not entries:
                errors.append("Official schedule fallback: no upcoming screenings discovered")
        else:
            try:
                schedule_html, _, _ = fetcher.fetch(NEW_BEV_SCHEDULE_URL, force=False)
                entries = parse_new_beverly_schedule(schedule_html, NEW_BEV_SCHEDULE_URL, current)
            except Exception as error:
                warnings.append(f"{NEW_BEV_SCHEDULE_URL}: {error}")

        detail_urls = list(dict.fromkeys(entry["pageUrl"] for entry in entries))
        detail_results = parallel_fetch(
            fetcher,
            detail_urls,
            force=False,
            max_workers=12,
        )
        for page_url in detail_urls:
            result = detail_results.get(page_url)
            if isinstance(result, Exception):
                warnings.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            detail_html, page_checked_at, _ = result
            try:
                details = parse_new_beverly_detail(detail_html, page_url)
            except Exception as error:
                warnings.append(f"{page_url}: {error}")
                continue
            details["checkedAt"] = page_checked_at
            detail_cache[page_url] = details

        details_by_title: dict[str, dict] = {}
        for details in detail_cache.values():
            for film in details.get("films", []):
                title = film.get("title")
                if title:
                    details_by_title[normalize_key(title)] = {**details, **film}

        for entry in session_entries:
            details = details_by_title.get(normalize_key(entry["title"]), {})
            fallback_entry = source_mode == "official-schedule-fallback"
            event_page_url = entry.get("pageUrl") if fallback_entry else NEW_BEV_SESSIONS_URL
            source_page_url = NEW_BEV_SCHEDULE_URL if fallback_entry else NEW_BEV_SESSIONS_URL
            event = make_event(
                source_id="new-beverly",
                source_name="New Beverly Cinema",
                title=entry["title"],
                start_at=entry["startAt"],
                now=current,
                page_url=event_page_url or NEW_BEV_SESSIONS_URL,
                ticket_url=details.get("ticketUrl") or event_page_url or NEW_BEV_SESSIONS_URL,
                venue="New Beverly Cinema",
                director=details.get("director", ""),
                year=details.get("year", ""),
                format_name=entry.get("format") or details.get("format", ""),
                synopsis=details.get("synopsis", ""),
                notes=" · ".join(filter(None, (details.get("notes", ""), entry.get("notes", "")))),
                image=details.get("image", ""),
                source_page_url=source_page_url,
                detail_status="verified-schedule-fallback" if fallback_entry else "verified",
            )
            event["sourceLastChecked"] = checked_at
            events.append(event)
    except Exception as error:
        errors.append(f"New Beverly Cinema: {error}")
    source = {
        "id": "new-beverly",
        "name": "New Beverly Cinema",
        "city": "los-angeles",
        "url": NEW_BEV_SESSIONS_URL,
        "scheduleUrl": NEW_BEV_SCHEDULE_URL,
        "sessionsUrl": NEW_BEV_SESSIONS_URL,
        "adapter": "new_beverly",
        "required": False,
        "sourceMode": source_mode,
        "lastChecked": checked_at,
        "status": "verified" if not errors else "degraded",
        "eventCount": len(events),
        "pagesChecked": 1 + (1 if session_entries else 0) + len(detail_cache),
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_vista(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    """Index Vista from Veezi's canonical ticketing session feed.

    Vista's marketing pages are useful for artwork, but their markup has
    changed repeatedly and can omit titles or showtimes. Veezi is the system
    that actually owns the dates, times, sold-out state, and special formats,
    so failures enriching a poster must never invalidate a valid session feed.
    """

    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    detail_cache: dict[str, dict] = {}
    session_entries: list[dict] = []
    try:
        sessions_html, checked_at, _ = fetcher.fetch(VISTA_SESSIONS_URL, force=force)
        session_entries = parse_vista_session_feed(sessions_html, current)
        if not session_entries:
            errors.append("Veezi sessions: no upcoming screenings discovered")

        # Enrichment is best-effort. Veezi remains the source of truth even if
        # a Vista movie page is missing, blocked, or changes its markup again.
        titles = list(dict.fromkeys(entry["title"] for entry in session_entries))
        movie_urls = [urljoin(VISTA_HOME_URL, f"movies/{slugify(title)}/") for title in titles[:DEFAULT_MAX_DETAIL_EVENTS]]
        detail_results = parallel_fetch(fetcher, movie_urls, force=False, max_workers=12)
        for page_url in movie_urls:
            result = detail_results.get(page_url)
            if isinstance(result, Exception):
                warnings.append(f"{page_url}: {result}")
                continue
            if not result:
                continue
            detail_html, page_checked_at, _ = result
            try:
                details = parse_vista_detail(detail_html, page_url, current)
            except Exception as error:
                warnings.append(f"{page_url}: {error}")
                continue
            details["checkedAt"] = page_checked_at
            detail_cache[page_url] = details
        details_by_title = {
            normalize_key(details.get("title")): details
            for details in detail_cache.values()
            if details.get("title")
        }
        for entry in session_entries:
            details = details_by_title.get(normalize_key(entry["title"]), {})
            event = make_event(
                source_id="vista-theater",
                source_name="Vista Theater Hollywood",
                title=entry["title"],
                start_at=entry["startAt"],
                now=current,
                page_url=VISTA_SESSIONS_URL,
                ticket_url=VISTA_SESSIONS_URL,
                venue="Vista Theater Hollywood",
                director=details.get("director", ""),
                year=details.get("year", ""),
                format_name=entry.get("format") or details.get("format") or "35mm / 70mm",
                synopsis=details.get("synopsis", ""),
                notes=" · ".join(filter(None, (details.get("notes", ""), entry.get("notes", "")))),
                image=details.get("image", ""),
                source_page_url=VISTA_SESSIONS_URL,
                detail_status="verified",
            )
            event["sourceLastChecked"] = details.get("checkedAt", checked_at)
            events.append(event)
    except Exception as error:
        errors.append(f"Vista Theater Hollywood: {error}")
    source = {
        "id": "vista-theater",
        "name": "Vista Theater Hollywood",
        "city": "los-angeles",
        "url": VISTA_SESSIONS_URL,
        "scheduleUrl": VISTA_SCHEDULE_URL,
        "sessionsUrl": VISTA_SESSIONS_URL,
        "adapter": "vista_theater",
        "lastChecked": checked_at,
        "status": "verified" if not errors else "degraded",
        "eventCount": len(events),
        "pagesChecked": 1 + len(detail_cache) if session_entries or detail_cache else 0,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def _scrape_vidiots(current: datetime, fetcher: CachedFetcher, force: bool) -> dict:
    """Index Vidiots from official pages, with a labeled TeenTix fallback.

    The coming-soon page is authoritative when it server-renders dates and
    times. Movie pages are then best-effort enrichment; if the index omits
    showtimes, they become the conservative fallback source. If the official
    site is blocked or structurally unavailable, TeenTix LA's Vidiots partner
    listing can supply a clearly labeled, optional fallback feed.
    """

    started = time.monotonic()
    checked_at = iso_utc()
    errors: list[str] = []
    warnings: list[str] = []
    events: list[dict] = []
    detail_cache: dict[str, dict] = {}
    movie_urls: list[str] = []
    listing_entries: list[dict] = []
    entries: list[dict] = []
    source_mode = "official"
    source_page_url = VIDIOTS_HOME_URL
    pages_checked = 0

    # Traverse from the official homepage into its current upcoming listings,
    # then into each linked movie page. Individual movie pages are the most
    # specific source for dates and times; listing cards remain a fallback for
    # pages that are temporarily unavailable or omit their showtime markup.
    listing_pages = [VIDIOTS_HOME_URL, VIDIOTS_URL]
    seen_listing_pages: set[str] = set()
    while listing_pages and len(seen_listing_pages) < 4:
        listing_page = listing_pages.pop(0)
        if listing_page in seen_listing_pages:
            continue
        seen_listing_pages.add(listing_page)
        try:
            listing_html, page_checked_at, _ = fetcher.fetch(listing_page, force=force)
            pages_checked += 1
            if listing_page == VIDIOTS_HOME_URL:
                checked_at = page_checked_at or checked_at
                listing_pages.extend(discover_vidiots_traversal_urls(listing_html, listing_page))
            elif source_page_url == VIDIOTS_HOME_URL:
                source_page_url = listing_page
            movie_urls.extend(discover_vidiots_movie_urls(listing_html, listing_page))
            listing_entries.extend(parse_vidiots_listing(listing_html, listing_page, current))
        except Exception as error:
            warnings.append(f"{listing_page}: {error}")

    movie_urls = list(dict.fromkeys(movie_urls))[:DEFAULT_MAX_DETAIL_EVENTS]
    if not movie_urls:
        warnings.append("Official Vidiots: no official movie pages discovered")
    detail_results = parallel_fetch(fetcher, movie_urls, force=force, max_workers=12)
    for page_url in movie_urls:
        result = detail_results.get(page_url)
        if isinstance(result, Exception):
            warnings.append(f"{page_url}: {result}")
            continue
        if not result:
            warnings.append(f"{page_url}: empty response")
            continue
        detail_html, page_checked_at, _ = result
        pages_checked += 1
        try:
            details = parse_vidiots_movie_detail(detail_html, page_url, current)
        except Exception as error:
            warnings.append(f"{page_url}: {error}")
            continue
        details["checkedAt"] = page_checked_at
        detail_cache[page_url] = details

    detail_entries = [
        {
            "title": details.get("title") or _vidiots_title_from_url(page_url),
            "pageUrl": page_url,
            "startAt": showtime["startAt"],
            "notes": showtime.get("notes", ""),
            "ticketUrl": showtime.get("ticketUrl", ""),
        }
        for page_url, details in detail_cache.items()
        if details.get("title") and details.get("showtimes")
        for showtime in details["showtimes"]
    ]
    detail_pages_with_schedule = {entry["pageUrl"] for entry in detail_entries}
    if detail_entries:
        # Do not let a less-specific listing card duplicate or override the
        # authoritative schedule from the same official movie page.
        entries = detail_entries + [
            entry for entry in listing_entries if entry.get("pageUrl") not in detail_pages_with_schedule
        ]
    else:
        entries = listing_entries
    if not entries:
        warnings.append("Official Vidiots: no upcoming showtimes found")

    unique_entries: dict[tuple[str, str], dict] = {}
    for entry in entries:
        key = (
            screening_title_key(entry.get("title", "")),
            entry.get("startAt").isoformat() if entry.get("startAt") else "",
        )
        current_entry = unique_entries.get(key)
        if current_entry is None or (
            entry.get("pageUrl") in detail_pages_with_schedule
            and current_entry.get("pageUrl") not in detail_pages_with_schedule
        ):
            unique_entries[key] = entry
    entries = list(unique_entries.values())

    # The official site currently blocks some server environments. TeenTix's
    # partner venue page is a public listing of the same Vidiots program and is
    # used only when the official adapter cannot produce any upcoming events.
    if not entries:
        try:
            partner_html, partner_checked_at, _ = fetcher.fetch(VIDIOTS_PARTNER_URL, force=force)
            checked_at = partner_checked_at
            pages_checked += 1
            partner_entries = parse_vidiots_partner_listing(partner_html, VIDIOTS_PARTNER_URL, current)
            if partner_entries:
                entries = partner_entries
                source_mode = "partner-fallback"
                source_page_url = VIDIOTS_PARTNER_URL
                warnings.append("Official Vidiots unavailable; using TeenTix LA partner listing")
            else:
                errors.append("TeenTix LA Vidiots partner listing: no upcoming showtimes found")
        except Exception as error:
            errors.append(f"TeenTix LA Vidiots partner listing: {error}")

    for entry in entries:
        page_url = entry["pageUrl"]
        details = detail_cache.get(page_url, {})
        start_at = entry["startAt"]
        is_partner = source_mode == "partner-fallback"
        event = make_event(
            source_id="vidiots",
            source_name="Vidiots",
            title=entry["title"],
            start_at=start_at,
            now=current,
            page_url=page_url,
            ticket_url=entry.get("ticketUrl") or details.get("ticketUrl") or page_url,
            venue="Vidiots",
            director=details.get("director", ""),
            year=details.get("year", ""),
            format_name=details.get("format", ""),
            synopsis=details.get("synopsis", ""),
            notes=" · ".join(filter(None, (details.get("notes", ""), entry.get("notes", "")))),
            image=details.get("image", ""),
            source_page_url=source_page_url,
            detail_status="partner-fallback" if is_partner else "verified",
        )
        event["sourceLastChecked"] = details.get("checkedAt", checked_at)
        if is_partner:
            event["sourceLabel"] = "Partner listing · TeenTix LA / Vidiots"
            event["synopsis"] = "Partner listing from TeenTix LA for Vidiots. Check the event page for current ticket availability."
        events.append(event)

    if not events and not errors:
        errors.append("Vidiots official and TeenTix fallback: no upcoming showtimes found")
    source = {
        "id": "vidiots",
        "name": "Vidiots",
        "city": "los-angeles",
        "url": VIDIOTS_HOME_URL,
        "scheduleUrl": VIDIOTS_URL,
        "partnerUrl": VIDIOTS_PARTNER_URL,
        "adapter": "vidiots",
        "required": False,
        "sourceMode": source_mode,
        "lastChecked": checked_at,
        "status": "verified" if events and not errors else "degraded",
        "eventCount": len(events),
        "pagesChecked": pages_checked,
        "durationMs": int((time.monotonic() - started) * 1000),
        "errors": errors[:5],
        "warnings": warnings[:5],
    }
    return {"events": events, "source": source, "errors": errors[:5]}


def scrape_los_angeles(now: datetime | None = None, fetcher: CachedFetcher | None = None, force: bool = False) -> dict:
    """Fetch all configured official LA sources concurrently and return one verified feed."""

    current = as_la(now)
    fetcher = fetcher or CachedFetcher()
    jobs = {
        "american-cinematheque": _scrape_american_cinematheque,
        "new-beverly": _scrape_new_beverly,
        "vista-theater": _scrape_vista,
        "vidiots": _scrape_vidiots,
        "academy-museum": _scrape_academy_museum,
        "fine-arts-theatre": _scrape_fine_arts,
        "filmforum": _scrape_filmforum,
        "old-town-music-hall": _scrape_old_town_music_hall,
        "ucla-film-archive": _scrape_ucla_film_archive,
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="rerun-source") as executor:
        futures = {
            source_id: executor.submit(job, current, fetcher, force)
            for source_id, job in jobs.items()
        }
        for source_id, future in futures.items():
            try:
                results[source_id] = future.result()
            except Exception as error:
                name = {
                    "american-cinematheque": "American Cinematheque",
                    "new-beverly": "New Beverly Cinema",
                    "vista-theater": "Vista Theater Hollywood",
                    "vidiots": "Vidiots",
                    "academy-museum": "Academy Museum of Motion Pictures",
                    "fine-arts-theatre": "Fine Arts Theatre Beverly Hills",
                    "filmforum": "Los Angeles Filmforum",
                    "old-town-music-hall": "Old Town Music Hall",
                    "ucla-film-archive": "UCLA Film & Television Archive",
                }[source_id]
                results[source_id] = {
                    "events": [],
                    "errors": [f"{name}: {error}"],
                    "source": {
                        "id": source_id,
                        "name": name,
                        "city": "los-angeles",
                        "status": "error",
                        "required": SOURCE_REGISTRY.get(source_id, {}).get("required", True),
                        "eventCount": 0,
                        "pagesChecked": 0,
                        "errors": [f"{name}: {error}"],
                    },
                }

    all_events: list[dict] = []
    sources: list[dict] = []
    errors: list[str] = []
    for source_id in jobs:
        result = results[source_id]
        all_events.extend(result.get("events", []))
        source = result["source"]
        source.setdefault("required", SOURCE_REGISTRY.get(source_id, {}).get("required", True))
        sources.append(source)
        errors.extend(result.get("errors", []))

    normalized_events = deduplicate_events(all_events)
    return {
        "screenings": normalized_events,
        "sources": sources,
        "errors": errors[:15],
        "checkedAt": iso_utc(),
        "sourceCount": len(sources),
        "successfulSources": sum(1 for source in sources if source.get("status") == "verified"),
        "requiredSourceCount": sum(1 for source in sources if source.get("required", True)),
        "successfulRequiredSources": sum(
            1 for source in sources if source.get("required", True) and source.get("status") == "verified"
        ),
        "blockingErrors": [
            error
            for source in sources
            if source.get("required", True)
            for error in source.get("errors", [])
        ][:15],
        "optionalErrors": [
            error
            for source in sources
            if not source.get("required", True)
            for error in source.get("errors", [])
        ][:15],
        "networkRequests": getattr(fetcher, "network_requests", 0),
        "cacheHits": getattr(fetcher, "cache_hits", 0),
    }


if __name__ == "__main__":
    result = scrape_los_angeles(force=True)
    print(f"Fetched {len(result['screenings'])} upcoming Los Angeles screenings from {len(result['sources'])} official sources.")
    for source in result["sources"]:
        print(f"- {source['name']}: {source['eventCount']} events ({source['status']})")
