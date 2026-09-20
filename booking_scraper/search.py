"""Search-page crawling: card extraction, pagination, 'Load more' handling."""

import asyncio
import contextlib
import re
import time
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page

from .config import (
    CARD_SELECTORS,
    SCROLL_STEP_PX,
    SCROLL_JITTER_PX,
    SCROLL_NEAR_BOTTOM_PAD,
    TITLE_ANCHOR_SELECTORS,
    _KEEP_KEYS,
)
from .logger import log
from .utils import (
    clean_url,
    hotel_key,
    safe_count,
    safe_visible,
)
from .browser import (
    kill_overlays,
    force_scale_1,
)


# ---------------------------------------------------------------------------
# Card counting
# ---------------------------------------------------------------------------

async def count_cards(page: Page) -> int:
    for s in CARD_SELECTORS:
        try:
            n = await page.locator(s).count()
            if n:
                return n
        except:
            pass
    try:
        return await page.locator('a[data-testid="title-link"]').count()
    except:
        return 0


# ---------------------------------------------------------------------------
# "Load more" button selectors
# ---------------------------------------------------------------------------

def _button_selectors() -> List[Tuple[str, str]]:
    return [
        ("role", r"Cargar más resultados|Load more results"),
        ("text", r"^Cargar más resultados$"),
        ("text", r"^Load more results$"),
        ("css", "#bodyconstraint-inner > div > div > div.f75d3e1279 > div.c39d0c9226 > div.df80b34405 > div.d3ef0e3593.c7bdc96a10 > div.cca574b93c > div.c3bdfd4ac2.a084498e74 > button"),
        ("css", "#bodyconstraint-inner > div > div > div.f75d3e1279 > div.c39d0c9226 > div.df80b34405 > div.d3ef0e3593.c7bdc96a10 > div.cca574b93c > div.c3bdfd4ac2.a084498e74 > button > span"),
        ("xpath", "//button[.//span[normalize-space()='Cargar más resultados'] or normalize-space()='Cargar más resultados']"),
    ]


async def _click_node_js(page: Page, handle) -> bool:
    try:
        await page.evaluate("""(n)=> {
            try{ n.scrollIntoView({block:'center', inline:'center'});}catch(e){}
            try{ n.click(); return; }catch(e){}
            try{
              n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
            }catch(e){}
        }""", handle)
        return True
    except:
        return False


# ---------------------------------------------------------------------------
# Card observer (MutationObserver in browser)
# ---------------------------------------------------------------------------

async def install_card_observer(page: Page):
    js = r"""
    () => {
      try {
        if (window.__RA_OBS_INSTALLED__) return;
        window.__RA_OBS_INSTALLED__ = true;
        const sel = '[data-testid="property-card"]';
        const state = window.__RA_CARDS__ = { count: 0, delta: 0, lastIncAt: Date.now() };
        const mo = new MutationObserver(muts => {
          let inc = 0;
          for (const m of muts) {
            if (m.addedNodes && m.addedNodes.length) {
              for (const n of m.addedNodes) {
                if (!(n instanceof Element)) continue;
                if (n.matches && n.matches(sel)) inc++;
                inc += n.querySelectorAll ? n.querySelectorAll(sel).length : 0;
              }
            }
          }
          if (inc > 0) {
            state.delta += inc;
            state.count += inc;
            state.lastIncAt = Date.now();
          }
        });
        const first = document.querySelectorAll(sel).length;
        state.count = first;
        mo.observe(document.documentElement, { childList: true, subtree: true });
        window.__RA_MO__ = mo;
      } catch (e) {}
    }"""
    with contextlib.suppress(Exception):
        await page.evaluate(js)


async def get_card_observer_state(page: Page) -> Dict[str, int]:
    try:
        return await page.evaluate("() => (window.__RA_CARDS__ || {count:0, delta:0, lastIncAt:0})")
    except:
        return {"count": 0, "delta": 0, "lastIncAt": 0}


# ---------------------------------------------------------------------------
# Spinner / lazy-batch wait
# ---------------------------------------------------------------------------

async def wait_spinners(page: Page, timeout_ms: int = 6000) -> bool:
    SPIN = '[data-testid*="spinner"], .bui-spinner, [class*="loader"], [aria-busy="true"]'
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        try:
            has = await page.evaluate(f"() => !!document.querySelector('{SPIN}')")
            if not has:
                return True
        except:
            return True
        await page.wait_for_timeout(150)
    return False


