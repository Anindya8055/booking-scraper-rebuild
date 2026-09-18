# Candidate Audit — Booking.com Production Retrieval

Date of audit: 2026-09-18

Purpose: independently evaluate four existing open-source projects that claim to retrieve real Booking.com hotel search data. Each candidate was cloned into a temporary directory outside this repository, inspected, and exercised with a minimal real Booking.com production search using the candidate's own implementation. No fixture, mock, or synthetic data was used.

Scope control:
- This report does NOT declare the overall project successful.
- This report does NOT choose a final architecture.
- This report does NOT build our scraper.

Raw command logs and observed output are preserved under `evidence/raw/`.

---

## Methodology

For each candidate:

1. Cloned into a separate temp directory under `/tmp/opencode/candidates/`.
2. Inspected LICENSE (actual file contents, not just README claims).
3. Located the exact Booking retrieval entry point and read the retrieval code.
4. Verified the URLs used are Booking.com production endpoints.
5. Installed the repo's documented dependencies and ran the smallest possible real production search test using the repo's own implementation.
6. Captured the raw observable output.
7. Classified the candidate.

Environment notes (same host, network, and NAT IP for all candidates):

- Python venvs built from Python 3.11 (system default is 3.14; repo-pinned deps install cleanly on 3.11).
- Playwright browsers downloaded to a shared cache via each venv's `playwright install chromium`.
- Candidate 3 (Node) was run under Node 20.20.2 (installed via mise) because the repo's dependencies (puppeteer 21 / yargs) break under the host's only preinstalled Node 26.8.1 (`require is not defined in ES module scope` / `Cannot find module './build/index.cjs'`).
- Booking.com serves its normal search UI, but intermittently serves anti-bot variants or requires additional load time. Observed per-candidate flakiness is recorded below; timestamped, independently observable successes are the basis for classification.

---

## Candidate 1 — Edioff/booking-scraper

| Field | Value |
|---|---|
| URL | https://github.com/Edioff/booking-scraper |
| Clone commit | `2e091e579f25b70139259781be3676a0ffa4ef26` (Initial commit, 2026-02-12) |
| License | **MIT** — LICENSE file present with full MIT text (Copyright (c) 2025 Johan Cruz) |

### Booking retrieval entry point

- `booking_scraper/utils.py:104` — `build_search_url(c)` builds the production search URL.
- `booking_scraper/runner.py:310` — producer navigates the page and extracts hotel cards; `wait_for_cards` waits until `[data-testid="property-card"]` elements exist.
- `booking_scraper/runner.py:143` / `utils.py:120` — detail phase via `build_detail_url_with_params()`.

### Production URL confirmation

```python
# utils.py:104-117 (repo's own code)
def build_search_url(c: CityCfg) -> str:
    ...
    return f"https://www.booking.com/searchresults.{c.lang}.html?{urlencode(params)}"
```

Observed constructed URL:

```
https://www.booking.com/searchresults.es.html?dest_id=-592318&dest_type=city&group_adults=2&group_children=0&no_rooms=1&checkin=2026-09-19&checkout=2026-09-21&order=price&lang=es&selected_currency=COP
```

### Dependencies / local run / external services

- `requirements.txt`: `playwright>=1.40.0` (resolved to 1.63.0 in the test venv) + `playwright install chromium`.
- Runs fully locally. No API keys, no paid service, no external credential anywhere in the codebase. Output is written to local JSON (`data/booking_results_<ts>.json`); `_source.url` records the real scrape origin.

### Real run — search phase (repo's own implementation)

Command:

```
cd /tmp/opencode/candidates/booking-scraper
PLAYWRIGHT_BROWSERS_PATH=<cache> /tmp/opencode/venvs/bs/bin/python /tmp/opencode/drivers/bs_test.py
```

Driver uses the repo's `load_cities`, `build_search_url`, `BookingComCrawler.wait_for_cards`, `count_cards`, `extract_visible_urls`, and `create_context` (the repo's own anti-detection context).

Observed output (two independent runs; log: `evidence/raw/edioff_booking_search.log`):

