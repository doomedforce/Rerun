import sys
import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scraper


LA = scraper.LA_TZ
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=LA)


AC_HTML = """
<html><head><meta property="og:image" content="https://assets.example/crash.jpg"></head>
<h1>CRASH</h1><p>FRI SEP 11, 2026 7:30 PM</p>
<p>Aero Theatre | 30th Anniversary</p><p>RELEASED IN: 1996</p>
<p>DIRECTED BY: David Cronenberg</p><h3>ABOUT THE FILM:</h3>
<p>A car crash victim becomes involved with an underground sub-culture.</p>
<p>FORMAT: 35mm</p><h3>Locations</h3><h4>Aero Theatre</h4>
<p>1328 Montana Ave</p><a href="https://tickets.example/crash">Tickets</a></html>
"""

SCHEDULE_HTML = """
<html><h1>Monthly Programming</h1><h2>September 2026</h2>
<a href="/program/september-8-rocky-bound-for-glory/">Tue, September 08 7:30 pm / 10:00 pm Rocky (Garrett Brown In Person) / Bound for Glory More Info / Buy Tickets</a>
<a href="/program/september-11-magnolia-friday-matinee/">Fri, September 11 2:00 pm Magnolia More Info / Buy Tickets</a></html>
"""

DETAIL_HTML = """
<html><h1>Special Event</h1><p>September 08, 2026</p>
<h3>Rocky (Garrett Brown In Person)</h3><p>7:30 pm</p>
<h3>Bound for Glory</h3><p>10:00 pm</p>
<h2>Rocky (Garrett Brown In Person)</h2><p>Underdog boxer.</p>
<p>Director</p><p>John G. Avildsen</p><p>Year</p><p>1976</p><p>Format</p><p>35mm</p>
<h2>Bound for Glory</h2><p>Woody Guthrie.</p>
<p>Director</p><p>Hal Ashby</p><p>Year</p><p>1976</p><p>Format</p><p>35mm</p>
<a href="https://ticketing.uswest.veezi.com">Buy Tickets</a></html>
"""

VISTA_HTML = """
<html>
<h2>Monthly Programming</h2>
<p>September</p><p>7th</p><p>Monday</p>
<a href="https://ticketing.uswest.veezi.com/showtimes/1">1:00 pm</a>
<a href="https://ticketing.uswest.veezi.com/showtimes/2">4:00 pm</a>
<p>September</p><p>8th</p><p>Tuesday</p><p>Matinee</p>
<a href="https://ticketing.uswest.veezi.com/showtimes/3">2:00 pm</a>
<h3>Onslaught</h3><p>2026 | 1h 32m | 35mm Presentation</p>
<p>A rogue squad of super soldiers breaks loose in the desert.</p>
<p>Director</p><p>Adam Wingard</p>
<a href="/movies/onslaught/">More Info</a>
<a href="https://ticketing.uswest.veezi.com">Buy Tickets</a>
</html>
"""

VISTA_LIVE_DETAIL_HTML = """
<html><h3>Showtimes</h3>
<p>September, 8th 2026</p>
<a href="https://ticketing.uswest.veezi.com/showtimes/1">1:00 pm</a>
<a href="https://ticketing.uswest.veezi.com/showtimes/2">4:00 pm</a>
<p>September, 9th 2026</p>
<a href="https://ticketing.uswest.veezi.com/showtimes/3">7:30 pm</a>
<h3>Summary</h3><p>Underdog boxer.</p>
<p>Director</p><p>Adam Wingard</p></html>
"""

VEEZI_SESSIONS_HTML = """
<html><h1>VISTA THEATER</h1><h2>Show Times</h2>
<h3>Tuesday 8, September</h3><h3>Onslaught</h3><p>R for strong bloody violence</p>
<h4>Tuesday 8, September</h4>
<a href="https://ticketing.uswest.veezi.com/ticket/1">1:00 PM</a>
<a href="https://ticketing.uswest.veezi.com/ticket/2">4:00 PM</a>
<h3>Wednesday 9, September</h3><h3>Great Guy/Held for Ransom</h3>
<p>Video Archives 16mm and DVD Screening</p><h4>Wednesday 9, September</h4>
<a href="https://ticketing.uswest.veezi.com/ticket/3">2:00 PM</a>
<h3>Friday 11, September</h3><h3>Streets of Fire</h3><h4>Friday 11, September</h4>
<a href="https://ticketing.uswest.veezi.com/ticket/4">11:59 PM SOLD OUT</a></html>
"""