async def wait_lazy_batch(page: Page, prev_count: int, time_budget_ms: int = 6000) -> bool:
    t0 = time.time()
    while (time.time() - t0) * 1000 < time_budget_ms:
        state = await get_card_observer_state(page)
        now = await count_cards(page)
        log("lazy_batch.wait", prev=prev_count, now=now, delta=state.get("delta"), lastIncAt=state.get("lastIncAt"))
        if state.get("delta", 0) > 0 or now > prev_count:
            return True
        await wait_spinners(page, 1200)
        await page.wait_for_timeout(180)
    return False


# ---------------------------------------------------------------------------
# Scroll detection & execution
# ---------------------------------------------------------------------------

async def detect_serp_scroll_container(page: Page) -> str:
    js = r"""
    () => {
      const cand = [
        '#searchresultsTmpl',
        '[data-testid="property-list"]',
        '#bodyconstraint-inner .c39d0c9226',
        '#bodyconstraint-inner',
        'main[role="main"]',
        'body'
      ];
      const isScrollable = (el) => {
        if (!el) return false;
        const sh = el.scrollHeight || 0;
        const ch = el.clientHeight || 0;
        if (sh - ch > 300) return true;
        const st = getComputedStyle(el);
        return (st.overflowY === 'auto' || st.overflowY === 'scroll') && ch > 0;
      };
      for (const sel of cand) {
        const el = document.querySelector(sel);
        if (el && isScrollable(el)) return sel;
      }
      return 'document';
    }"""
    try:
        sel = await page.evaluate(js)
        return sel or 'document'
    except:
        return 'document'


async def get_scroll_metrics(page: Page, sel: str) -> Dict[str, int]:
    js = r"""
    (sel, pad) => {
      const el = sel === 'document' ? (document.scrollingElement || document.documentElement) : document.querySelector(sel);
      if (!el) return {ok:false};
      const top = el.scrollTop|0;
      const sh  = el.scrollHeight|0;
      const ch  = el.clientHeight|0;
      const nearBottom = (sh - (top + ch)) < pad;
      return {ok:true, top, sh, ch, nearBottom};
    }"""
    try:
        return await page.evaluate(js, sel, SCROLL_NEAR_BOTTOM_PAD)
    except:
        return {"ok": False}


async def scroll_once(page: Page, sel: str) -> Dict[str, int]:
    js = r"""
    (sel, step, jitter) => {
      const el = sel === 'document' ? (document.scrollingElement || document.documentElement) : document.querySelector(sel);
      if (!el) return {ok:false};
      const prevTop = el.scrollTop|0;
      const rnd = Math.floor(Math.random() * (jitter+1));
      el.scrollTo({top: prevTop + step + rnd, behavior: 'auto'});
      return {ok:true, prevTop, top: el.scrollTop|0, sh: el.scrollHeight|0, ch: el.clientHeight|0};
    }"""
    try:
        return await page.evaluate(js, sel, SCROLL_STEP_PX, SCROLL_JITTER_PX)
    except:
        return {"ok": False}


async def scroll_infinite_step(page: Page):
    try:
        await page.evaluate(
            """(step, jitter) => {
                const el = document.scrollingElement || document.documentElement;
                const cur = el.scrollTop || 0;
                const rnd = Math.floor(Math.random() * (jitter+1));
                el.scrollTo({top: cur + step + rnd, behavior:'auto'});
            }""",
            SCROLL_STEP_PX, SCROLL_JITTER_PX
        )
        await asyncio.sleep(0.2)
    except Exception as e:
        log("scroll_step.error", error=str(e))


