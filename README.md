# Booking.com Scraper Rebuild

A command-line tool that retrieves **real Booking.com production data** — hotel search
results with per-hotel rooms/units and prices — and stores it as normalized, grouped JSON.

It is a working, demo-ready MVP: a single CLI command drives the real Booking.com
website headlessly (Playwright), landing on real hotel search pages and real property
detail pages, and saving what Booking actually shows. **The tool does not use mock
data, fixtures, paid scraping APIs, or synthetic results.**

> Status: demo-ready MVP. It works end-to-end on real Booking.com data but is **not**
> a production-grade, datacenter-scale scraper (see [Limitations](#limitations)).

## What it does

1. Resolves the destination you type (e.g. `Bogota`) against Booking.com's own live
   destination lookup (the same autocomplete the website uses) to obtain the target's
   internal destination id/type. **No manual city database is required.**
2. Opens the real Booking.com search-results page for that destination and your dates
   (adults / children / rooms / currency).
3. Collects hotel links, stopping as soon as the requested `--max-hotels` is reached.
4. Opens each hotel's real detail page and extracts rooms/units, prices, plans,
   cancellation rules, beds, sizes, amenities and images from the live page.
5. Writes one grouped JSON file: each hotel record contains its `hotel` object, its
   list of `units`, and provenance (`source_url`, `collected_at`).

## Requirements

- Linux/macOS (developed and verified on Linux)
- Python 3.10+
- [Playwright for Python](https://playwright.dev/python/) (see `requirements.txt`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

That's it. The destination resolution talks to Booking.com's own production endpoint,
so no API keys, accounts, or proxy configuration are needed.

## Basic CLI usage

```text
usage: python -m src.cli [-h] [--checkin CHECKIN] [--checkout CHECKOUT]
                         [--adults ADULTS] [--children CHILDREN]
                         [--rooms ROOMS] [--currency CURRENCY]
                         [--max-hotels MAX_HOTELS] [--headless |
                         --no-headless] [--output-dir OUTPUT_DIR]
                         [--prefix PREFIX]
                         destination

positional arguments:
  destination           Destination city or area to search, e.g. 'Bogota', 'Medellin, Colombia'
```

Options (defaults in parentheses):

| Option            | Meaning                                       |
| ----------------- | --------------------------------------------- |
| `--checkin`       | Check-in date `YYYY-MM-DD` (tomorrow)         |
| `--checkout`      | Check-out date `YYYY-MM-DD` (check-in + 1)    |
| `--adults`        | Number of adults (2)                          |
| `--children`      | Number of children (0)                        |
| `--rooms`         | Number of rooms (1)                           |
| `--currency`      | ISO 4217 currency code (USD)                  |
| `--max-hotels`    | Stop after this many hotels (5)               |
| `--headless` / `--no-headless` | Run headless (default) or with a visible browser |
| `--output-dir`    | Output directory (data)                       |
| `--prefix`        | Output filename prefix (booking_results)      |

### Example command

```bash
.venv/bin/python -m src.cli "Bogota" \
    --checkin 2026-09-20 --checkout 2026-09-21 \
    --adults 2 --rooms 1 --currency COP --max-hotels 3
```

Terminal output:

```text
Searching Booking.com...
Destination: Bogota
Dates: 2026-09-20 -> 2026-09-21
Guests: 2 adults, 1 room
Currency: COP
Target hotels: 3
Destination resolved: Bogotá, co (id -578472, type city, live)
Search phase: 3 unique hotels (max_hotels=3 reached)

Found hotel 1/3: Hotel Dorado 100
Found hotel 2/3: Habitaciòn calida y amplia para 2 o 3 personas
Found hotel 3/3: HOTEL DANUBIO

Completed.
Hotels: 3
Rooms/units: 10
Output: data/booking_results_20260920_104410.json
Metadata: data/booking_results_20260920_104410.meta.json
```

Run `.venv/bin/python -m src.cli --help` for the full help text. Detailed crawl
tracing is kept out of the normal demo output and written to
`data/logs/booking_debug.log` instead.

## Performance notes

- Hotel detail pages are fetched concurrently: up to `DETAIL_WORKERS` (default 3,
  clamped to 1–4) hotels are opened at once, each in its own browser context, so the
  workers never share a page. Set `DETAIL_WORKERS=1` for strictly sequential
  behaviour.
- Extraction lookups are bounded (short timeouts), so a missing field costs
  milliseconds instead of Playwright's default 30s action timeout.

## Robustness (Phase 3)

The application layer is hardened for repeated local use:

- **Bounded retries.** Transient failures (navigation timeouts, per-hotel
  extraction timeouts, live search-phase hiccups) are retried with exponential
  backoff. Retry counts are configurable and capped — there are no retry loops.
  Configuration (via environment variables):

  | Env var                | Default | Meaning                        |
  | ---------------------- | ------- | ------------------------------ |
  | `DETAIL_MAX_RETRIES`   | 2       | Max retries per hotel detail   |
  | `DETAIL_RETRY_BACKOFF_S` | 4       | Base backoff (doubles per retry) |
  | `DETAIL_WORKERS`       | 3       | Concurrent detail fetches (1–4) |
  | `JOB_TIMEOUT_S`        | 900     | Whole-job timeout in seconds   |

- **Partial-failure recovery.** A hotel that fails permanently is recorded and
  skipped; every successfully extracted hotel is still saved. Successful/failed
  counts and the failed hotel ids, reasons, retry count and attempt count are
  preserved (see [Output structure](#output-structure)).
- **Cancellation.** `Ctrl+C` on the CLI, or the **Cancel** button in the web UI,
  gracefully stops pending work: in-flight browser contexts are closed, already
  completed hotels are kept, and the run ends with an explicit `cancelled`
  status (exit code 130 on the CLI).
- **Job timeout protection.** Every job has a deadline (`JOB_TIMEOUT_S`); a job
  that exceeds it is marked `failed` with the timeout reason instead of running
  forever.
- **Explicit, user-facing error states.** Failures surface as short messages
  (CLI: exit code + one line; UI: banner + status pill). Raw tracebacks never
  reach the terminal or the browser; they go to the debug log.

## Localhost browser UI

The same scraper can be driven from a local web page instead of the CLI. The web app
is a thin wrapper around the existing `src.orchestrator.py` — it adds **no second
scraper and no mock data**; every search still hits real Booking.com through the exact
same application-layer code path.

### Start

```bash
.venv/bin/uvicorn src.webapp:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>.

- The server binds to `127.0.0.1` only (local access; not publicly exposed).
- One search can run at a time. A second submission while a search is already running
  is rejected with a clear message. Searches run in a background worker (own event
  loop), so the browser stays responsive and progress is polled from the server.
- While a search is running the page shows a **Cancel** button. Clicks are applied
  gracefully: the status pill moves to `cancelling` and then `cancelled`, and any
  hotels already extracted are shown and downloadable.
- The status pill reflects the actual job state (`running`, `cancelling`, `done`,
  `cancelled`, `error`), and the summary shows requested/successful/failed hotel
  counts plus the number of retries used. A failed or partial run explains why
  (e.g. timeout reason, list of failed hotel ids).
- Detailed crawl tracing is not shown in the browser; it still goes to
  `data/logs/booking_debug.log`.

### The form

Fields match the CLI defaults: Destination, Check-in (tomorrow), Check-out (+1 day),
Adults (2), Children (0), Rooms (1), Currency (USD), Hotel limit (5). Press
**Search Booking.com** to start.

### Progress

A 4-step tracker (Destination → Search hotels → Extract rooms → Done) is derived
live from the orchestrator's own progress lines, alongside running counts of hotels
found, the search target, failures and retries. The raw log lines are still available
under a collapsible **Technical log** panel. A **Cancel** button is shown while the
job is `running`/`cancelling`.

### Results

When the job finishes:

- summary stats (hotels, rooms/units, requested count, retries, collection timestamp);
- an expandable card per hotel — name, property type, location/address, a room-type
  count, property amenity chips, and a link to the live Booking.com page — with a
  scannable room list underneath (name, beds, size, plan, price, cancellation policy,
  room amenities, photo);
- a warning banner + a list of hotel ids/reasons when a run completed with partial
  failures, and a distinct message when a run was cancelled or produced no results;
- a **Download JSON** button that serves the very file written by the existing
  storage layer (`data/booking_results_<timestamp>.json`).

No field is invented: a hotel/room attribute Booking's page didn't expose (rating,
image, size, cancellation policy, etc.) is simply omitted from the card instead of
being filled with a placeholder value.

### API

| Method | Path                         | Purpose                          |
| ------ | ---------------------------- | -------------------------------- |
| GET    | `/`                          | The UI page                      |
| GET    | `/api/health`                | Health check                     |
| POST   | `/api/search`                | Start a search (JSON payload)    |
| GET    | `/api/search/{job_id}`       | Poll status/progress/results     |
| POST   | `/api/search/{job_id}/cancel` | Request graceful cancellation    |
| GET    | `/api/search/{job_id}/download` | Download the generated JSON    |
| GET    | `/api/search/{job_id}/meta`  | Run metadata (companion file)    |

Request validation mirrors the CLI rules (empty destination, past check-in,
check-in ≥ check-out, out-of-range occupants). Starting a second concurrent
search returns `409`. Job ids are 12-hex identifiers returned by `POST /api/search`.
Job statuses: `running`, `cancelling`, `done`, `cancelled`, `error`.

## Output structure

Each run writes one JSON file: `data/booking_results_<timestamp>.json`. It is a
top-level list of hotel records:

```jsonc
[
  {
    "hotel": {
      "hotel_id": "danubio",                    // Booking URL slug
      "hotel_name": "HOTEL DANUBIO",
      "booking_url": "https://www.booking.com/hotel/co/danubio.es.html?...",
      "address": "DIG 60sur diagonal 60sur #3 69, 110521 Bogotá, Colombia",
      "city": "Bogotá",
      "country": "Colombia",
      "latitude": 4.5368,
      "longitude": -74.1151,
      "property_type": "Hotel",                 // from Booking's own JSON-LD
      "amenities": ["Servicio de habitaciones", "WiFi gratis", "Recepción 24 horas"],
      "images": []
    },
    "units": [
      {
        "unit_id": "958927201",                 // Booking room/rate-block id
        "unit_name": "Habitación con cama grande",
        "price_amount": 39200.0,
        "price_currency": "COP",
        "price_text": "COP 39.200",
        "plan_name": null,                      // vs "solo alojamiento", "con desayuno", ...
        "cancellation": null,
        "beds_text": null,
        "size_m2": null,                        // present when Booking exposes it
        "amenities": [],
        "images": ["https://cf.bstatic.com/xdata/..."],
        "unique_url": "https://www.booking.com/hotel/co/danubio.es.html#unit-958927201"
      }
    ],
    "source_url": "https://www.booking.com/hotel/co/danubio.es.html?checkin=...",
    "collected_at": "2026-09-20T04:35:00.000000Z"
  }
]
```

Fields are omitted/`null` when the live page does not expose them for that hotel.

### Companion metadata file

Each successful run also writes `data/booking_results_<timestamp>.meta.json`
next to the data file. It describes the run and stays valid JSON:

```jsonc
{
  "status": "completed_with_partial_failures",   // | "completed" | "cancelled" | "failed"
  "requested_hotels": 3,
  "successful_hotels": 2,
  "failed_hotels": 1,
  "units": 6,
  "retries_total": 1,
  "cancelled": false,
  "timeout_s": null,
  "failure_reason": null,
  "params": { "destination": "Dhaka", "checkin": "2026-09-21", /* ... */ },
  "max_hotels": 3,
  "destination": { "label": "Dhaka", "dest_id": -2092392, "dest_type": "city", "source": "live" },
  "failures": [
    {
      "index": 1,
      "hotel_id": "some-slug",
      "hotel_name": null,
      "reason": "no units extracted",
      "retries": 0,
      "attempts": 1
    }
  ],
  "timing": { "storage.save": { "total_s": 0.4, "count": 1 } },
  "collected_at": "2026-09-20T04:35:00.000000Z"
}
```

A `cancelled` run keeps its completed hotels; its `failures` list marks the
hotels that never finished (reason `cancelled before completion`).

## Testing

Application-control logic (retries, partial-failure handling, cancellation,
job timeout, metadata/schema validity, web API behaviour) is unit-tested with a
fake client — no Booking traffic — so it is fast and deterministic:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

The real retrieval path is deliberately **not** unit-tested; the only valid
proof is a live run against Booking.com (see Basic CLI usage). A live
failure/recovery demo is included:

```bash
.venv/bin/python scripts/partial_failure_demo.py "Dhaka" --max-hotels 3
```

It performs a real search but injects one transient failure (recovered by a
retry) and one permanent failure, to demonstrate the retry + partial-failure
metadata on live data.

A standalone schema/QA validator checks any collected result + metadata files for
structural correctness (required keys, correct types, hotel → unit grouping,
provenance, no fabricated values in place of genuinely missing fields):

```bash
.venv/bin/python scripts/validate_results.py data/booking_results_*.json
```

### Known extraction fix (Phase 4)

Live QA across Dhaka, Bogota, Bangkok and Singapore (hotel limits 1–10, varying
dates/guests/rooms) surfaced one real defect: `hotel_name` could absorb hidden DOM
text — once an inline SVG badge's CSS rules, once a promo banner — because the
name was read with raw `textContent` instead of rendered text. Fixed by reading
rendered text (`inner_text`) and preferring the name's own child element under
`#hp_hotel_name` so sibling badges never leak in; pinned with a regression test
(`tests/test_dom_extraction.py`) against a static DOM fixture (no live traffic).

## Destination resolution

Destinations are resolved against Booking.com's live `AutoComplete` GraphQL endpoint
(the same call the production website's search box makes), from the same browser
session — so the resolved `dest_id`/`dest_type` are Booking's current values, not a
hand-maintained list. If the live call ever fails, the optional `cities.booking.json`
file (repo root) is used as a name match fallback; keep it only if you want an offline
fallback.

## Limitations

- **Single-page search, single-run tool.** One destination per run; a bounded number of
  hotels (default 5). Re-run per destination.
- **Reliance on Booking.com's current layout.** Extraction targets Booking's live
  selectors and JSON-LD. Booking redesigns can break selectors and need adjustment.
- **Not datacenter-scale.** No proxy rotation, no CAPTCHA/challenge handling beyond the
  basic headless-browser housekeeping it already does, no distributed queueing.
- **Prices are live "as shown" values** for the given dates/occupancy; they change and
  are not authoritative booking quotes.
- **Some room rows genuinely have no price.** A small share of rate rows (observed on
  partner/collaborator content) render without a visible price on Booking's own page;
  the scraper leaves `price_amount`/`price_text` `null` rather than guessing.
- **A single hotel can fail while the rest of a run succeeds** (e.g. a page that never
  renders a room table); this is reported per-hotel in the run metadata's `failures`
  list, not treated as a whole-run failure.
- **No star rating / review score is captured.** Booking's rating widget is not parsed,
  so hotel cards intentionally omit a rating rather than show a placeholder.
- Entry point is `python -m src.cli` from the repository root; the web UI is a thin
  wrapper over the same code path (`src.webapp` + `src/web/index.html`).

## Layout

```
src/cli.py           CLI (arguments, defaults, demo output)
src/orchestrator.py  orchestration: search -> details -> storage (retries,
                     partial-failure recovery, cancellation, timeout, metadata)
src/client.py        Booking.com browser client (search + detail extraction)
src/errors.py        typed failure classes (retryable vs permanent)
src/destination.py   live destination resolution + config fallback
src/models.py        normalized data models
src/storage.py       grouped JSON writer + companion metadata writer
src/webapp.py        localhost FastAPI app (thin wrapper over the orchestrator)
src/web/index.html   localhost browser UI
booking_scraper/     proven extraction layer (search, detail, browser, utils)
tests/               unit tests for the application-control logic (fake client)
scripts/             live failure/recovery demo (real Booking data)
data/                generated JSON output + debug logs (git-ignored)
```