class FakeFetcher:
    network_requests = 0
    cache_hits = 0

    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url, force=False):
        self.network_requests += 1
        if url not in self.pages:
            raise RuntimeError(f"missing fixture: {url}")
        return self.pages[url], "2026-09-07T19:00:00Z", False


class ScraperTests(unittest.TestCase):
    def test_cached_fetcher_retries_transient_http_failure(self):
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b"<html>ok</html>"

        with TemporaryDirectory() as cache_dir:
            failure = HTTPError("https://example.test/feed", 503, "temporarily unavailable", {"Retry-After": "0"}, None)
            fetcher = scraper.CachedFetcher(
                cache_dir=Path(cache_dir),
                request_gap=0,
                retries=1,
                retry_max_seconds=0,
            )
            with patch.object(scraper, "urlopen", side_effect=[failure, Response()]) as mocked_urlopen:
                html, checked_at, from_cache = fetcher.fetch("https://example.test/feed", force=True)

            self.assertEqual(html, "<html>ok</html>")
            self.assertTrue(checked_at)
            self.assertFalse(from_cache)
            self.assertEqual(mocked_urlopen.call_count, 2)
            self.assertEqual(fetcher.network_requests, 2)

    def test_page_parser_collects_lazy_and_responsive_artwork(self):
        parser = scraper.PageParser()
        parser.feed(
            '<picture><source data-srcset="/poster-large.jpg 2x, /poster.jpg 1x">'
            '<img data-lazy-src="/poster-lazy.jpg" style="background-image:url(\'/still.jpg\')"></picture>'
        )
        self.assertEqual(parser.images, ["/poster-large.jpg", "/poster-lazy.jpg"])

    def test_pacific_fallback_has_correct_daylight_offsets(self):
        fallback = scraper.PacificTimeZone()
        winter = datetime(2026, 1, 15, 12, tzinfo=fallback)
        summer = datetime(2026, 7, 15, 12, tzinfo=fallback)
        self.assertEqual(winter.utcoffset(), timedelta(hours=-8))
        self.assertEqual(summer.utcoffset(), timedelta(hours=-7))
        self.assertEqual(winter.tzname(), "PST")
        self.assertEqual(summer.tzname(), "PDT")

    def test_american_cinematheque_event_extracts_abbreviated_month_and_labels(self):
        event = scraper.parse_american_cinematheque_event(
            AC_HTML,
            "https://www.americancinematheque.com/now-showing/crash-09-11-2026/",
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["startAt"], "2026-09-11T19:30:00-07:00")
        self.assertEqual(event["director"], "David Cronenberg")
        self.assertEqual(event["format"], "35mm")
        self.assertEqual(event["venue"], "Aero Theatre")
        self.assertEqual(event["image"], "https://assets.example/crash.jpg")

    def test_new_beverly_schedule_pairs_double_feature_titles_and_times(self):
        entries = scraper.parse_new_beverly_schedule(SCHEDULE_HTML, scraper.NEW_BEV_SCHEDULE_URL, NOW)
        self.assertEqual([entry["title"] for entry in entries], [
            "Rocky (Garrett Brown In Person)",
            "Bound for Glory",
            "Magnolia",
        ])
        self.assertEqual([entry["startAt"].strftime("%H:%M") for entry in entries], ["19:30", "22:00", "14:00"])

    def test_new_beverly_detail_keeps_each_film_metadata(self):
        details = scraper.parse_new_beverly_detail(DETAIL_HTML, "https://thenewbev.com/program/september-8-rocky-bound-for-glory/")
        self.assertEqual(details["films"][0]["director"], "John G. Avildsen")
        self.assertEqual(details["films"][1]["director"], "Hal Ashby")
        self.assertEqual(details["films"][0]["year"], "1976")
        self.assertEqual(details["ticketUrl"], "https://ticketing.uswest.veezi.com")

    def test_vista_detail_extracts_all_showtimes_and_metadata(self):
        details = scraper.parse_vista_detail(
            VISTA_HTML,
            "https://www.vistatheaterhollywood.com/movies/onslaught/",
            NOW,
        )
        self.assertEqual(details["title"], "Onslaught")
        self.assertEqual(details["year"], "2026")
        self.assertEqual(details["format"], "35mm Presentation")
        self.assertEqual(details["director"], "Adam Wingard")
        self.assertTrue(details["ticketUrl"].startswith("https://ticketing.uswest.veezi.com"))
        self.assertEqual([item["startAt"].strftime("%Y-%m-%d %H:%M") for item in details["showtimes"]], [
            "2026-09-07 13:00",
            "2026-09-07 16:00",
            "2026-09-08 14:00",
        ])
        self.assertEqual(details["showtimes"][-1]["notes"], "Matinee")

    def test_vista_live_detail_handles_comma_dates_and_slug_title(self):
        details = scraper.parse_vista_detail(
            VISTA_LIVE_DETAIL_HTML,
            "https://www.vistatheaterhollywood.com/movies/onslaught/",
            NOW,
        )
        self.assertEqual(details["title"], "Onslaught")
        self.assertEqual([item["startAt"].strftime("%Y-%m-%d %H:%M") for item in details["showtimes"]], [
            "2026-09-08 13:00",
            "2026-09-08 16:00",
            "2026-09-09 19:30",
        ])
        self.assertEqual(details["director"], "Adam Wingard")
        self.assertEqual(details["synopsis"], "Underdog boxer.")

    def test_vista_discovery_restricts_to_official_movie_pages(self):
        html = '<a href="/movies/onslaught/">More Info</a><a href="https://example.com/movies/fake">Fake</a>'
        self.assertEqual(
            scraper.discover_vista_movie_urls(html),
            ["https://www.vistatheaterhollywood.com/movies/onslaught/"],
        )

    def test_vista_adapter_keeps_ticket_feed_verified_when_detail_layout_differs(self):
        pages = {
            scraper.VISTA_SESSIONS_URL: VEEZI_SESSIONS_HTML,
        }
        result = scraper._scrape_vista(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 4)
        self.assertEqual({event["title"] for event in result["events"]}, {"Onslaught", "Great Guy/Held for Ransom", "Streets of Fire"})

    def test_vista_veezi_feed_parses_titles_dates_formats_and_sold_out(self):
        entries = scraper.parse_vista_session_feed(VEEZI_SESSIONS_HTML, NOW)
        self.assertEqual([entry["title"] for entry in entries], [
            "Onslaught", "Onslaught", "Great Guy/Held for Ransom", "Streets of Fire",
        ])
        self.assertEqual(entries[0]["startAt"].strftime("%Y-%m-%d %H:%M"), "2026-09-08 13:00")
        self.assertEqual(entries[2]["format"], "16mm / DVD")
        self.assertEqual(entries[3]["notes"], "Sold out")

    def test_vista_adapter_uses_veezi_as_source_of_truth(self):
        pages = {scraper.VISTA_SESSIONS_URL: VEEZI_SESSIONS_HTML}
        result = scraper._scrape_vista(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 4)
        self.assertEqual(result["events"][0]["sourceUrl"], scraper.VISTA_SESSIONS_URL)

    def test_new_beverly_schedule_stays_verified_when_enrichment_is_unavailable(self):
        result = scraper._scrape_new_beverly(
            NOW,
            FakeFetcher({scraper.NEW_BEV_SCHEDULE_URL: SCHEDULE_HTML}),
            force=True,
        )
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 3)
        self.assertFalse(result["source"]["errors"])
        self.assertEqual(len(result["source"]["warnings"]), 2)

    def test_scrape_combines_official_sources_and_deduplicates(self):
        pages = {
            scraper.AC_NOW_SHOWING_URL: '<a href="/now-showing/crash-09-11-2026/">Crash</a>',
            scraper.NEW_BEV_SCHEDULE_URL: SCHEDULE_HTML,
            scraper.VISTA_SESSIONS_URL: VEEZI_SESSIONS_HTML,
            "https://www.americancinematheque.com/now-showing/crash-09-11-2026/": AC_HTML,
            "https://thenewbev.com/program/september-8-rocky-bound-for-glory/": DETAIL_HTML,
            "https://thenewbev.com/program/september-11-magnolia-friday-matinee/": DETAIL_HTML,
            "https://www.vistatheaterhollywood.com/movies/onslaught/": VISTA_HTML,
        }
        result = scraper.scrape_los_angeles(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(len(result["screenings"]), 8)
        self.assertEqual({event["sourceName"] for event in result["screenings"]}, {"American Cinematheque", "New Beverly Cinema", "Vista Theater Hollywood"})
        self.assertTrue(all(event["city"] == "los-angeles" for event in result["screenings"]))


if __name__ == "__main__":
    unittest.main()
