"""
Booking.com client for scraping hotel and unit data.
"""

import asyncio
import contextlib
import time
from datetime import datetime
from typing import List, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from booking_scraper.search import (
    count_cards,
    extract_visible_urls,
    install_card_observer,
    detect_serp_scroll_container,
    get_scroll_metrics,
    scroll_once,
    scroll_infinite_step,
    lazy_scroll_until_growth,
    click_load_more_if_present,
    wait_lazy_batch,
)
from booking_scraper.detail import (
    extract_units,
    extract_lat_lon_from_ldjson,
    extract_city_country_from_ldjson,
    extract_property_type_from_ldjson,
    extract_amenities,
    ensure_availability_loaded,
    get_meta,
    safe_first_text,
    sanitize_url_in_page,
)
from booking_scraper.browser import (
    create_context,
    kill_overlays,
    force_scale_1,
    wait_modal_closed_fast,
    quick_dismiss_overlays,
    close_any_map,
)
from booking_scraper.utils import (
    build_search_url,
    hotel_key,
    norm_amenity_flag,
    build_detail_url_with_params,
)
from booking_scraper.logger import log
import booking_scraper.timing as bl_timing
from booking_scraper.config import (
    BROWSER_PER_DETAIL,
    MAX_IDLE_WAVES,
    VIEWPORT_W,
    VIEWPORT_H,
)
from booking_scraper.models import CityCfg, ParentCtx, UnitRow

from .errors import (
    ExtractionError,
    FetchError,
    NavigationError,
    RetryableError,
    SearchPhaseError,
)
from .models import HotelSearchParams, Hotel, HotelUnit, ScrapedHotel


# Pause (ms) before the post-navigation overlay-dismissal loop. Long enough
# for late-rendered overlays to appear, short enough to not be a dead wait.
PRE_DISMISS_WAIT_MS = 900