async def lazy_scroll_until_growth(page: Page, scroll_sel: str, min_delta: int = 2,
                                   max_ms: int = 9000, step_px: int = 900) -> bool:
    """
    Scrollea en pasos, espera batch lazy, aplica backoff si esta lento.
    Devuelve True si crecieron las cards.
    """
    t0 = time.time()
    backoff = 140
    base_cards = await count_cards(page)

    while (time.time() - t0) * 1000 < max_ms:
        for _ in range(10):
            try:
                await page.evaluate("(dy)=>window.scrollBy(0, dy)", step_px)
            except:
                await scroll_once(page, scroll_sel)
            await page.wait_for_timeout(backoff)

        if await wait_lazy_batch(page, base_cards, time_budget_ms=1600 + backoff * 2):
            state = await get_card_observer_state(page)
            now_cards = await count_cards(page)
            delta = max(state.get("delta", 0), now_cards - base_cards)
            log("lazy_scroll.batch", grew=True, delta=delta, now=now_cards, base=base_cards)
            with contextlib.suppress(Exception):
                await page.evaluate("() => { if (window.__RA_CARDS__) window.__RA_CARDS__.delta = 0; }")
            return True

        backoff = min(backoff + 120, 900)

    with contextlib.suppress(Exception):
        await page.evaluate("() => { if (window.__RA_CARDS__) window.__RA_CARDS__.delta = 0; }")
    log("lazy_scroll.batch", grew=False)
    return False


# ---------------------------------------------------------------------------
# URL extraction from SERP
# ---------------------------------------------------------------------------

async def extract_visible_urls(page: Page) -> List[str]:
    for sel in TITLE_ANCHOR_SELECTORS:
        anchors = page.locator(sel)
        n = await safe_count(anchors)
        if n > 0:
            hrefs: List[str] = []
            for i in range(n):
                href = None
                with contextlib.suppress(Exception):
                    href = await anchors.nth(i).get_attribute("href")
                if href and "/hotel/" in href:
                    hrefs.append(clean_url(href))
            out, seen = [], set()
            for h in hrefs:
                k = hotel_key(h)
                if k not in seen:
                    seen.add(k)
                    out.append(h)
            return out
    return []


# ---------------------------------------------------------------------------
# Review score (SERP card)
#
# Booking.com's search-result cards expose an accessible aria-label such as
# "Scored 8.7, Wonderful, 1,234 reviews, Opens <hotel> information" on
# a[data-testid="review-score-link"]. This technique (the review-score
# container/testid) is documented and demonstrated in two independent,
# actively-maintained open-source GitHub projects --
# brandonshrt/booking.com-scraper (Playwright, `[data-testid="review-score"]`)
# and ALeterouin/Booking_scraper (Selenium, `[data-testid="rating-stars"]`
# for the separate star classification) -- neither of which uses a paid API
# or Booking.com credentials. No Booking.com official page or API was
# consulted to find this; the aria-label format itself was confirmed against
# our own already-legitimate live SERP fetch. A property with no review
# score simply has no matching element -- we never invent one.
# ---------------------------------------------------------------------------
_RATING_ARIA_JS = r"""
() => {
  const out = [];
  const cards = document.querySelectorAll('[data-testid="property-card"]');
  cards.forEach((card) => {
    const a = card.querySelector('a[data-testid="title-link"]') || card.querySelector('a[href*="/hotel/"]');
    if (!a) return;
    const href = a.getAttribute('href');
    if (!href) return;
    const scoreLink = card.querySelector('a[data-testid="review-score-link"], div[data-testid="review-score"]');
    let aria = null;
    if (scoreLink) {
      aria = scoreLink.getAttribute('aria-label') || scoreLink.textContent || null;
    }
    out.push({ href, aria });
  });
  return out;
}
"""
_RATING_ARIA_RE = re.compile(
    r"scored\s+(\d+(?:[.,]\d+)?)\s*,\s*([^,]*),\s*([\d,]+)\s*review", re.I
)


def _parse_rating_aria(aria: Optional[str]) -> Optional[Dict[str, object]]:
    if not aria:
        return None
    m = _RATING_ARIA_RE.search(aria)
    if not m:
        return None
    try:
        score = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    label = (m.group(2) or "").strip() or None
    if label and label.lower() in ("review score",):
        label = None
    count_txt = (m.group(3) or "").replace(",", "").strip()
    count = int(count_txt) if count_txt.isdigit() else None
    return {"score": score, "label": label, "count": count}


async def extract_visible_ratings(page: Page) -> Dict[str, Dict[str, object]]:
    """Return ``{hotel_key(url): {"score", "label", "count"}}`` for cards
    currently in the DOM that genuinely expose a review score. Cards without
    one are simply absent from the result -- never fabricated."""
    out: Dict[str, Dict[str, object]] = {}
    try:
        rows = await page.evaluate(_RATING_ARIA_JS)
    except Exception:
        return out
    for row in rows or []:
        href = row.get("href")
        if not href:
            continue
        parsed = _parse_rating_aria(row.get("aria"))
        if not parsed:
            continue
        out[hotel_key(href)] = parsed
    return out


