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
```

Run `.venv/bin/python -m src.cli --help` for the full help text. Detailed crawl
tracing is kept out of the normal demo output and written to
`data/logs/booking_debug.log` instead.

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
- Entry point is `python -m src.cli` from the repository root.

## Layout

```
src/cli.py           CLI (arguments, defaults, demo output)
src/orchestrator.py  orchestration: search -> details -> storage
src/client.py        Booking.com browser client (search + detail extraction)
src/destination.py   live destination resolution + config fallback
src/models.py        normalized data models
src/storage.py       grouped JSON writer
booking_scraper/     proven extraction layer (search, detail, browser, utils)
data/                generated JSON output + debug logs (git-ignored)
```