```
cards_on_page: 25
extracted_hotel_urls[0:5]:
  https://www.booking.com/hotel/co/medellin-continental.es.html
  https://www.booking.com/hotel/co/country-medellin.es.html
  https://www.booking.com/hotel/co/canela-house-medellin.es.html
  https://www.booking.com/hotel/co/habitacion-central-con-cama-doble-1.es.html
  https://www.booking.com/hotel/co/le-couple-suite.es.html
first_card_title: Medellin Continental Hotel
first_card_price: COP 90.000   (COP 90,000 — live price on a live card)
```

(First earlier run: 20 cards, same hotels. Both runs returned real data.)

### Real run — detail phase (repo's own implementation)

Command drove the repo's `_detail_core` for the first extracted detail URL. Output file: `evidence/raw/edioff_detail_results.json` (3 room rows):

```
unit_name:    Habitación Doble
price_text:   COP 90.000
price_amount: 90000.0  (COP)
city/country: Bogota / Medellín
lat/lon:      6.2447319, -75.5664159   (real Medellín coordinates)
source url:   https://www.booking.com/hotel/co/medellin-continental.es.html?checkin=2026-09-19&checkout=2026-09-21&...&lang=es&selected_currency=COP#unit-1403364201_412141595_2_0_0
```

### Reliability note

Using the repo's own `create_context` (desktop UA + `navigator.webdriver` spoof) is required; a bare Playwright context (no UA override) is blocked by Booking and never yields cards. With the repo's context, retrieval is reliable.

### Classification: **REAL_DATA_CONFIRMED**

Real Booking.com production search data (live cards, live prices, live geo) was returned by the candidate's own implementation.

---

## Candidate 2 — dominikkollar/accommap

| Field | Value |
|---|---|
| URL | https://github.com/dominikkollar/accommap |
| Clone commit | `b1ea2a6d0b7c5118cae439716eccdacad2b41ff3` (2026-04-03) |
| License | **No LICENSE file in repository.** README states "MIT", but no license text is committed anywhere in the repo. |

### Booking retrieval entry point

- `scraper/booking/scraper.py:15` — `BASE_URL = "https://www.booking.com/searchresults.cs.html"`.
- `scraper/booking/scraper.py:145` — `scrape_booking(max_pages)` is the Booking.com entry point: builds the search URL, loads cards, extracts listing + coordinates from detail pages.
- `scraper/main.py` — orchestrates Trivago, Google Places (optional), Booking.

### Production URL confirmation

```python
# scraper/booking/scraper.py:160-161 (repo's own code)
search_url = f"{BASE_URL}?{urlencode(SEARCH_PARAMS)}"
url = f"{search_url}&offset={offset}"
```

Observed constructed URL:

```
https://www.booking.com/searchresults.cs.html?ss=Jihomoravsk%C3%BD+kraj%2C+%C4%8Cesk%C3%A1+republika&lang=en-gb&sb=1&src_elem=sb&src=searchresults&dest_type=region&ac_position=0&ac_langcode=cs&rows=25
```

Real Booking.com hotel detail pages are also used for coordinate extraction (`_get_coordinates`, `scraper.py:113`).

### Dependencies / local run / external services

- `scraper/requirements.txt`: playwright==1.49.1, beautifulsoup4, httpx, psycopg2-binary, sqlalchemy, geoalchemy2, pydantic, python-dotenv, tenacity, geopy (+ venv browser install).
- The repo's full stack wants PostgreSQL + PostGIS (docker-compose; `DATABASE_URL`). The `scrape_booking()` function itself is standalone and returned records with no database present.
- Google Places requires `GOOGLE_PLACES_API_KEY` (optional; `scraper/main.py:37` skips Google entirely when absent). **Not required for Booking retrieval.** No paid service needed for the Booking component.
- Runs locally. `scrape_booking()` is written to be invoked from repo root with the `scraper` package on the path.

### Real run — Booking component (repo's own implementation)

Command:

```
cd /tmp/opencode/candidates/accommap
PLAYWRIGHT_BROWSERS_PATH=<cache> /tmp/opencode/venvs/ac/bin/python /tmp/opencode/drivers/ac_test.py
```