# ---------------------------------------------------------------------------
# "Load more" click
# ---------------------------------------------------------------------------

async def click_load_more_if_present(page: Page, old_count: int, wait_new_ms: int = 10_000,
                                     scroll_sel: Optional[str] = None,
                                     _scroll_once_fn=None) -> bool:
    """Try to click a 'Load more' button and wait for new cards.

    *_scroll_once_fn* is an optional async callable ``(page, sel) -> dict``
    used when scrolling is needed to bring the button into view.
    """
    await kill_overlays(page)
    await force_scale_1(page)

    base_metrics = await get_scroll_metrics(page, scroll_sel or 'document')
    base_sh = base_metrics.get("sh", 0) if base_metrics.get("ok") else 0

    with contextlib.suppress(Exception):
        btn = page.get_by_role("button", name=re.compile(r"Cargar más resultados|Load more results", re.I)).first
        if await btn.is_visible():
            await btn.scroll_into_view_if_needed()
            await btn.click(force=True)
            t0 = time.time()
            while (time.time() - t0) * 1000 < wait_new_ms:
                now_cards = await count_cards(page)
                now_metrics = await get_scroll_metrics(page, scroll_sel or 'document')
                if now_cards > old_count or (now_metrics.get("ok") and now_metrics.get("sh", base_sh) > base_sh):
                    return True
                await page.wait_for_timeout(200)

    with contextlib.suppress(Exception):
        btn = page.get_by_text(re.compile(r"^Cargar más resultados$", re.I)).first
        if await btn.is_visible():
            await btn.scroll_into_view_if_needed()
            await btn.click(force=True)
            t0 = time.time()
            while (time.time() - t0) * 1000 < wait_new_ms:
                now_cards = await count_cards(page)
                now_metrics = await get_scroll_metrics(page, scroll_sel or 'document')
                if now_cards > old_count or (now_metrics.get("ok") and now_metrics.get("sh", base_sh) > base_sh):
                    return True
                await page.wait_for_timeout(200)

    for kind, sel in _button_selectors():
        try:
            if kind == "css":
                loc = page.locator(sel).first
            elif kind == "xpath":
                loc = page.locator(f"xpath={sel}").first
            elif kind == "text":
                loc = page.get_by_text(re.compile(sel, re.I)).first
            else:
                continue

            if not await loc.count():
                continue

            tag = None
            with contextlib.suppress(Exception):
                tag = await loc.evaluate("(n)=>n.tagName")
            if tag and str(tag).lower() == "span":
                loc = loc.locator("xpath=ancestor::button[1]").first

            for _ in range(6):
                with contextlib.suppress(Exception):
                    await loc.scroll_into_view_if_needed()
                await page.wait_for_timeout(120)
                if await safe_visible(loc):
                    break
                if scroll_sel and _scroll_once_fn:
                    await _scroll_once_fn(page, scroll_sel)

            try:
                await loc.click(force=True, timeout=1200)
            except:
                h = await loc.element_handle()
                if not (h and await _click_node_js(page, h)):
                    continue

            t0 = time.time()
            while (time.time() - t0) * 1000 < wait_new_ms:
                cur_cards = await count_cards(page)
                now_metrics = await get_scroll_metrics(page, scroll_sel or 'document')
                if cur_cards > old_count or (now_metrics.get("ok") and now_metrics.get("sh", base_sh) > base_sh):
                    return True
                await page.wait_for_timeout(300)
        except Exception as e:
            log("load-more click fail", selector=sel, error=str(e))
            continue

    with contextlib.suppress(Exception):
        loc = page.locator(
            "button:has-text('Cargar más resultados'), "
            "button:has-text('Load more results'), "
            "button:has-text('Show more results')"
        ).first
        if await loc.is_visible():
            await loc.scroll_into_view_if_needed()
            await loc.click(force=True)
            t0 = time.time()
            while (time.time() - t0) * 1000 < wait_new_ms:
                now_cards = await count_cards(page)
                now_metrics = await get_scroll_metrics(page, scroll_sel or 'document')
                if now_cards > old_count or (now_metrics.get("ok") and now_metrics.get("sh", base_sh) > base_sh):
                    return True
                await page.wait_for_timeout(200)

    return False
