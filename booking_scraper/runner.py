"""Main orchestration: main() async function, worker setup, queue management."""

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .config import (
    BROWSER_PER_DETAIL,
    DETAIL_WORKERS,
    MAX_IDLE_WAVES,
    ON_OPEN_ESC_WAIT_MS,
    VIEWPORT_W,
    VIEWPORT_H,
    DEFAULT_LANG,
    _KEEP_KEYS,
)
from .models import CityCfg, ParentCtx, UnitRow
from .logger import log
from .utils import (
    build_search_url,
    build_detail_url_with_params,
    ensure_dates,
    hotel_key,
    norm_amenity_flag,
    load_cities,
    safe_count,
    safe_visible,
    safe_text_content,
    text_or_none,
)
from .storage import DB
from .browser import (
    create_context,
    kill_overlays,
    force_scale_1,
    close_any_map,
    quick_dismiss_overlays,
    wait_modal_closed_fast,
)
from .search import (
    count_cards,
    install_card_observer,
    get_card_observer_state,
    wait_spinners,
    wait_lazy_batch,
    detect_serp_scroll_container,
    get_scroll_metrics,
    scroll_once,
    scroll_infinite_step,
    lazy_scroll_until_growth,
    extract_visible_urls,
    click_load_more_if_present,
)
from .detail import (
    extract_units,
    extract_lat_lon_from_ldjson,
    extract_city_country_from_ldjson,
    extract_amenities,
    ensure_availability_loaded,
    get_meta,
    safe_first_text,
    sanitize_url_in_page,
)


# ---------------------------------------------------------------------------
# The main crawler class
# ---------------------------------------------------------------------------

