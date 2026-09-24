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
start a crawl. Before Rerun has completed its first fully verified crawl, the
API returns an intentional `503` instead of presenting bundled seed data as
live. Once a verified catalog exists, a temporary required-source outage serves
that last verified catalog with `servingLastVerifiedSnapshot: true` and clear
status copy while the worker retries. New Beverly and Vidiots are also indexed
when available, but are marked optional so ticketing or program-site outages
cannot block the verified feed. The current Los Angeles sources are:

- [American Cinematheque Now Showing](https://www.americancinematheque.com/now-showing/)
- [New Beverly Cinema Veezi sessions](https://ticketing.uswest.veezi.com/sessions/?siteToken=fmtswb0qqbym3de6c4bbsqj89m)
- [Vista Theater Hollywood Veezi sessions](https://ticketing.uswest.veezi.com/sessions/?siteToken=20xhpa3yt2hhkwt4zjvfcwsaww)
- [Vidiots](https://vidiotsfoundation.org/) / [Coming Soon](https://vidiotsfoundation.org/coming-soon/)
- [Academy Museum calendar — Screenings filter](https://www.academymuseum.org/en/calendar?programTypes=16i3uOYQwism7sMDhIQr2O)
- [Egyptian Theatre](https://www.egyptiantheatre.com/)
- [Fine Arts Theatre Beverly Hills Veezi sessions](https://ticketing.uswest.veezi.com/sessions/?siteToken=tez3prscsvfbagchhkxbevjwk8)
- [Los Angeles Filmforum schedule](https://www.lafilmforum.org/schedule/)
- [Old Town Music Hall](https://www.oldtownmusichall.org/home/)
- [Brain Dead Studios programming](https://wearebraindead.com/pages/programming)
- [UCLA Film & Television Archive events](https://www.cinema.ucla.edu/events/)

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
and every event keeps its source `sourceId`, `sourceUrl`, `sourceName`, and
`sourceLastChecked` value. A failed required-source refresh never overwrites
`last-verified-snapshot.json`. When one or more sources still verify during
that outage, their non-empty results replace only their own prior records in a
clearly degraded current snapshot; failed or zero-result sources retain their
last verified records. If no verified snapshot exists, the API returns an
intentional HTTP 503 with source-level errors, a `Retry-After` header, and a
`retryAfterSeconds` value. Optional-source failures and reachable zero-result
sources are marked `degraded` through `/api/sources` and the feed health label.
The browser honors the server's backoff (up to 15 minutes) instead of polling
every few seconds while a venue is unavailable, so a Vista outage no longer
fills the terminal with repeated requests.

Transient upstream failures (timeouts, connection errors, 408/425/429, and
temporary 5xx responses) receive one short, per-domain retry. A venue's
canonical schedule remains authoritative when optional detail pages fail: New
Beverly showtimes come from its Veezi session feed, while detail pages only enrich
artwork and film metadata. These bounded retries and enrichment warnings do
not relax the complete required-source verification requirement.
Academy Museum and UCLA requests use a bounded 12-second timeout by default
(`RERUN_SLOW_SOURCE_HTTP_TIMEOUT`) because their official program pages can be
larger or slower than a venue ticketing page.

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
Each event page's primary `Locations` metadata is used for attribution and
takes priority over a conflicting series card, including Directors Village in
Westwood. Vista uses
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

The Academy Museum adapter traverses the official calendar's `Screenings`
program-type filter, follows pagination/load-more calendar links, and then
opens each linked program detail page for the exact date/time, format, artwork,
and ticket link. It always combines that graph with the official Screenings &
Series index, current series pages, and the Academy sitemap or sitemap index,
including series links found in XML, so a partially hydrated calendar cannot
hide newly posted programs. It accepts detail pages whose event content begins
with `Screening` or `Screenings`, including a `Screening from Series` label
that shares a block with its series link; conversations, book signings, special
events, exhibitions, and related-event links remain excluded. If Academy has published a screening date but not its
clock time, Rerun keeps the date as `Time TBA` and links to the official event
details rather than inferring a time from a related card. The Academy Museum source is optional for feed health, so
a temporary museum-site outage does not erase the other official listings.

Egyptian Theatre traversal checks its official home, series, special-engagement,
and sitemap paths, then follows individual film pages and explicitly
screening-related event pages. Current program-series links are prioritized
over archival sitemap URLs. If the Egyptian Theatre has posted a date but not
a time, the entry is marked `Time TBA`; a matching timed official listing at
the Egyptian on the same day is kept instead, so one screening is not shown
twice.

Fine Arts Theatre Beverly Hills uses its official Veezi sessions feed, reusing
the same canonical session parser as Vista and New Beverly. Filmforum is
traversed from its official schedule into program detail pages; each detail
page is authoritative for its own ticket link. Old Town Music Hall is
traversed from its official homepage into movie pages and their dated
showtimes. UCLA Film & Television Archive traversal checks the official events
and series indexes, always walks sitemap indexes and child sitemaps, and then
follows the bounded official series-to-series graph surfaced by server-rendered
`Explore other series` cards. It reads exact future event cards from those
series pages, whose dates and times remain available even when the broad
calendar is JavaScript-only; only future pages with screening or film-program
markers are retained.
Brain Dead Studios traverses the official [Coming Soon slate](https://studios.wearebraindead.com/coming-soon/)
into every linked `/movies/` page, which Rerun treats as a screening regardless
of the title text. Linked `/special_events/` pages require an explicit
film/screening marker. The public Programming page provides first-party
schedule fallback. If the Studio blocks plain HTTP access to the slate or a
movie detail page, Rerun can fill only cards explicitly tagged Film from the
[Mato Brain Dead venue calendar](https://ma.to/venue/braindeadstudiosfairfax);
those cards are visibly labeled as partner listings and link to Mato rather
than being represented as official Studio records. All supplemental sources are
optional so a source-specific outage cannot invalidate the
complete feed from the existing required venues.

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

After configuring the Instagram secrets, the same authenticated token can
trigger a one-time current-day post:

```bash
curl -X POST \
  -H "X-Rerun-Refresh-Token: $RERUN_REFRESH_TOKEN" \
  https://your-rerun-host.example/api/instagram/publish
```

Refresh timing, worker state, and source health are available from
`GET /api/health` and `GET /api/sources`. `RERUN_WORKER_POLL_SECONDS` controls
how often the worker checks for a due refresh; `RERUN_REFRESH_TTL_SECONDS`
defaults to 30 minutes, and `RERUN_REFRESH_RETRY_SECONDS` defaults to 10
minutes after a failed attempt. Health responses include whether durable
storage is configured, source-level availability, and whether the browse API
is currently serving a last verified snapshot.

## Render launch configuration

All operational state (the verified snapshot, crawl cache, worker state, and
subscriber SQLite database) must live on persistent storage in production.
On Render, attach a persistent disk, mount it at `/var/data`, and set:

```text
RERUN_DATA_DIR=/var/data
```

Then configure Render's HTTP health check path as `/api/health`. After the next
successful crawl, confirm the health payload reports
`storage.persistentStorageConfigured: true`, a populated `lastVerifiedAt`, and
`feed.complete: true`. The raw JSON catalog paths are intentionally blocked;
clients must use the API endpoints below.

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
- `POST /api/notify` with `{ "email": "you@example.com" }` — local signup store

Alert signups are stored in SQLite at `data/rerun.sqlite3`, with an idempotent
migration from the prototype's `data/subscribers.json` file. The database and
all operational JSON files are blocked from static serving. The form has a
short per-IP rate limit and honeypot field; it stores subscribers but does not
send mail until a delivery provider is connected.

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

- Official-source Los Angeles browse feed with verified-snapshot outage fallback
- Day-first browse view with previous/next navigation and a dedicated calendar page
- Selectable month calendar with per-day screening counts and details
- Browse requests are filtered to the selected date server-side, keeping the live payload small
- Source links on every card and in the details modal
- Search across title, director, genre, format, and venue
- Quick date filters for tonight, tomorrow, this weekend, and all upcoming screenings
- Soonest showtime or theatre sorting across the full date range
- Save and unsave screenings with browser `localStorage`; saved cards expose ticket, calendar, and share actions, while a second Save click removes a screening
- Film details modal with synopsis, format, venue, address, source, and notes
- Shareable screening URLs (`?screening=...`)
- Calendar `.ics` download from every screening detail view
- SQLite-backed alert signup connected to the local API
- Installable/offline shell through `manifest.json` and `service-worker.js`

## Deployment checklist

1. Attach a Render persistent disk and set `RERUN_DATA_DIR` to its mount path.
2. Configure `/api/health` as the service health check.
3. Deploy, wait for one successful worker crawl, then inspect `/api/health`.
4. Confirm `lastVerifiedAt`, `feed.complete`, and each required source before
   announcing the guide.
5. Review venue terms, ticket links, privacy copy, and image rights before
   commercial launch.

Useful Render references: [persistent disks](https://render.com/docs/disks),
[health checks](https://render.com/docs/health-checks), and
[Blueprint configuration](https://render.com/docs/blueprint-spec).

## Instagram Today in LA publisher

The scheduled worker can publish one daily Instagram post after midnight Pacific
using Meta's Content Publishing API. It selects future, official-source
screenings for Los Angeles, prefers one screening per venue, uses the first
available official image as the post artwork, and records the date and returned
media ID in `data/instagram-post-state.json` so restarts do not duplicate a post.

Configure these deployment secrets; never commit the access token:

```text
RERUN_INSTAGRAM_ACCESS_TOKEN=...
RERUN_PUBLIC_BASE_URL=https://your-rerun-domain.example
```

The default mode is `instagram-login`, which uses Instagram's
`graph.instagram.com` API and expects an Instagram Login access token with
content-publishing permission. For a token obtained through Facebook Login,
set `RERUN_INSTAGRAM_API_MODE=facebook-login` and also set
`RERUN_INSTAGRAM_USER_ID` to the professional Instagram account ID.

Optional settings are `RERUN_INSTAGRAM_API_MODE`, `RERUN_INSTAGRAM_USER_ID`,
`RERUN_INSTAGRAM_API_VERSION`, `RERUN_INSTAGRAM_SELECTION_LIMIT`, and a
comma-separated `RERUN_INSTAGRAM_FEATURED_IDS` list for editorial overrides.
The publisher stays disabled until the token and public base URL are present.
The manual `/api/instagram/publish` route forces a retry after a previous
publishing error; the scheduled worker still observes the normal retry delay.
