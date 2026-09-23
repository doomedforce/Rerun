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

AC_EGYPTIAN_HTML = """
<html><p>Aero Theatre</p><p>Los Feliz 3</p>
<h1>The Power of Paranoia: THE VISIT / TRAP</h1><p>MON SEP 28, 2026 7:00 PM</p>
<p>Egyptian Theatre | Q&amp;A with filmmaker M. Night Shyamalan</p>
<h3>ABOUT THE FILMS:</h3><p>Two films about paranoia.</p><p>FORMAT: DCP</p>
<h3>Locations</h3><h4>Egyptian Theatre</h4><p>6712 Hollywood Blvd</p></html>
"""

AC_LOS_FELIZ_HTML = """
<html><p>Aero Theatre</p><p>Egyptian Theatre</p>
<h1>CRASH</h1><p>FRI SEP 11, 2026 7:30 PM</p>
<p>Los Feliz 3 | Introduction by filmmaker</p><p>RELEASED IN: 1996</p>
<p>DIRECTED BY: David Cronenberg</p><h3>ABOUT THE FILM:</h3>
<p>A car crash victim becomes involved with an underground sub-culture.</p>
<p>FORMAT: 35mm</p><h3>Locations</h3><h4>Los Feliz 3</h4>
<p>1822 N Vermont Ave</p></html>
"""

AC_STRAW_DOGS_HTML = """
<html><h1>STRAW DOGS</h1><p>TUE SEP 8, 2026 10:00 PM</p>
<p>Los Feliz 3 | ‘You Can’t Stream That on Television’</p>
<p>RELEASED IN: 1971</p><p>DIRECTED BY: Sam Peckinpah</p>
<p>FORMAT: 35mm</p><h3>Locations</h3><h4>Los Feliz 3</h4>
<p>1822 N Vermont Ave</p><h2>Related Events</h2>
<a href="/now-showing/two-lane-black-top-09-08-2026/">TWO-LANE BLACK TOP</a>
<p>Tue September 8, 2026</p><p>7:30 PM</p><a href="/now-showing/two-lane-black-top-09-08-2026/">Read More</a></html>
"""

AC_TWO_LANE_HTML = """
<html><p>Aero Theatre</p><h1>TWO-LANE BLACK TOP</h1>
<p>TUE SEP 8, 2026 7:30 PM</p><p>Aero Theatre | 55th Anniversary</p>
<p>RELEASED IN: 1971</p><p>DIRECTED BY: Monte Hellman</p><p>FORMAT: DCP</p>
<h3>Locations</h3><h4>Aero Theatre</h4><p>1328 Montana Ave</p></html>
"""

AC_SERIES_HTML = """
<html><h1>Programmed by Mel Brooks: 100 Years at the Movies</h1>
<h3>About the Series</h3><h3>Membership</h3>
<p>WED SEP 9, 2026</p><p>7:30 PM</p><h3>THE ELEPHANT MAN</h3>
<p>Aero Theatre | 'Programmed by Mel Brooks: 100 Years at the Movies'</p>
<a href="/now-showing/the-elephant-man-09-09-2026/">View Details</a>
</html>
"""

AC_MULTI_MONTH_SERIES_HTML = """
<html><h1>Program</h1>
<p>SUN SEP 6, 2026</p><p>7:00 PM</p><h3>PAST FILM</h3><p>Los Feliz 3 | Program</p>
<a href="/now-showing/past-film-09-06-2026/">View Details</a>
<p>TUE OCT 6, 2026</p><p>10:00 PM</p><h3>FUTURE FILM</h3><p>Los Feliz 3 | Program</p>
<a href="/now-showing/future-film-10-06-2026/">View Details</a>
</html>
"""