class BookingComCrawler:
    def __init__(self, db: DB, cities: List[CityCfg]):
        self.db = db
        self.cities = cities
        self.browser: Optional[Browser] = None

    # -- housekeeping -------------------------------------------------------

    async def _post_open_housekeeping(self, page: Page):
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(ON_OPEN_ESC_WAIT_MS)
            await page.keyboard.press("Escape")
            await wait_modal_closed_fast(page, 600)
        await force_scale_1(page)

    async def _post_navigate_housekeeping(self, page: Page):
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(ON_OPEN_ESC_WAIT_MS)
            await page.keyboard.press("Escape")
            await wait_modal_closed_fast(page, 800)
        await quick_dismiss_overlays(page, 2)
        await close_any_map(page)
        await force_scale_1(page)
        await install_card_observer(page)

    async def accept_cookies(self, page: Page):
        with contextlib.suppress(Exception):
            btn = page.locator("#onetrust-accept-btn-handler")
            if await safe_count(btn):
                await btn.click()
                await page.wait_for_timeout(400)

    async def wait_for_cards(self, page: Page, timeout_ms: int = 35000):
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

    async def gentle_scroll(self, page: Page, times: int = 2, pause_ms: int = 600):
        await page.wait_for_timeout(50)

    # -- context helpers ----------------------------------------------------

    async def new_context(self, city: Optional[CityCfg]) -> BrowserContext:
        assert self.browser
        return await create_context(self.browser, city)

    # -- detail core --------------------------------------------------------

    async def _detail_core(self, page: Page, slug: str, url: str, listing_status: str,
                           property_type: str, city: Optional[CityCfg], run_id: int):
        metrics = {"modal_attempts": 0, "map_closed_via": "none", "dismiss_rounds": 0,
                   "t_detail_ms": 0, "t_availability_ms": 0}
        t_detail_start = time.time()

        def _conlog(msg):
            try:
                txt = msg.text()
                log("browser.console", text=txt[:500])
            except Exception:
                pass
        page.on("console", _conlog)

        detail_url = build_detail_url_with_params(url, city)
        log("goto DETAIL", url=detail_url, slug=slug)
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)

        await self._post_navigate_housekeeping(page)
        await self.accept_cookies(page)

        rounds = await quick_dismiss_overlays(page, 2)
        metrics["dismiss_rounds"] = rounds
        how_map = await close_any_map(page)
        metrics["map_closed_via"] = how_map

        t_av_start = time.time()
        await ensure_availability_loaded(page)
        metrics["t_availability_ms"] = int((time.time() - t_av_start) * 1000)

        await sanitize_url_in_page(page)

        og_title = await get_meta(page, 'meta[property="og:title"]')
        og_desc = await get_meta(page, 'meta[property="og:description"]')
        h1_text = await safe_first_text(page, "h1, h2")
        hp_name = await safe_first_text(page, "#hp_hotel_name")
        city_ld, country_ld, street_ld = await extract_city_country_from_ldjson(page)
        address = await safe_first_text(page, '[data-testid="address"]') or await safe_first_text(page, "#showMap2 span") or street_ld
        lat, lon = await extract_lat_lon_from_ldjson(page)

        amenities = await extract_amenities(page)
        amenity_flags = {
            "has_fireplace": False,
            "has_private_pool": False,
            "has_view": None,
            "has_air_conditioning": None,
            "has_elevator": None,
            "has_security_24h": None,
            "has_bbq_area": None,
            "has_balcony": None,
            "has_storage_room": None,
            "has_garden": None,
            "has_office_space": None,
        }
        for a in amenities:
            f = norm_amenity_flag(a)
            if f and f in amenity_flags:
                amenity_flags[f] = True

        city_name = city_ld or (city.name if city else None)
        country_name = country_ld

        units = await extract_units(page)

        if not units:
            ts = int(time.time() * 1000)
            shot = Path(f"debug_booking_{slug}_{ts}.png")
            html_path = Path(f"debug_booking_{slug}_{ts}.html")
            with contextlib.suppress(Exception):
                await page.screenshot(path=str(shot), full_page=True)
            with contextlib.suppress(Exception):
                html = await page.content()
                html_path.write_text(html, encoding="utf-8")
            log("no units; dumped debug", slug=slug, png=str(shot), html=str(html_path))

        parent = ParentCtx(
            parent_url=detail_url,
            parent_title=((hp_name or og_title or h1_text) or "")[:120],
            parent_desc=og_desc,
            parent_amenities=amenities,
            parent_amenity_flags=amenity_flags,
            address=address,
            city_name=city_name,
            country_name=country_name,
            lat=lat,
            lon=lon,
            currency=(city.currency if city else "COP"),
            listing_status=listing_status,
            property_type=property_type,
            city=city
        )

        for idx, u in enumerate(units):
            unit_url = f"{detail_url}#unit-{u.unit_id}"
            self.db.add_rental(run_id, detail_url, u, parent)

        metrics["t_detail_ms"] = int((time.time() - t_detail_start) * 1000)
        log("DETAIL done", slug=slug, units=len(units), **metrics)

    # -- detail launchers ---------------------------------------------------

    async def _process_detail_on(self, run_id: int, slug: str, url: str,
                                 listing_status: str, property_type: str, city: Optional[CityCfg]):
        headless_env = os.getenv("HEADLESS", "True").strip()
        headless = False if headless_env in ("0", "false", "False") else True
        async with async_playwright() as p2:
            b = await p2.chromium.launch(
                headless=headless,
                args=["--disable-gpu", "--no-sandbox", f"--lang={DEFAULT_LANG}",
                      "--disable-blink-features=AutomationControlled",
                      f"--window-size={VIEWPORT_W},{VIEWPORT_H}"]
            )
            try:
                ctx = await create_context(b, city)
                page = await ctx.new_page()
                await self._post_open_housekeeping(page)

                try:
                    sizes = await page.evaluate(
                        "({ww: innerWidth, wh: innerHeight, dpr: devicePixelRatio, sw: screen.width, sh: screen.height})")
                    log("viewport.debug.detail", **sizes)
                except:
                    pass

                try:
                    await self._detail_core(page, slug, url, listing_status, property_type, city, run_id)
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
                    with contextlib.suppress(Exception):
                        await ctx.close()
            finally:
                with contextlib.suppress(Exception):
                    await b.close()

    async def process_detail(self, run_id: int, slug: str, url: str,
                             listing_status: str, property_type: str, city: Optional[CityCfg]):
        ctx = await self.new_context(city)
        page = await ctx.new_page()
        await self._post_open_housekeeping(page)

        try:
            sizes = await page.evaluate(
                "({ww: innerWidth, wh: innerHeight, dpr: devicePixelRatio, sw: screen.width, sh: screen.height})")
            log("viewport.debug.detail", **sizes)
        except:
            pass

        try:
            await self._detail_core(page, slug, url, listing_status, property_type, city, run_id)
        except Exception as e:
            log("DETAIL error", slug=slug, error=str(e))
        finally:
            with contextlib.suppress(Exception):
                await page.close()
            with contextlib.suppress(Exception):
                await ctx.close()

    # -- city processing (producer / worker) --------------------------------

    async def process_city(self, run_id: int, city: CityCfg, p, max_hotels: Optional[int] = None):
        list_ctx = await self.new_context(city)
        list_page = await list_ctx.new_page()
        await self._post_open_housekeeping(list_page)

        try:
            sizes = await list_page.evaluate(
                "({ww: innerWidth, wh: innerHeight, dpr: devicePixelRatio, sw: screen.width, sh: screen.height})")
            log("viewport.debug", **sizes)
        except:
            pass

        q: asyncio.Queue[Tuple[str, str, str, str]] = asyncio.Queue()
        seen: set[str] = set()

        async def producer():
            try:
                search_url = build_search_url(city)
                log("goto LIST", url=search_url)
                await list_page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
                await self._post_navigate_housekeeping(list_page)
                await self.accept_cookies(list_page)
                await quick_dismiss_overlays(list_page, 2)
                await close_any_map(list_page)
                await self.wait_for_cards(list_page)

                await kill_overlays(list_page)
                await force_scale_1(list_page)
                await install_card_observer(list_page)
                for _ in range(16):
                    with contextlib.suppress(Exception):
                        await list_page.evaluate("window.scrollBy(0, 900)")
                    await list_page.wait_for_timeout(160)

                scroll_sel = await detect_serp_scroll_container(list_page)
                log("serp.scroll_container", selector=scroll_sel)

                idle_waves = 0
                wave = 0
                last_seen_count = 0

                while True:
                    urls = await extract_visible_urls(list_page)
                    new_this_wave = 0
                    for u in urls:
                        slug = hotel_key(u)
                        if slug in seen:
                            continue
                        seen.add(slug)
                        await q.put((slug, u, "FOR_RENT", "APARTMENT"))
                        new_this_wave += 1
                        if max_hotels is not None and len(seen) >= max_hotels:
                            break

                    if max_hotels is not None and len(seen) >= max_hotels:
                        log("list max_hotels reached", total=len(seen), max_hotels=max_hotels)
                        break

                    cards_now = await count_cards(list_page)
                    metr = await get_scroll_metrics(list_page, scroll_sel)
                    log("list wave", wave=wave, visibles=len(urls), nuevos=new_this_wave,
                        acumulados=len(seen), cards=cards_now, scroll_top=metr.get("top"),
                        client_h=metr.get("ch"), scroll_h=metr.get("sh"),
                        nearBottom=metr.get("nearBottom"))

                    grew = len(seen) > last_seen_count
                    if grew:
                        idle_waves = 0
                        last_seen_count = len(seen)
                    else:
                        idle_waves += 1

                    if metr.get("nearBottom"):
                        clicked = await click_load_more_if_present(
                            list_page, cards_now, 15000,
                            scroll_sel=scroll_sel,
                            _scroll_once_fn=scroll_once,
                        )
                        if clicked:
                            for _ in range(3):
                                await scroll_once(list_page, scroll_sel)
                                await asyncio.sleep(0.25)
                            wave += 1
                            continue

                    grew_by_scroll = await lazy_scroll_until_growth(
                        list_page, scroll_sel, min_delta=2, max_ms=9000)
                    if grew_by_scroll:
                        idle_waves = 0
                        last_seen_count = len(seen)
                        wave += 1
                        continue

                    clicked = await click_load_more_if_present(
                        list_page, cards_now, 15000,
                        scroll_sel=scroll_sel,
                        _scroll_once_fn=scroll_once,
                    )
                    if clicked:
                        for _ in range(2):
                            await scroll_once(list_page, scroll_sel)
                            await asyncio.sleep(0.25)
                        wave += 1
                        continue

                    if idle_waves >= MAX_IDLE_WAVES:
                        with contextlib.suppress(Exception):
                            await list_page.evaluate(
                                """(sel)=>{const el = sel==='document'?(document.scrollingElement||document.documentElement):document.querySelector(sel); if(el) el.scrollTop = el.scrollHeight;}""",
                                scroll_sel
                            )
                        await wait_lazy_batch(list_page, cards_now, time_budget_ms=1800)
                        final_cards = await count_cards(list_page)
                        if final_cards > cards_now:
                            with contextlib.suppress(Exception):
                                await list_page.evaluate("window.scrollTo(0, 0)")
                            await kill_overlays(list_page)
                            await force_scale_1(list_page)
                            idle_waves = 0
                            last_seen_count = len(seen)
                            wave += 1
                            continue

                        log("list exhausted", waves=wave, total=len(seen))
                        break

                    wave += 1
            except Exception as e:
                log("producer error", error=str(e))
            finally:
                for _ in range(DETAIL_WORKERS):
                    await q.put(("", "", "", ""))

        async def worker(wid: int):
            while True:
                slug, url, status, ptype = await q.get()
                if not slug:
                    q.task_done()
                    break
                try:
                    if BROWSER_PER_DETAIL:
                        await self._process_detail_on(run_id, slug, url, status, ptype, city)
                    else:
                        await self.process_detail(run_id, slug, url, status, ptype, city)
                except Exception as e:
                    log("worker error", worker=wid, slug=slug, error=str(e))
                finally:
                    q.task_done()

        prod_task = asyncio.create_task(producer())
        workers = [asyncio.create_task(worker(i)) for i in range(DETAIL_WORKERS)]

        await prod_task
        await q.join()
        await asyncio.gather(*workers, return_exceptions=True)

        with contextlib.suppress(Exception):
            await list_page.close()
        with contextlib.suppress(Exception):
            await list_ctx.close()

    # -- top-level run ------------------------------------------------------

    async def run(self, max_hotels: Optional[int] = None):
        run_id = self.db.run_start()
        success = True
        notes = ""
        try:
            headless_env = os.getenv("HEADLESS", "True").strip()
            headless = False if headless_env in ("0", "false", "False") else True
            log("launching browser", headless=headless)
            async with async_playwright() as p:
                main_browser = await p.chromium.launch(
                    headless=headless,
                    args=[
                        "--disable-gpu", "--no-sandbox", f"--lang={DEFAULT_LANG}",
                        "--disable-blink-features=AutomationControlled",
                        f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
                    ]
                )
                self.browser = main_browser
                for idx, city in enumerate(self.cities):
                    city = ensure_dates(city)
                    log("city start", idx=idx, name=city.name, checkin=city.checkin,
                        checkout=city.checkout, nights=city.nights)
                    try:
                        await self.process_city(run_id, city, p, max_hotels=max_hotels)
                    except Exception as e:
                        success = False
                        msg = f"[city {city.name} error: {e}]"
                        log("city error", name=city.name, error=str(e))
                with contextlib.suppress(Exception):
                    await main_browser.close()
        except Exception as e:
            success = False
            notes += f"[fatal {e}]"
            log("fatal error", error=str(e))
        finally:
            self.db.run_end()
            log("run finished", success=success, notes=notes)


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------

async def main():
    import os
    max_hotels = os.getenv("MAX_HOTELS")
    if max_hotels is not None:
        max_hotels = int(max_hotels)
    else:
        max_hotels = None

    cities = load_cities()
    if not cities:
        return

    db = DB(source="booking_col")
    crawler = BookingComCrawler(db, cities)
    await crawler.run(max_hotels=max_hotels)
    log("Crawler finalizado. Revisa data/booking_results_*.json para los resultados.")
