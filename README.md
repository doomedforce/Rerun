# Rerun

Rerun is a standalone repertory-film discovery product: a calm, local-first
guide to the screenings worth leaving the house for. Los Angeles is the first
live market.

## Run locally

The project has no install step. If Node is available:

```bash
npm start
```

If `npm` is not installed, use the built-in Python server instead:

```bash
python3 server.py
```

On Windows, the equivalent command is `python server.py`. The server includes
a built-in America/Los_Angeles timezone fallback, so a fresh Python install
does not need the optional `tzdata` package just to start Rerun.

Open [http://localhost:4173](http://localhost:4173). Set `RERUN_PORT=4174` if
the default port is already in use.

## Official LA feed

Rerun runs one scheduled refresh worker at a time. Browse requests are
read-only: they serve the last atomically written catalog snapshot and never
start a crawl. The API refuses to return screening data unless all required
sources report verified, so the browser never renders a partial required-source list. New
Beverly and Vidiots are also indexed when available, but are marked optional
so ticketing or program-site outages cannot block the verified feed. The
current Los Angeles sources are:

- [American Cinematheque Now Showing](https://www.americancinematheque.com/now-showing/)
- [New Beverly Cinema Veezi sessions](https://ticketing.uswest.veezi.com/sessions/?siteToken=fmtswb0qqbym3de6c4bbsqj89m)
- [Vista Theater Hollywood Veezi sessions](https://ticketing.uswest.veezi.com/sessions/?siteToken=20xhpa3yt2hhkwt4zjvfcwsaww)
- [Vidiots](https://vidiotsfoundation.org/) / [Coming Soon](https://vidiotsfoundation.org/coming-soon/)

Vidiots is traversed from its official homepage into the current upcoming-listing
pages and then into each linked movie page. Individual official movie pages are
the preferred source for dates and showtimes; listing cards are used only when a
movie page is unavailable or omits its schedule. If the official site cannot
produce upcoming showtimes, Rerun uses the [TeenTix LA Vidiots partner listing](https://la.teentix.org/venues/vidiots/)
as an optional fallback. Those cards are labeled as partner listings and link
directly to the TeenTix event page, so the provenance is visible rather than
implied to be official.

When Vidiots exposes separate ticket anchors for separate showtimes, each
visible showtime preserves its own ticket URL instead of inheriting the first
ticket button on the movie page.

Each venue adapter runs concurrently. Within each source, detail pages are
fetched concurrently with a per-domain request gap, and responses are reused
from the local HTML cache in `data/cache/`. Showtimes are normalized to America/Los_Angeles,
past events are removed, duplicate source/title/time records are collapsed,
and every event keeps its source `sourceUrl`, `sourceName`, and
`sourceLastChecked` value. If a required source is unavailable, the API
withholds the catalog and the browser remains in its complete-index loading
state. That response is an intentional HTTP 503, not a missing page: it
includes the source-level error, a `Retry-After` header, and a
`retryAfterSeconds` value. Optional-source failures remain visible through
`/api/sources` and the feed health label.
The browser honors the server's backoff (up to 15 minutes) instead of polling
every few seconds while a venue is unavailable, so a Vista outage no longer
fills the terminal with repeated requests.

Transient upstream failures (timeouts, connection errors, 408/425/429, and
temporary 5xx responses) receive one short, per-domain retry. A venue's
canonical schedule remains authoritative when optional detail pages fail: New
Beverly showtimes come from its Veezi session feed, while detail pages only enrich
artwork and film metadata. These bounded retries and enrichment warnings do
not relax the complete required-source verification requirement.

If the New Beverly Veezi endpoint times out or returns no usable sessions, the
official New Beverly schedule is used as a clearly labeled fallback so one
ticketing-host outage cannot block the required Los Angeles feed.

American Cinematheque discovery checks its event index and the server-rendered
homepage before falling back to seed URLs, follows active official `/series/`
pages to reach each program's full event list—including future months—and then
expands official Related Events links so screenings that are only connected
from another event page are not lost. The series calendar cards are the
authoritative AC schedule layer for title, time, venue, and past-event
filtering; non-screening headings such as About the Series and Membership are
excluded; individual event pages only enrich them with artwork and metadata.
Each event page's primary venue metadata is used for attribution. Vista uses
the Veezi session feed as the source of truth for dates, times, sold-out state,
and special formats;
Vista movie pages are optional enrichment for artwork and metadata. Vidiots
uses its official coming-soon index to discover official movie pages and uses
server-rendered index showtimes when available; movie pages provide best-effort
enrichment or a conservative fallback. Source-level durations are included
in `/api/sources` to make future bottlenecks visible.
The browse and calendar API response limit is 500 screenings; enrichment detail
requests remain separately bounded so a larger catalog does not create an
unbounded burst of venue-page requests.
Vista detail parsing accepts both the schedule page's split month/day layout
and the movie pages' combined `September, 8th 2026` layout. When a movie page
omits a visible title, the adapter derives it from the official `/movies/`
slug and uses the page's `Summary` section for metadata.

Manual refreshes recheck the schedule/index pages while reusing detail pages
that are still inside the cache window, so a user-triggered update does not
re-download every poster and metadata page.

Manual refresh is disabled unless `RERUN_REFRESH_TOKEN` is configured. With a
token, trigger a refresh from an administrative shell; the response returns
after indexing completes:

```bash
curl -X POST \
  -H "X-Rerun-Refresh-Token: $RERUN_REFRESH_TOKEN" \
  http://localhost:4173/api/refresh
```

Refresh timing, worker state, and source health are available from
`GET /api/health` and `GET /api/sources`. `RERUN_WORKER_POLL_SECONDS` controls
how often the worker checks for a due refresh; `RERUN_REFRESH_TTL_SECONDS`
defaults to 30 minutes, and `RERUN_REFRESH_RETRY_SECONDS` defaults to 10
minutes after a failed attempt.
There is deliberately no partial/offline listing mode in the UI.

The scraper is intentionally conservative: it only follows event pages on the
configured official venue domains, plus the explicit TeenTix LA Vidiots partner
listing used as a fallback. It does not bypass paywalls or bot controls, and
expects venue terms/robots policies to be reviewed before a commercial launch.
Always open the linked venue or partner listing to confirm the time, ticket
availability, and image rights before buying or publishing.

## API routes

- `GET /api/health` — service and feed status
- `GET /api/screenings?city=los-angeles&date=tonight&q=keaton` — filtered feed
- `GET /api/screenings/:id` — one screening record
- `GET /api/venues?city=los-angeles` — official venue directory
- `GET /api/sources` — source adapters, counts, last checks, and errors
- `POST /api/refresh` — force a source refresh with `X-Rerun-Refresh-Token`
- `POST /api/notify` with `{ "email": "you@example.com" }` — local alert signup

Alert signups are stored in SQLite at `data/rerun.sqlite3`, with an idempotent
migration from the prototype's `data/subscribers.json` file. The database and
all operational JSON files are blocked from static serving.

## Tests

The standard-library parser tests run without network access:

```bash
python3 -m unittest discover -s tests -v
```

## Artwork

Screening artwork is captured from each venue's official program page when
available. Each listing keeps its source URL so the image and showtime can be
verified before publishing or using it in a commercial context.

## Product behavior

- Official-source Los Angeles browse feed with stale/degraded transparency
- Day-first browse view with previous/next navigation and a dedicated calendar page
- Selectable month calendar with per-day screening counts and details
- Browse requests are filtered to the selected date server-side, keeping the live payload small
- Source links on every card and in the details modal
- Search across title, director, genre, format, and venue
- Date filters for all dates, tonight, and this weekend
- Soonest showtime or theatre sorting across the full date range
- Save and unsave films with browser `localStorage`
- Film details modal with synopsis, format, venue, address, source, and notes
- Shareable screening URLs (`?screening=...`)
- Calendar `.ics` download from every screening detail view
- SQLite-backed alert signup connected to the local API
- Installable/offline shell through `manifest.json` and `service-worker.js`