Driver calls the repo's own `scrape_booking(max_pages=1)` and prints the returned `AccommodationRecord` list.

Observed output — successful run (`evidence/raw/accommap_booking_attempt3.log`, `records_count: 14`), including:

```
Hotel Arkada (Bucovice)      lat 49.15046448572197   lng 17.002321779891645   source booking
Hotel Allvet                 lat 49.25569688640717   lng 16.958639323711395   source booking
Vinařství U Kapličky         lat 48.87029             lng 16.751093            source booking
Hotel Termal Mušov           lat 48.90449109488005   lng 16.578739285469055   source booking
Wellness Hotel Happy Star    lat 48.799788125331986  lng 15.992773175239563   source booking
```

Each record carries a real `source_url`:

```
https://www.booking.com/hotel/cz/arkada-bucovice.en-gb.html?aid=304142&...&srpvid=5ea647734c0a00bc&srepoch=...&from=searchresults
```

(An earlier successful run produced the same real properties with real coordinates.)

### Reliability note

Booking page-load uses `wait_until="networkidle"` with a 30 s timeout, which intermittently times out. In this audit: 4 runs total → 2 returned real data (14 records and an earlier run with the same real properties), 2 returned 0 with log `Timeout loading page 0` (attempts 1 and 2; see `evidence/raw/accommap_booking_attempt1.log`, `evidence/raw/accommap_booking_attempt2.log`). Approx. 50% success on this network.

Price extraction (`[data-testid="price-and-discounted-price"]`) returned `null` on the `.cs.html` cards in observed runs; names, geo, and profile URLs were captured.

### Classification: **REAL_DATA_CONFIRMED**

Real Booking.com production search data (property names, geo coordinates, profile URLs) was returned by the candidate's own implementation. Component-level flaky (networkidle timeouts).

---

## Candidate 3 — shamsulalam1114/TravelScout-AI

| Field | Value |
|---|---|
| URL | https://github.com/shamsulalam1114/TravelScout-AI |
| Clone commit | `c7f8823d6c1e77b5d5071cb3471fa4616aeafb4e` (2026-02-08) |
| License | **No LICENSE file in repository.** README badge + `## 📄 License` say "MIT" but no license text is committed. |

### Booking retrieval entry point

- `backend/scrapers/hotelScrapper.js:23` — `scrapeBookingDotCom(location, checkInDate, checkOutDate)` is the Booking.com entry point (built on `puppeteer-extra` + stealth plugin).
- `backend/scrapers/hotelScrapper.js:105` — `scrapeHotels()` merges Booking.com + Agoda + MakeMyTrip (Booking is one of three concurrent sources).
- `backend/server.js:166` — the API route that surfaces it.

### Production URL confirmation

```js
// hotelScrapper.js:31-33 (repo's own code)
const url = `https://www.booking.com/searchresults.html?ss=${encodeURIComponent(
  location
)}&checkin=${checkInDate}&checkout=${checkOutDate}&group_adults=2&no_rooms=1&group_children=0`;
```

Observed constructed URL:

```
https://www.booking.com/searchresults.html?ss=Prague%2C%20Czech%20Republic&checkin=2026-10-01&checkout=2026-10-04&group_adults=2&no_rooms=1&group_children=0
```

### Dependencies / local run / external services

- `backend/package.json`: puppeteer, puppeteer-extra, puppeteer-extra-plugin-stealth, random-useragent, express, cheerio, axios, @google/generative-ai.
- Runs locally (Node 18–20). Booking retrieval itself needs puppeteer + a Chrome/Chromium binary only. In this audit: `npm install` (313 packages) under Node 20.20.2, run with `PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable` (system Chrome) to avoid a second bundled-Chromium download.
- External/paid services: the **hotel scraper** requires none. The wider app's AI endpoints need `GEMINI_API_KEY` (server returns an "AI features are not configured" response when absent) and server routes reference `MONGODB_URI`; a `.env` file containing these values is **committed to the repository** (values were not read or used). The Booking scraper path does not depend on them.

### Real run — Booking component (repo's own implementation)

Command:

```
cd /tmp/opencode/candidates/TravelScout-AI/backend
PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable mise exec node@20 -- node /tmp/opencode/drivers/ts_retry.js
```

Driver calls the repo's exported `scrapeHotels(...)` (which internally runs `scrapeBookingDotCom`) for Prague, 2026-10-01 → 2026-10-04, and filters results to `source === "Booking.com"`.

Observed output (`evidence/raw/travelscout_booking_retry.log`):

```
attempt 1: elapsed 57.2s  total_hotels 25  booking_dot_com_count 25
attempt 2: elapsed 58.5s  total_hotels  0  booking_dot_com_count  0   (Booking anti-bot variant: <html class=" b_bot">, no property-card in 20s)
attempt 3: elapsed 57.7s  total_hotels 25  booking_dot_com_count 25
```

Real Booking.com results returned (attempts 1 and 3), e.g.:

```
Gallery Hotel SIS   price 23186  link https://www.booking.com/hotel/cz/bonn.html?aid=304142&...&all_sr_blocks=7726416_89028068_0_2_0&...
a&o Prague Rhea     price 29058  link https://www.booking.com/hotel/cz/fortunarhea.html?aid=304142&...
Gregory House       price 44766  link https://www.booking.com/hotel/cz/gregory-house.html?aid=304142&...
imageUrl            https://cf.bstatic.com/xdata/images/hotel/square240/...webp   (live Booking CDN images)
```

(A standalone single run before this also failed with `Hotel cards not found ... class=" b_bot"` — see `evidence/raw/travelscout_booking.log`.)

### Reliability note

Booking intermittently serves an anti-bot page (`class=" b_bot"`), and the code waits only 20 s for `[data-testid="property-card"]`, after which it returns zero hotels for that run. In this audit: 4 runs → 2 succeeded (25 real hotels each), 2 failed (0). Approx. 50% success. Does NOT substitute another website for Booking: `scrapeBookingDotCom` targets `booking.com/searchresults.html` directly.

### Classification: **REAL_DATA_CONFIRMED**

Real Booking.com production search data (hotel names, live prices, real detail/booking links, live CDN images) was returned by the candidate's own implementation. Intermittent anti-bot failures mean retries are needed.

---

## Candidate 4 — Anmoldureha/hotels-skill

| Field | Value |
|---|---|
| URL | https://github.com/Anmoldureha/hotels-skill |
| Clone commit | `b577a864e5606d0083490b07d73982290990afdf` (2026-02-16, initial commit) |
| License | **No LICENSE file.** `package.json` declares `"license": "ISC"`. No MIT/other text committed. |

### Booking retrieval entry point

- `booking.py:6` — `search_hotels(location, checkin, checkout, adults)` (Playwright sync), the sole retrieval function.
- `mcp_server.py:49-69` — exposes it as the MCP tool `search_hotels`.

### Production URL confirmation

```python
# booking.py:17 (repo's own code)
url = f"https://www.booking.com/searchresults.html?ss={urllib.parse.quote(location)}&checkin={checkin}&checkout={checkout}&group_adults={adults}&no_rooms=1&group_children=0"
```

Observed constructed URL:

```
https://www.booking.com/searchresults.html?ss=Prague%2C%20Czech%20Republic&checkin=2026-10-01&checkout=2026-10-04&group_adults=2&no_rooms=1&group_children=0
```

### Dependencies / local run / external services

- README's documented path: Python venv + `pip install playwright` + `playwright install chromium`. (`package.json` is tangential; it lists `puppeteer-core` and a nonexistent `booking.js`.)
- Runs fully locally. No API keys, no paid service, no external credentials. `node_modules/` is committed to the repo but is not needed for the Python entry point.

### Real run (repo's own CLI)

Command:

```
cd /tmp/opencode/candidates/hotels-skill
PLAYWRIGHT_BROWSERS_PATH=<cache> /tmp/opencode/venvs/hs/bin/python booking.py "Prague, Czech Republic" 2026-10-01 2026-10-04 2
```

Observed output (`evidence/raw/hotels_skill_booking.log`) — 10 real hotels, including:

```
Avenue Legerova 19                        BDT 63,594   Scored 9.0   https://www.booking.com/hotel/cz/legerova-19.en-gb.html?aid=304142&...&srpvid=01b149bb2de00373&srepoch=...
Congress & Wellness Hotel Olsanka         BDT 46,080   Scored 7.9   https://www.booking.com/hotel/cz/olsanka.en-gb.html?aid=304142&...&srpvid=66db4744bc6f022c&...
Havelska Old Town Residence               BDT 147,254  Scored 8.0   https://www.booking.com/hotel/cz/havelska-old-town-residence.en-gb.html?...
Admiral Botel                             BDT 54,727   Scored 8.7
Perla Hotel                               BDT 88,860   Scored 8.8
```

Live geo-dependent pricing is visible: Booking geolocated this host to Bangladesh and returned prices in BDT.

### Reliability note

Two runs, both successful (10 real hotels each). Prices display without a currency parameter because the code does not force a currency; Booking returns the geo-located default.

### Classification: **REAL_DATA_CONFIRMED**

Real Booking.com production search data (hotel titles, live prices, ratings, real detail links) was returned by the candidate's own implementation.

---

## Comparison table

| # | Candidate | Commit (date) | License (actual) | Booking URL is production | Retrieval entry point | Runs locally | Paid service / credentials required for Booking retrieval | Real live Booking data observed | Classification |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Edioff/booking-scraper | `2e091e5` (2026-02-12) | MIT (LICENSE file) | Yes — `booking.com/searchresults.{lang}.html` | `utils.py build_search_url` + `runner.py` (Playwright, search + detail) | Yes (Python 3.10+, Playwright) | No | Yes — live cards, names, prices, room units, geo (2 search runs + 1 detail run) | **REAL_DATA_CONFIRMED** |
| 2 | dominikkollar/accommap | `b1ea2a6` (2026-04-03) | None filed; README claims MIT | Yes — `booking.com/searchresults.cs.html` (+ real hotel detail pages) | `scraper/booking/scraper.py scrape_booking()` (Playwright) | Yes (Playwright; full stack wants Postgres) | No (Google Places key optional, unrelated to Booking) | Yes — names, real lat/lng, profile URLs (2 of 4 runs) | **REAL_DATA_CONFIRMED** (flaky) |
| 3 | shamsulalam1114/TravelScout-AI | `c7f8823` (2026-02-08) | None filed; README badge claims MIT | Yes — `booking.com/searchresults.html` | `backend/scrapers/hotelScrapper.js scrapeBookingDotCom()` (Puppeteer-extra) | Yes (Node 18–20 + Chrome) | No (Gemini key exists for app AI features only; unused by Booking scraper) | Yes — names, prices, real links, CDN images (2 of 4 runs) | **REAL_DATA_CONFIRMED** (flaky) |
| 4 | Anmoldureha/hotels-skill | `b577a86` (2026-02-16) | None filed; package.json `ISC` | Yes — `booking.com/searchresults.html` | `booking.py search_hotels()` (Playwright) | Yes (Python + Playwright) | No | Yes — titles, prices, ratings, real links (2 of 2 runs) | **REAL_DATA_CONFIRMED** |

## Summary of findings

- All four candidates reach **real Booking.com production** endpoints and, when their run succeeds, return **genuine live Booking.com data** (hotel names, prices, ratings, links, geo). No candidate relies on another website to stand in for Booking.com, and none uses mocked or fixture responses.
- No candidate requires a paid service or credential **for Booking retrieval**. (accommap optionally uses Google Places; TravelScout-AI optionally uses Gemini for its AI endpoints — both are separate from Booking retrieval.)
- Licensing hygiene is poor: only candidate 1 ships an actual LICENSE file. Candidates 2 and 3 only claim MIT in a README; candidate 4 declares ISC in `package.json` only.
- All Playwright/Puppeteer-based candidates are intermittent on this network because Booking.com anti-bot varies the served page (`class=" b_bot"` or slow/`networkidle`-blocking loads). Retry/backoff behavior, a fixed desktop user agent, and a real browser context are what separate reliable retrievals from empty runs.