AC_SERIES_CARD_ASSOCIATION_HTML = """
<html><h1>Program</h1>
<h3>DOUBLE INDEMNITY</h3>
<p>WED SEP 9, 2026</p><p>7:30 PM</p><h3>THE ELEPHANT MAN</h3>
<p>Aero Theatre | Program</p>
<a href="/now-showing/double-indemnity-10-17-2026/">View Details</a>
<p>SAT OCT 17, 2026</p><p>12:00 PM</p><h3>DOUBLE INDEMNITY</h3>
<p>Egyptian Theatre | Program</p>
<a href="/now-showing/the-elephant-man-09-09-2026/">View Details</a>
</html>
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

NEW_BEV_UNTITLED_TIME_HTML = """
<html><h1>Monthly Programming</h1><h2>September 2026</h2>
<a href="/program/september-14-program/">Mon, September 14 7:30 pm More Info / Buy Tickets</a>
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
<a href="https://ticketing.uswest.veezi.com/ticket/1">1:00 PM BOOKINGS CLOSED</a>
<a href="https://ticketing.uswest.veezi.com/ticket/2">4:00 PM</a>
<h3>Wednesday 9, September</h3><h3>Great Guy/Held for Ransom</h3>
<p>Video Archives 16mm and DVD Screening</p><h4>Wednesday 9, September</h4>
<a href="https://ticketing.uswest.veezi.com/ticket/3">2:00 PM</a>
<h3>Friday 11, September</h3><h3>Streets of Fire</h3><h4>Friday 11, September</h4>
<a href="https://ticketing.uswest.veezi.com/ticket/4">11:59 PM SOLD OUT</a></html>
"""

VIDIOTS_INDEX_HTML = """
<html><h1>Coming Soon to Vidiots</h1>
<article><a href="/movies/possession/">Possession</a></article>
<a href="https://example.com/movies/fake/">Fake external page</a></html>
"""

VIDIOTS_INDEX_WITH_SHOWTIME_HTML = """
<html><h1>Coming Soon to Vidiots</h1>
<article><a href="/movies/possession/">Possession</a>
<p>Friday, September 11, 2026</p><a href="/tickets/possession">7:30 PM</a></article></html>
"""

VIDIOTS_HOME_HTML = """
<html><h1>Vidiots</h1>
<a href="/coming-soon/">Coming Soon</a>
<a href="/upcoming-movies/">Upcoming Movies</a>
</html>
"""

VIDIOTS_MULTI_SHOWTIME_DETAIL_HTML = """
<html><h1>Possession</h1>
<p>Friday, September 11, 2026</p><a href="https://tickets.example/possession-730">7:30 PM</a>
<p>Friday, September 11, 2026</p><a href="https://tickets.example/possession-945">9:45 PM</a>
<p>Director: Andrzej Zulawski</p><p>1981</p><p>35mm</p></html>
"""

VIDIOTS_RELATED_SHOWTIME_DETAIL_HTML = """
<html><h1>Only at Seven</h1>
<p>Friday, September 11, 2026</p><a href="https://tickets.example/only-at-seven">7:00 PM</a>
<h2>Related Movies</h2>
<p>Friday, September 11, 2026</p><a href="https://tickets.example/another-movie">9:45 PM</a>
</html>
"""

VIDIOTS_DETAIL_HTML = """
<html><head><meta property="og:image" content="https://assets.example/possession.jpg"></head>
<h1>Possession</h1><p>Friday, September 11, 2026</p>
<a href="https://tickets.example/possession">7:30 PM</a>
<p>Director: Andrzej Zulawski</p><p>1981</p><p>35mm</p>
<p>Special guest in attendance</p></html>
"""

VIDIOTS_PARTNER_HTML = """
<html><h1>Vidiots</h1><h2>Upcoming Events</h2>
<article><h3><a href="/calendar/event/the-traveler/">The Traveler</a></h3>
<p>Saturday, September 12, 2026</p><p>4:30pm</p></article>
<article><h3><a href="/calendar/event/splash/">Splash</a></h3>
<p>Saturday, September 12, 2026</p><p>7:00pm</p></article>
</html>
"""

ACADEMY_CALENDAR_HTML = """
<html><body>
<a href="/programs/detail/far-from-heaven-019fce4c-example">Far from Heaven</a>
<a href="/en/calendar?programTypes=16i3uOYQwism7sMDhIQr2O&page=2">Next</a>
</body></html>
"""

ACADEMY_DETAIL_HTML = """
<html><head>
<meta property="og:image" content="https://assets.example/far-from-heaven.jpg">
<script type="application/json">{"name":"Far from Heaven in 35mm","startDate":"2026-09-23T19:30:00-07:00","endDate":"2026-09-24T19:30:00-07:00","releaseYear":"2002","format":"35mm","description":"A lush drama.","programType":"Screenings","relatedEvents":[{"name":"Related Screening","startDate":"2026-09-15T19:30:00-07:00"}]}</script>
</head><body>
<h1>Far from Heaven in 35mm</h1>
<p>Screening from Series</p><p>Wed, Sep 23, 2026</p>
<p>In person: producer Brad Simpson</p><p>35mm</p>
<a href="https://tickets.example/far-from-heaven">Buy Tickets</a>
</body></html>
"""

ACADEMY_NON_SCREENING_HTML = """
<html><h1>Monster Mash</h1><p>Special Events</p>
<p>Sat, Sep 26, 2026</p><p>A family day at the museum.</p></html>
"""

ACADEMY_RELATED_TIME_HTML = """
<html><body><h1>Own Event Without a Posted Time</h1>
<p>Screening from Series</p><p>Thu, Sep 24, 2026</p>
<h2>More in Series</h2><p>Fri, Sep 25, 2026</p><p>9:45 PM</p>
</body></html>
"""

FINE_ARTS_HTML = """
<html><h2>Fine Arts Theatre Beverly Hills</h2>
<h3>Wednesday 23, September</h3><h3>Citizen Kane</h3>
<h4>Wednesday 23, September</h4><a href="/ticket/1">7:30 PM</a></html>
"""

FILMFORUM_HTML = """
<html><h2><a href="/events/rare-film-2026-09-27/">Rare Film</a></h2>
<p>Date: Sep 27, 2026 7:30PM</p><p>Location: 2220 Arts + Archives</p>
<p>A rare theatrical screening.</p>
<a href="/events/rare-film-2026-09-27/">Details</a>
<a href="https://tickets.example/rare-film">Buy Tickets</a></html>
"""

FILMFORUM_MULTI_HTML = """
<html>
<h2><a href="/events/rare-film-2026-09-27/">Rare Film</a></h2>
<p>Date: Sep 27, 2026 7:30PM</p><p>Location: 2220 Arts + Archives</p>
<a href="/events/rare-film-2026-09-27/">Details</a>
<a href="https://link.dice.fm/rare-film">Buy Tickets</a>
<h2><a href="/events/another-film-2026-10-04/">Another Film</a></h2>
<p>Date: Oct 4, 2026 7:30PM</p><p>Location: 2220 Arts + Archives</p>
<a href="/events/another-film-2026-10-04/">Details</a>
<a href="https://link.dice.fm/another-film">Buy Tickets</a>
</html>
"""

FILMFORUM_RARE_DETAIL_HTML = """
<html><h1>Rare Film</h1><p>Rare film program.</p>
<a href="https://link.dice.fm/correct-rare-film">Buy Tickets</a></html>
"""

FILMFORUM_ANOTHER_DETAIL_HTML = """
<html><h1>Another Film</h1><p>Another film program.</p>
<a href="https://link.dice.fm/correct-another-film">Buy Tickets</a></html>
"""

OLD_TOWN_HTML = """
<html><head><meta property="og:image" content="https://indy-systems.imgix.net/gold-rush.jpg">
<script type="application/ld+json">{"description":"A classic silent film.","date":"2026-10-24"}</script></head>
<body><h1>The Gold Rush</h1><p>1925 - Silent</p>
<p>Live Accompaniment by Robert York on the Mighty Wurlitzer</p>
<a href="/showtimes/gold-rush-1">October 24, 2:30 pm</a></body></html>
"""

UCLA_HTML = """
<html><head><meta property="og:image" content="https://cinema.ucla.edu/dandridge.jpg"></head>
<body><h1>Introducing Dorothy Dandridge</h1><p>Screening 1 of 3</p>
<p>Dec 2, 2026</p><p>7:00 pm</p><p>Billy Wilder Theater</p>
<p>Year</p><p>1999</p><p>Digital</p></body></html>
"""

UCLA_RELATED_TIME_HTML = """
<html><body><h1>Current UCLA Event</h1><p>Screening 1 of 1</p>
<p>Dec 2, 2026</p><p>7:00 pm</p><p>Billy Wilder Theater</p>
<h2>More in this series</h2><p>Dec 3, 2026</p><p>9:45 pm</p><p>Another Theater</p>
</body></html>
"""

UCLA_SERIES_HTML = """
<html><body><h1>Dorothy Dandridge</h1><p>Billy Wilder Theater</p>
<article><h2><a href="/events/introducing-dorothy-dandridge-2026-12-02/">Introducing Dorothy Dandridge</a></h2>
<p>Dec 2, 20267:30 pm</p><p>35mm</p></article>
<article><h2><a href="/events/malaga-decks-ran-red-2026-12-13/">Malaga / Decks Ran Red</a></h2>
<p>Dec 13, 2026</p><p>7:00 pm</p><p>16mm</p></article>
<h2>Stay updated</h2><a href="/events/unrelated-nav-event-2026-12-20/">Unrelated nav event</a>
<p>Dec 20, 2026</p><p>9:45 pm</p></body></html>
"""

UCLA_SITEMAP_INDEX_XML = """
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.cinema.ucla.edu/events-sitemap.xml</loc></sitemap>
</sitemapindex>
"""

UCLA_EVENT_SITEMAP_XML = """
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cinema.ucla.edu/events/introducing-dorothy-dandridge-2026-12-02/</loc></url>
  <url><loc>https://cinema.ucla.edu/series/archive-treasures/</loc></url>