class BookingScraperClient:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser: Optional[Browser] = None
        from .destination import DestinationResolver
        self._resolver = DestinationResolver()
        self.last_resolved: Optional[dict] = None

    async def __aenter__(self):
        await self._start_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._close_browser()

    async def _start_browser(self):
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--lang=es-ES",
                "--disable-blink-features=AutomationControlLED",
                f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
            ],
        )

    async def _close_browser(self):
        if self.browser:
            await self.browser.close()

    async def _new_context(self, city: Optional[CityCfg] = None) -> BrowserContext:
        assert self.browser
        return await create_context(self.browser, city)

    async def _resolve_city(
        self,
        params: HotelSearchParams,
    ) -> CityCfg:
        """Return a CityCfg for the requested destination.

        Uses a previously-resolved destination when already known, otherwise
        queries Booking.com's live AutoComplete endpoint (with the local
        cities.booking.json config as fallback)."""
        cached = self._resolver.cached(params)
        if cached is not None:
            return self._resolver.apply_params(cached, params)

        context = await self._new_context(None)
        page = await context.new_page()
        try:
            from booking_scraper.browser import (
                kill_overlays,
                force_scale_1,
                quick_dismiss_overlays,
                close_any_map,
            )
            base_url = "https://www.booking.com/index.es.html"
            with bl_timing.timed("dest.resolve"):
                log("goto BASE for resolution", url=base_url)
                await page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
                with contextlib.suppress(Exception):
                    await page.wait_for_timeout(PRE_DISMISS_WAIT_MS)
                    await page.keyboard.press("Escape")
                await quick_dismiss_overlays(page, 2)
                await close_any_map(page)
                await kill_overlays(page)
                await force_scale_1(page)
                resolved = await self._resolver.resolve(page, params)
        finally:
            await page.close()
            await context.close()

        self.last_resolved = {
            "query": params.destination,
            "label": resolved.label,
            "dest_id": resolved.dest_id,
            "dest_type": resolved.dest_type,
            "country_code": resolved.country_code,
            "source": resolved.source,
        }
        log(
            "destination resolved",
            query=params.destination,
            dest_id=resolved.dest_id,
            dest_type=resolved.dest_type,
            source=resolved.source,
        )
        return self._resolver.apply_params(resolved, params)

    async def search_hotel_urls(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int] = None,
    ) -> List[str]:
        """
        Search for hotels and return a list of hotel URLs.
        """
        city_cfg = await self._resolve_city(params)

        context = await self._new_context(city_cfg)
        page = await context.new_page()

        try:
            try:
                with bl_timing.timed("serp.search"):
                    search_url = build_search_url(city_cfg)
                    log("goto LIST", url=search_url)
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
                    await self._post_navigate_housekeeping(page)
                    await quick_dismiss_overlays(page, 2)
                    await close_any_map(page)
                    await install_card_observer(page)
                    await self.wait_for_cards(page)

                    # Scroll to load more hotels
                    await self._scroll_to_load_hotels(page, max_hotels)

                    # Extract hotel URLs
                    urls = await extract_visible_urls(page)
            except FetchError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("Search phase error (retryable)", error=str(e))
                raise SearchPhaseError(f"search failed: {e}") from e
            # Deduplicate
            seen = set()
            unique_urls = []
            for url in urls:
                slug = hotel_key(url)
                if slug not in seen:
                    seen.add(slug)
                    unique_urls.append(url)
                    if max_hotels and len(unique_urls) >= max_hotels:
                        break

            return unique_urls[:max_hotels] if max_hotels else unique_urls
        finally:
            await page.close()
            await context.close()

    async def _scroll_to_load_hotels(self, page: Page, max_hotels: Optional[int]):
        """Scroll the search results page to load more hotels."""
        idle_waves = 0
        wave = 0
        last_seen_count = 0
        seen: set[str] = set()

        while True:
            urls = await extract_visible_urls(page)
            new_this_wave = 0
            for url in urls:
                slug = hotel_key(url)
                if slug in seen:
                    continue
                seen.add(slug)
                new_this_wave += 1

            cards_now = await count_cards(page)
            metr = await get_scroll_metrics(page, await detect_serp_scroll_container(page))
            log(
                "list wave",
                wave=wave,
                visibles=len(urls),
                nuevos=new_this_wave,
                acumulados=len(seen),
                cards=cards_now,
                scroll_top=metr.get("top"),
                client_h=metr.get("ch"),
                scroll_h=metr.get("sh"),
                nearBottom=metr.get("nearBottom"),
            )

            grew = len(seen) > last_seen_count
            if grew:
                idle_waves = 0
                last_seen_count = len(seen)
            else:
                idle_waves += 1

            if max_hotels and len(seen) >= max_hotels:
                log("list max_hotels reached", total=len(seen), max=max_hotels)
                break

            if metr.get("nearBottom"):
                clicked = await click_load_more_if_present(
                    page, cards_now, 15000,
                    scroll_sel=await detect_serp_scroll_container(page),
                    _scroll_once_fn=scroll_once,
                )
                if clicked:
                    for _ in range(3):
                        await scroll_once(page, await detect_serp_scroll_container(page))
                        await asyncio.sleep(0.25)
                    wave += 1
                    continue

            grew_by_scroll = await lazy_scroll_until_growth(
                page, await detect_serp_scroll_container(page), min_delta=2, max_ms=9000
            )
            if grew_by_scroll:
                idle_waves = 0
                last_seen_count = len(seen)
                wave += 1
                continue

            clicked = await click_load_more_if_present(
                page, cards_now, 15000,
                scroll_sel=await detect_serp_scroll_container(page),
                _scroll_once_fn=scroll_once,
            )
            if clicked:
                for _ in range(2):
                    await scroll_once(page, await detect_serp_scroll_container(page))
                    await asyncio.sleep(0.25)
                wave += 1
                continue

            if max_hotels and len(seen) >= max_hotels:
                log("list max_hotels reached", total=len(seen), max=max_hotels)
                break

            if idle_waves >= MAX_IDLE_WAVES:
                # Try to scroll to bottom and wait for lazy load
                with contextlib.suppress(Exception):
                    await page.evaluate(
                        """(sel)=>{const el = sel==='document'?(document.scrollingElement||document.documentElement):document.querySelector(sel); if(el) el.scrollTop = el.scrollHeight;}""",
                        await detect_serp_scroll_container(page),
                    )
                await wait_lazy_batch(page, cards_now, time_budget_ms=1800)
                final_cards = await count_cards(page)
                if final_cards > cards_now:
                    with contextlib.suppress(Exception):
                        await page.evaluate("window.scrollTo(0, 0)")
                    await kill_overlays(page)
                    await force_scale_1(page)
                    idle_waves = 0
                    last_seen_count = len(seen)
                    wave += 1
                    continue

                log("list exhausted", waves=wave, total=len(seen))
                break

            wave += 1

    async def wait_for_cards(self, page: Page, timeout_ms: int = 35000):
        """Wait for hotel cards to appear on the search page."""
        await kill_overlays(page)
        await force_scale_1(page)
        t0 = time.time()
        while (time.time() - t0) * 1000 < timeout_ms:
            n = await count_cards(page)
            if n > 0:
                log("serp.cards.ok", count=n)
                return
            await page.wait_for_timeout(300)
        raise RuntimeError("No aparecieron cards en SERP")

    async def get_hotel_detail(
        self,
        hotel_url: str,
        params: HotelSearchParams,
    ) -> ScrapedHotel:
        """
        Fetch the detail page for a hotel and return a ScrapedHotel.

        Raises a typed FetchError instead of silently returning None: the
        orchestrator uses the error class to decide whether a bounded retry is
        worthwhile. Every attempt opens and closes its own page/context, so an
        interrupted attempt leaves nothing behind.
        """
        city_cfg = await self._resolve_city(params)
        slug = hotel_key(hotel_url)

        with bl_timing.timed(f"detail.total.{slug}"):
            context = await self._new_context(city_cfg)
            page = await context.new_page()

            try:
                return await self._detail_core(
                    page, hotel_url, params, city_cfg
                )
            except FetchError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("Detail error (retryable)", hotel_url=hotel_url, error=str(e))
                raise RetryableError(
                    f"unexpected detail failure: {e}"
                ) from e
            finally:
                await page.close()
                await context.close()

    async def _detail_core(
        self,
        page: Page,
        hotel_url: str,
        params: HotelSearchParams,
        city: CityCfg,
    ) -> ScrapedHotel:
        """Core detail extraction logic."""
        # Build the detail URL with parameters
        detail_url = build_detail_url_with_params(hotel_url, city)
        with bl_timing.timed("detail.nav"):
            log("goto DETAIL", url=detail_url)
            try:
                await page.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("Navigation error (retryable)", url=detail_url, error=str(e))
                raise NavigationError(f"navigation failed: {e}") from e

            await self._post_navigate_housekeeping(page)
            await quick_dismiss_overlays(page, 2)
            await close_any_map(page)

        # Extract hotel information from the detail page
        with bl_timing.timed("detail.info"):
            hotel = await self._extract_hotel_info(page, detail_url, city)
        if not hotel:
            raise ExtractionError(f"no hotel info extracted for {detail_url}")

        # Extract unit information
        with bl_timing.timed("detail.units"):
            units = await self._extract_unit_info(page, hotel, city)
        if not units:
            log("No units found for hotel", hotel_url=detail_url)
            raise ExtractionError(f"no units extracted for {detail_url}")

        # Create the ScrapedHotel
        scraped = ScrapedHotel(
            hotel=hotel,
            units=units,
            source_url=detail_url,
            collected_at=datetime.utcnow().isoformat() + "Z",
            raw_data=None,  # We could store the raw data if needed
        )
        return scraped

    async def _extract_hotel_info(
        self,
        page: Page,
        detail_url: str,
        city: CityCfg,
    ) -> Optional[Hotel]:
        """Extract hotel information from the detail page."""
        # Extract latitude and longitude from LD+JSON
        lat, lon = await extract_lat_lon_from_ldjson(page)