</urlset>
"""

UCLA_EVENT_SITEMAP_WITH_HISTORY_XML = """
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.cinema.ucla.edu/events/past-event-2026-01-01/</loc></url>
  <url><loc>https://www.cinema.ucla.edu/events/introducing-dorothy-dandridge-2026-12-02/</loc></url>
</urlset>
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
    def test_academy_discovery_follows_filtered_calendar_pagination(self):
        self.assertEqual(
            scraper.discover_academy_event_urls(ACADEMY_CALENDAR_HTML),
            ["https://www.academymuseum.org/programs/detail/far-from-heaven-019fce4c-example/"],
        )
        localized = '<a href="/en/programs/detail/the-beguiled-019fc-example">The Beguiled</a>'
        self.assertEqual(
            scraper.discover_academy_event_urls(localized),
            ["https://www.academymuseum.org/en/programs/detail/the-beguiled-019fc-example/"],
        )
        self.assertEqual(
            scraper.discover_academy_traversal_urls(ACADEMY_CALENDAR_HTML),
            ["https://www.academymuseum.org/en/calendar?programTypes=16i3uOYQwism7sMDhIQr2O&page=2"],
        )

    def test_academy_sitemap_index_reaches_detail_pages(self):
        sitemap_index = """
        <sitemapindex><sitemap><loc>https://www.academymuseum.org/program-sitemap.xml</loc></sitemap></sitemapindex>
        """
        program_sitemap = """
        <urlset><url><loc>https://www.academymuseum.org/en/programs/detail/far-from-heaven-019f-example</loc></url></urlset>
        """
        self.assertEqual(
            scraper.discover_academy_sitemap_urls(sitemap_index, "https://www.academymuseum.org/sitemap.xml"),
            ["https://www.academymuseum.org/program-sitemap.xml"],
        )
        pages = {
            scraper.ACADEMY_CALENDAR_URL: "<html></html>",
            scraper.ACADEMY_FILMS_URL: "<html></html>",
            scraper.ACADEMY_SITEMAP_URLS[0]: sitemap_index,
            "https://www.academymuseum.org/program-sitemap.xml": program_sitemap,
            "https://www.academymuseum.org/en/programs/detail/far-from-heaven-019f-example/": ACADEMY_DETAIL_HTML,
        }
        result = scraper._scrape_academy_museum(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(len(result["events"]), 1)

    def test_academy_parser_requires_screenings_label_and_reads_embedded_metadata(self):
        event = scraper.parse_academy_event(
            ACADEMY_DETAIL_HTML,
            "https://www.academymuseum.org/programs/detail/far-from-heaven-019fce4c-example/",
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["venue"], "Academy Museum of Motion Pictures")
        self.assertEqual(event["startAt"], "2026-09-23T19:30:00-07:00")
        self.assertEqual(event["format"], "35mm")
        self.assertEqual(event["sourceStatus"], "verified-academy-calendar")
        localized_event = scraper.parse_academy_event(
            ACADEMY_DETAIL_HTML,
            "https://www.academymuseum.org/en/programs/detail/far-from-heaven-019fce4c-example/",
            NOW,
        )
        self.assertIsNotNone(localized_event)
        self.assertEqual(localized_event["startAt"], "2026-09-23T19:30:00-07:00")
        self.assertIsNone(
            scraper.parse_academy_event(
                ACADEMY_NON_SCREENING_HTML,
                "https://www.academymuseum.org/programs/detail/monster-mash-example/",
                NOW,
            )
        )

    def test_academy_parser_never_uses_a_related_event_time(self):
        event = scraper.parse_academy_event(
            ACADEMY_RELATED_TIME_HTML,
            "https://www.academymuseum.org/programs/detail/own-event-without-a-posted-time/",
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["startAt"], "2026-09-24T12:00:00-07:00")
        self.assertEqual(event["time"], "Time TBA")
        self.assertEqual(event["ticketStatus"], "details")
        self.assertEqual(event["sourceStatus"], "verified-academy-date-only")
        self.assertNotIn("9:45", event["notes"])

    def test_academy_scraper_traverses_calendar_and_detail_pages(self):
        calendar_page_2 = '<a href="/programs/detail/second-screening-019f-example">Second Screening</a>'
        pages = {
            scraper.ACADEMY_CALENDAR_URL: ACADEMY_CALENDAR_HTML,
            "https://www.academymuseum.org/en/calendar?programTypes=16i3uOYQwism7sMDhIQr2O&page=2": calendar_page_2,
            "https://www.academymuseum.org/programs/detail/far-from-heaven-019fce4c-example/": ACADEMY_DETAIL_HTML,
            "https://www.academymuseum.org/programs/detail/second-screening-019f-example/": ACADEMY_DETAIL_HTML.replace("Far from Heaven", "Second Screening"),
        }
        result = scraper._scrape_academy_museum(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(len(result["events"]), 2)

    def test_academy_scraper_uses_server_rendered_series_when_calendar_is_empty(self):
        detail_url = "https://www.academymuseum.org/programs/detail/far-from-heaven-019fce4c-example/"
        pages = {
            scraper.ACADEMY_CALENDAR_URL: "<html><p>Data Loading in Progress</p></html>",
            scraper.ACADEMY_FILMS_URL: "<html><h1>Screenings &amp; Series</h1></html>",
            scraper.ACADEMY_SERIES_SEED_URLS[0]: '<a href="/programs/detail/far-from-heaven-019fce4c-example">Far from Heaven</a>',
            detail_url: ACADEMY_DETAIL_HTML,
        }
        result = scraper._scrape_academy_museum(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 1)

    def test_additional_repertory_sources_parse_official_shapes(self):
        fine_arts = scraper.parse_fine_arts_sessions(FINE_ARTS_HTML, NOW)
        self.assertEqual(fine_arts[0]["title"], "Citizen Kane")
        self.assertEqual(fine_arts[0]["startAt"].isoformat(), "2026-09-23T19:30:00-07:00")

        filmforum = scraper.discover_filmforum_entries(FILMFORUM_HTML)
        self.assertEqual(filmforum[0]["title"], "Rare Film")
        self.assertEqual(filmforum[0]["location"], "2220 Arts + Archives")
        self.assertTrue(filmforum[0]["pageUrl"].endswith("/events/rare-film-2026-09-27/"))

        old_town = scraper.parse_old_town_movie(
            OLD_TOWN_HTML,
            "https://www.oldtownmusichall.org/movie/the-gold-rush/",
            NOW,
        )
        self.assertEqual(old_town["title"], "The Gold Rush")
        self.assertEqual(old_town["year"], "1925")
        self.assertEqual(old_town["showtimes"][0]["startAt"].isoformat(), "2026-10-24T14:30:00-07:00")

        ucla = scraper.parse_ucla_event(
            UCLA_HTML,
            "https://www.cinema.ucla.edu/events/introducing-dorothy-dandridge-2026-12-02/",
            NOW,
        )
        self.assertIsNotNone(ucla)
        self.assertEqual(ucla["venue"], "Billy Wilder Theater")
        self.assertEqual(ucla["startAt"], "2026-12-02T19:00:00-08:00")

    def test_filmforum_ticket_links_are_resolved_from_each_detail_page(self):
        entries = scraper.discover_filmforum_entries(FILMFORUM_MULTI_HTML)
        self.assertEqual([entry["ticketUrl"] for entry in entries], ["", ""])
        pages = {
            scraper.FILMFORUM_SCHEDULE_URL: FILMFORUM_MULTI_HTML,
            "https://www.lafilmforum.org/events/rare-film-2026-09-27/": FILMFORUM_RARE_DETAIL_HTML,
            "https://www.lafilmforum.org/events/another-film-2026-10-04/": FILMFORUM_ANOTHER_DETAIL_HTML,
        }
        result = scraper._scrape_filmforum(NOW, FakeFetcher(pages), force=True)
        ticket_urls = {event["title"]: event["ticketUrl"] for event in result["events"]}
        self.assertEqual(ticket_urls["Rare Film"], "https://link.dice.fm/correct-rare-film")
        self.assertEqual(ticket_urls["Another Film"], "https://link.dice.fm/correct-another-film")

    def test_ucla_sitemap_index_reaches_event_urls(self):
        self.assertEqual(
            scraper.discover_ucla_sitemap_urls(UCLA_SITEMAP_INDEX_XML, scraper.UCLA_SITEMAP_URL),
            ["https://www.cinema.ucla.edu/events-sitemap.xml"],
        )
        self.assertEqual(
            scraper.discover_ucla_event_urls(UCLA_EVENT_SITEMAP_XML),
            ["https://cinema.ucla.edu/events/introducing-dorothy-dandridge-2026-12-02/"],
        )

    def test_ucla_series_cards_supply_exact_upcoming_showtimes(self):
        self.assertEqual(
            scraper.discover_ucla_series_urls(
                '<a href="/series/dorothy-dandridge-2026/">Dorothy Dandridge</a>'
            ),
            ["https://www.cinema.ucla.edu/series/dorothy-dandridge-2026/"],
        )
        events = scraper.parse_ucla_series_events(
            UCLA_SERIES_HTML,
            "https://www.cinema.ucla.edu/series/dorothy-dandridge-2026/",
            NOW,
        )
        self.assertEqual([event["title"] for event in events], ["Introducing Dorothy Dandridge", "Malaga / Decks Ran Red"])
        self.assertEqual(events[0]["startAt"], "2026-12-02T19:30:00-08:00")
        self.assertEqual(events[1]["startAt"], "2026-12-13T19:00:00-08:00")
        self.assertEqual(events[0]["format"], "35mm")
        self.assertEqual(events[0]["ticketStatus"], "details")

    def test_ucla_scraper_uses_static_series_cards_when_indexes_are_client_rendered(self):
        pages = {
            scraper.UCLA_EVENTS_URL: "<html><p>Data Loading in Progress</p></html>",
            scraper.UCLA_SERIES_URL: "<html><p>Data Loading in Progress</p></html>",
            **{url: "<urlset></urlset>" for url in scraper.UCLA_SITEMAP_URLS},
            **{url: UCLA_SERIES_HTML for url in scraper.UCLA_SERIES_SEED_URLS},
        }
        result = scraper._scrape_ucla_film_archive(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 2)
        self.assertEqual(
            [event["title"] for event in result["events"]],
            ["Introducing Dorothy Dandridge", "Malaga / Decks Ran Red"],
        )

    def test_ucla_parser_stops_before_more_in_this_series(self):
        event = scraper.parse_ucla_event(
            UCLA_RELATED_TIME_HTML,
            "https://www.cinema.ucla.edu/events/current-ucla-event-2026-12-02/",
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["startAt"], "2026-12-02T19:00:00-08:00")
        self.assertNotIn("Another Theater", event["synopsis"])

    def test_ucla_sitemap_prefers_future_events_before_historical_urls(self):
        future_url = "https://www.cinema.ucla.edu/events/introducing-dorothy-dandridge-2026-12-02/"
        pages = {
            scraper.UCLA_EVENTS_URL: "<html><p>Data Loading in Progress</p></html>",
            scraper.UCLA_SERIES_URL: "<html><p>Data Loading in Progress</p></html>",
            scraper.UCLA_SITEMAP_URLS[0]: UCLA_EVENT_SITEMAP_WITH_HISTORY_XML,
            future_url: UCLA_HTML,
        }
        with patch.object(scraper, "DEFAULT_MAX_AC_EVENTS", 1):
            result = scraper._scrape_ucla_film_archive(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 1)
        self.assertEqual(result["events"][0]["sourceUrl"], future_url)

    def test_new_sources_are_optional_and_registered(self):
        for source_id in ("fine-arts-theatre", "filmforum", "old-town-music-hall", "ucla-film-archive"):
            self.assertIn(source_id, scraper.SOURCE_REGISTRY)
            self.assertFalse(scraper.SOURCE_REGISTRY[source_id].get("required", True))

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

    def test_cached_fetcher_gives_veezi_a_longer_timeout(self):
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b"<html>ok</html>"

        with TemporaryDirectory() as cache_dir:
            fetcher = scraper.CachedFetcher(cache_dir=Path(cache_dir), request_gap=0, retries=0, timeout=4)
            with patch.object(scraper, "urlopen", return_value=Response()) as mocked_urlopen:
                fetcher.fetch(scraper.NEW_BEV_SESSIONS_URL, force=True)

            self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], max(4, scraper.VEEZI_TIMEOUT))

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

    def test_american_cinematheque_rejects_program_pages_as_screenings(self):
        event = scraper.parse_american_cinematheque_event(
            '<html><h1>Programmed by Mel Brooks: 100 Years at the Movies</h1>'
            '<p>WED SEP 9, 2026 7:30 PM</p><p>Aero Theatre | Program</p></html>',
            "https://www.americancinematheque.com/series/programmed-by-mel-brooks-100-years-at-the-movies/",
            NOW,
        )
        self.assertIsNone(event)

    def test_american_cinematheque_uses_event_url_date_not_related_event_date(self):
        html = AC_HTML.replace(
            "FRI SEP 11, 2026 7:30 PM",
            "SAT SEP 12, 2026 9:00 PM FRI SEP 11, 2026 7:30 PM",
        )
        event = scraper.parse_american_cinematheque_event(
            html,
            "https://www.americancinematheque.com/now-showing/crash-09-11-2026/",
            NOW,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["startAt"], "2026-09-11T19:30:00-07:00")

    def test_american_cinematheque_event_uses_primary_metadata_venue(self):
        cases = (
            (AC_EGYPTIAN_HTML, "https://www.americancinematheque.com/now-showing/the-visit-trap-09-28-2026/", "Egyptian Theatre"),
            (AC_LOS_FELIZ_HTML, "https://www.americancinematheque.com/now-showing/crash-09-11-2026/", "Los Feliz 3"),
            (
                '<p>Aero Theatre | Global navigation/program label</p>' + AC_LOS_FELIZ_HTML,
                "https://www.americancinematheque.com/now-showing/straw-dogs-09-08-2026/",
                "Los Feliz 3",
            ),
        )
        for html, page_url, expected_venue in cases:
            with self.subTest(expected_venue=expected_venue):
                event = scraper.parse_american_cinematheque_event(html, page_url, NOW)
                self.assertIsNotNone(event)
                self.assertEqual(event["venue"], expected_venue)

    def test_american_cinematheque_expands_related_events(self):
        straw_url = "https://www.americancinematheque.com/now-showing/straw-dogs-09-08-2026/"
        two_lane_url = "https://www.americancinematheque.com/now-showing/two-lane-black-top-09-08-2026/"
        pages = {
            scraper.AC_NOW_SHOWING_URL: f'<a href="{straw_url}">STRAW DOGS</a>',
            straw_url: AC_STRAW_DOGS_HTML,
            two_lane_url: AC_TWO_LANE_HTML,
        }
        with patch.object(scraper, "AC_SEED_URLS", []):
            result = scraper._scrape_american_cinematheque(NOW, FakeFetcher(pages), force=True)
        events = {event["title"]: event for event in result["events"]}
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(events["STRAW DOGS"]["venue"], "Los Feliz 3")
        self.assertEqual(events["TWO-LANE BLACK TOP"]["venue"], "Aero Theatre")

    def test_american_cinematheque_discovers_series_and_canonicalizes_tracking(self):
        urls = scraper.discover_american_cinematheque_urls(
            '<a href="/series/programmed-by-mel-brooks-100-years-at-the-movies/">Program</a>'
            '<a href="/now-showing/city-lights-09-03-2026/?mc_cid=tracking">City Lights</a>'
        )
        self.assertEqual(urls, [
            "https://www.americancinematheque.com/series/programmed-by-mel-brooks-100-years-at-the-movies/",
            "https://www.americancinematheque.com/now-showing/city-lights-09-03-2026/",
        ])

    def test_american_cinematheque_expands_series_event_list(self):
        series_url = "https://www.americancinematheque.com/series/programmed-by-mel-brooks-100-years-at-the-movies/"
        elephant_url = "https://www.americancinematheque.com/now-showing/the-elephant-man-09-09-2026/"
        elephant_html = AC_HTML.replace("CRASH", "THE ELEPHANT MAN").replace("FRI SEP 11", "WED SEP 9")
        pages = {
            scraper.AC_NOW_SHOWING_URL: f'<a href="{series_url}">Program</a>',
            series_url: AC_SERIES_HTML,
            elephant_url: elephant_html,
        }
        with patch.object(scraper, "AC_SEED_URLS", []):
            result = scraper._scrape_american_cinematheque(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual([event["title"] for event in result["events"]], ["THE ELEPHANT MAN"])

    def test_american_cinematheque_series_keeps_future_months_and_drops_past_cards(self):
        entries = scraper.parse_american_cinematheque_series(
            AC_MULTI_MONTH_SERIES_HTML,
            "https://www.americancinematheque.com/series/program/",
            NOW,
        )
        self.assertEqual([entry["title"] for entry in entries], ["FUTURE FILM"])
        self.assertEqual(entries[0]["startAt"].strftime("%Y-%m-%d %H:%M"), "2026-10-06 22:00")
        self.assertEqual(entries[0]["venue"], "Los Feliz 3")

    def test_american_cinematheque_series_keeps_card_titles_with_shifted_heading_and_link_order(self):
        entries = scraper.parse_american_cinematheque_series(
            AC_SERIES_CARD_ASSOCIATION_HTML,
            "https://www.americancinematheque.com/series/program/",
            NOW,
        )
        self.assertEqual([entry["title"] for entry in entries], ["THE ELEPHANT MAN", "DOUBLE INDEMNITY"])
        self.assertEqual([entry["venue"] for entry in entries], ["Aero Theatre", "Egyptian Theatre"])
        self.assertEqual(
            [entry["pageUrl"] for entry in entries],
            [
                "https://www.americancinematheque.com/now-showing/the-elephant-man-09-09-2026/",
                "https://www.americancinematheque.com/now-showing/double-indemnity-10-17-2026/",
            ],
        )

    def test_american_cinematheque_series_card_survives_detail_failure(self):
        series_url = "https://www.americancinematheque.com/series/program/"
        pages = {
            scraper.AC_NOW_SHOWING_URL: f'<a href="{series_url}">Program</a>',
            series_url: AC_MULTI_MONTH_SERIES_HTML,
        }
        with patch.object(scraper, "AC_SEED_URLS", []):
            result = scraper._scrape_american_cinematheque(NOW, FakeFetcher(pages), force=True)
        future = [event for event in result["events"] if event["title"] == "FUTURE FILM"]
        self.assertEqual(len(future), 1)
        self.assertEqual(future[0]["sourceStatus"], "verified-series-calendar")
        self.assertEqual(future[0]["venue"], "Los Feliz 3")
        self.assertEqual(result["source"]["status"], "verified")
        self.assertTrue(result["source"]["warnings"])

    def test_new_beverly_schedule_pairs_double_feature_titles_and_times(self):
        entries = scraper.parse_new_beverly_schedule(SCHEDULE_HTML, scraper.NEW_BEV_SCHEDULE_URL, NOW)
        self.assertEqual([entry["title"] for entry in entries], [
            "Rocky (Garrett Brown In Person)",
            "Bound for Glory",
            "Magnolia",
        ])
        self.assertEqual([entry["startAt"].strftime("%H:%M") for entry in entries], ["19:30", "22:00", "14:00"])

    def test_new_beverly_schedule_drops_untitled_program_rows(self):
        entries = scraper.parse_new_beverly_schedule(NEW_BEV_UNTITLED_TIME_HTML, scraper.NEW_BEV_SCHEDULE_URL, NOW)
        self.assertEqual(entries, [])

    def test_new_beverly_schedule_drops_ambiguous_multi_time_fallback_rows(self):
        html = """
        <a href="/program/september-11-ambiguous/">
          Friday, September 11 7:00 pm / 9:45 pm One Film More Info
        </a>
        """
        entries = scraper.parse_new_beverly_schedule(html, scraper.NEW_BEV_SCHEDULE_URL, NOW)
        self.assertEqual(entries, [])

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
        self.assertEqual(entries[0]["notes"], "Bookings closed")
        self.assertEqual(entries[2]["format"], "16mm / DVD")
        self.assertEqual(entries[3]["notes"], "Sold out")

    def test_vista_veezi_feed_ignores_generic_non_screening_headings(self):
        html = """
        <html><h3>Special Event</h3><h3>Show Times</h3>
        <h3>Tuesday 8, September</h3>
        <a href="https://ticketing.uswest.veezi.com/ticket/1">7:30 PM</a></html>
        """
        self.assertEqual(scraper.parse_vista_session_feed(html, NOW), [])

    def test_global_deduper_ignores_source_name_and_title_punctuation(self):
        events = [
            {"city": "los-angeles", "venue": "Vista Theater", "title": "The Film", "startAt": "2026-09-11T19:30:00-07:00", "sourceName": "Vista Theater Hollywood", "sourceStatus": "verified"},
            {"city": "los-angeles", "venue": "Vista Theater", "title": "THE-FILM", "startAt": "2026-09-11T19:30:00-07:00", "sourceName": "Vista ticketing", "sourceStatus": "verified-series-calendar", "synopsis": "Richer metadata"},
        ]
        deduped = scraper.deduplicate_events(events)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["sourceName"], "Vista ticketing")

    def test_vista_adapter_uses_veezi_as_source_of_truth(self):
        pages = {scraper.VISTA_SESSIONS_URL: VEEZI_SESSIONS_HTML}
        result = scraper._scrape_vista(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 4)
        self.assertEqual(result["events"][0]["sourceUrl"], scraper.VISTA_SESSIONS_URL)

    def test_vidiots_discovery_restricts_to_official_movie_pages(self):
        self.assertEqual(
            scraper.discover_vidiots_movie_urls(VIDIOTS_INDEX_HTML),
            ["https://vidiotsfoundation.org/movies/possession/"],
        )

    def test_vidiots_movie_detail_extracts_showtime_and_metadata(self):
        details = scraper.parse_vidiots_movie_detail(
            VIDIOTS_DETAIL_HTML,
            "https://vidiotsfoundation.org/movies/possession/",
            NOW,
        )
        self.assertEqual(details["title"], "Possession")
        self.assertEqual(details["showtimes"][0]["startAt"].strftime("%Y-%m-%d %H:%M"), "2026-09-11 19:30")
        self.assertEqual(details["director"], "Andrzej Zulawski")
        self.assertEqual(details["year"], "1981")
        self.assertEqual(details["format"], "35mm")
        self.assertEqual(details["showtimes"][0]["ticketUrl"], "https://tickets.example/possession")

    def test_vidiots_movie_detail_keeps_multiple_showtimes_on_one_date(self):
        details = scraper.parse_vidiots_movie_detail(
            VIDIOTS_MULTI_SHOWTIME_DETAIL_HTML,
            "https://vidiotsfoundation.org/movies/possession/",
            NOW,
        )
        self.assertEqual(
            [showtime["startAt"].strftime("%Y-%m-%d %H:%M") for showtime in details["showtimes"]],
            ["2026-09-11 19:30", "2026-09-11 21:45"],
        )
        self.assertEqual(
            [showtime["ticketUrl"] for showtime in details["showtimes"]],
            [
                "https://tickets.example/possession-730",
                "https://tickets.example/possession-945",
            ],
        )

    def test_vidiots_movie_detail_ignores_related_movie_showtimes(self):
        details = scraper.parse_vidiots_movie_detail(
            VIDIOTS_RELATED_SHOWTIME_DETAIL_HTML,
            "https://vidiotsfoundation.org/movies/only-at-seven/",
            NOW,
        )
        self.assertEqual(
            [showtime["startAt"].strftime("%H:%M") for showtime in details["showtimes"]],
            ["19:00"],
        )

    def test_vidiots_traversal_discovers_upcoming_listing_pages(self):
        self.assertEqual(
            scraper.discover_vidiots_traversal_urls(VIDIOTS_HOME_HTML),
            [
                "https://vidiotsfoundation.org/coming-soon/",
                "https://vidiotsfoundation.org/upcoming-movies/",
            ],
        )

    def test_vidiots_prefers_movie_page_schedule_over_listing_card_schedule(self):
        pages = {
            scraper.VIDIOTS_HOME_URL: VIDIOTS_HOME_HTML,
            scraper.VIDIOTS_URL: VIDIOTS_INDEX_WITH_SHOWTIME_HTML,
            "https://vidiotsfoundation.org/movies/possession/": VIDIOTS_MULTI_SHOWTIME_DETAIL_HTML,
        }
        result = scraper._scrape_vidiots(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["sourceMode"], "official")
        self.assertEqual(result["source"]["eventCount"], 2)
        self.assertEqual([event["time"] for event in result["events"]], ["7:30 PM", "9:45 PM"])
        self.assertEqual(
            [event["ticketUrl"] for event in result["events"]],
            [
                "https://tickets.example/possession-730",
                "https://tickets.example/possession-945",
            ],
        )

    def test_vidiots_partner_listing_extracts_event_pages_and_showtimes(self):
        entries = scraper.parse_vidiots_partner_listing(VIDIOTS_PARTNER_HTML, now=NOW)
        self.assertEqual([entry["title"] for entry in entries], ["The Traveler", "Splash"])
        self.assertEqual(entries[0]["startAt"].strftime("%Y-%m-%d %H:%M"), "2026-09-12 16:30")
        self.assertEqual(entries[1]["pageUrl"], "https://la.teentix.org/calendar/event/splash/")

    def test_vidiots_uses_teentix_partner_listing_when_official_site_is_unavailable(self):
        pages = {scraper.VIDIOTS_PARTNER_URL: VIDIOTS_PARTNER_HTML}
        result = scraper._scrape_vidiots(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["sourceMode"], "partner-fallback")
        self.assertEqual(result["source"]["eventCount"], 2)
        self.assertTrue(result["source"]["warnings"])
        self.assertEqual(result["events"][0]["sourceLabel"], "Partner listing · TeenTix LA / Vidiots")
        self.assertEqual(result["events"][0]["sourcePageUrl"], scraper.VIDIOTS_PARTNER_URL)
        self.assertTrue(result["events"][0]["sourceUrl"].startswith("https://la.teentix.org/calendar/event/"))

    def test_vidiots_index_can_verify_without_movie_page_enrichment(self):
        pages = {scraper.VIDIOTS_URL: VIDIOTS_INDEX_WITH_SHOWTIME_HTML}
        result = scraper._scrape_vidiots(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 1)
        self.assertTrue(result["source"]["warnings"])
        self.assertEqual(result["events"][0]["title"], "Possession")

    def test_vidiots_adapter_verifies_official_movie_pages(self):
        pages = {
            scraper.VIDIOTS_URL: VIDIOTS_INDEX_HTML,
            "https://vidiotsfoundation.org/movies/possession/": VIDIOTS_DETAIL_HTML,
        }
        result = scraper._scrape_vidiots(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 1)
        self.assertEqual(result["events"][0]["sourceName"], "Vidiots")

    def test_new_beverly_veezi_feed_stays_verified_when_enrichment_is_unavailable(self):
        result = scraper._scrape_new_beverly(
            NOW,
            FakeFetcher({scraper.NEW_BEV_SESSIONS_URL: VEEZI_SESSIONS_HTML}),
            force=True,
        )
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["eventCount"], 4)
        self.assertFalse(result["source"]["errors"])
        self.assertTrue(result["source"]["warnings"])
        self.assertEqual(result["events"][0]["sourceUrl"], scraper.NEW_BEV_SESSIONS_URL)

    def test_new_beverly_uses_official_schedule_when_veezi_is_unavailable(self):
        result = scraper._scrape_new_beverly(
            NOW,
            FakeFetcher({scraper.NEW_BEV_SCHEDULE_URL: SCHEDULE_HTML}),
            force=True,
        )
        self.assertEqual(result["source"]["status"], "verified")
        self.assertEqual(result["source"]["sourceMode"], "official-schedule-fallback")
        self.assertEqual(result["source"]["eventCount"], 3)
        self.assertTrue(result["source"]["warnings"])
        self.assertEqual(result["events"][0]["sourcePageUrl"], scraper.NEW_BEV_SCHEDULE_URL)

    def test_scrape_combines_official_sources_and_deduplicates(self):
        pages = {
            scraper.AC_NOW_SHOWING_URL: '<a href="/now-showing/crash-09-11-2026/">Crash</a>',
            scraper.NEW_BEV_SESSIONS_URL: VEEZI_SESSIONS_HTML,
            scraper.VISTA_SESSIONS_URL: VEEZI_SESSIONS_HTML,
            "https://www.americancinematheque.com/now-showing/crash-09-11-2026/": AC_HTML,
            "https://thenewbev.com/program/september-8-rocky-bound-for-glory/": DETAIL_HTML,
            "https://thenewbev.com/program/september-11-magnolia-friday-matinee/": DETAIL_HTML,
            "https://www.vistatheaterhollywood.com/movies/onslaught/": VISTA_HTML,
            scraper.VIDIOTS_URL: VIDIOTS_INDEX_HTML,
            "https://vidiotsfoundation.org/movies/possession/": VIDIOTS_DETAIL_HTML,
        }
        result = scraper.scrape_los_angeles(NOW, FakeFetcher(pages), force=True)
        self.assertEqual(len(result["screenings"]), 10)
        self.assertEqual({event["sourceName"] for event in result["screenings"]}, {"American Cinematheque", "New Beverly Cinema", "Vista Theater Hollywood", "Vidiots"})
        self.assertTrue(all(event["city"] == "los-angeles" for event in result["screenings"]))


if __name__ == "__main__":
    unittest.main()