# Extract title and description
        og_title = await get_meta(page, 'meta[property="og:title"]')
        og_desc = await get_meta(page, 'meta[property="og:description"]')
        h1_text = await safe_first_text(page, "h1, h2")
        hp_name = await safe_first_text(page, "#hp_hotel_name")

        city_ld, country_ld, street_ld = await extract_city_country_from_ldjson(page)

        # Extract address
        address = await safe_first_text(page, '[data-testid="address"]') or \
                  await safe_first_text(page, "#showMap2 span") or street_ld

        title = hp_name or og_title or h1_text or ""
        # Use structured page data (JSON-LD address) for city/country.
        # Never parse city/country by splitting the title on commas.
        city_name = city_ld or city.name
        country_name = country_ld

        # Extract property type (real JSON-LD type; no inference)
        property_type = await extract_property_type_from_ldjson(page)

        # Filter property facilities (property-level only, cleaned of page garbage)
        amenities = await extract_amenities(page)
        amenity_flags = {}
        for a in amenities:
            f = norm_amenity_flag(a)
            if f:
                amenity_flags[f] = True

        # Extract hotel id from the URL
        hotel_id = hotel_key(detail_url)

        return Hotel(
            hotel_id=hotel_id,
            hotel_name=title,
            booking_url=detail_url.split("#")[0],  # Remove fragment
            address=address,
            city=city_name,
            country=country_name,
            latitude=lat,
            longitude=lon,
            property_type=property_type,
            description=og_desc,
            amenities=amenities,
            amenity_flags=amenity_flags,
        )

    async def _extract_unit_info(
        self,
        page: Page,
        hotel: Hotel,
        city: CityCfg,
    ) -> List[HotelUnit]:
        """Extract unit/room information from the detail page."""
        units = await extract_units(page)
        hotel_units = []

        for unit in units:
            # Normalize the unit data to our HotelUnit model
            # We have to map from UnitRow to HotelUnit
            # Note: UnitRow might have None for price_currency, we can use the city's currency or parent's currency.
            # In the existing code, the parent's currency is used in storage.
            price_currency = unit.price_currency or city.currency

            hotel_unit = HotelUnit(
                unit_id=unit.unit_id,
                unit_name=unit.unit_name,
                price_amount=unit.price_amount,
                price_currency=price_currency,
                price_text=unit.price_text,
                plan_name=unit.plan_name,
                cancellation=unit.cancellation,
                beds_text=unit.beds_text,
                size_m2=unit.size_m2,
                amenities=list(unit.amenities or []),
                images=list(unit.images or []),
                price_row_text=unit.price_row_text,
                header_chips=unit.header_chips,
                sections=unit.sections,
                long_desc=unit.long_desc,
                comfort_score=unit.comfort_score,
                unique_url=f"{hotel.booking_url}#unit-{unit.unit_id}",
            )
            hotel_units.append(hotel_unit)

        return hotel_units

    # -- Housekeeping methods (copied from the existing runner) --

    async def _post_navigate_housekeeping(self, page: Page):
        import contextlib
        from booking_scraper.browser import wait_modal_closed_fast, quick_dismiss_overlays, close_any_map
        # Short pause so late-appearing overlays can render before we dismiss
        # them; dismissal itself is a poll-and-click loop, so it does not need
        # the full legacy escape wait.
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(PRE_DISMISS_WAIT_MS)
            await page.keyboard.press("Escape")
            await wait_modal_closed_fast(page, 800)
        await quick_dismiss_overlays(page, 2)
        await close_any_map(page)
        from booking_scraper.browser import force_scale_1
        await force_scale_1(page)
        from booking_scraper.search import install_card_observer
        await install_card_observer(page)
