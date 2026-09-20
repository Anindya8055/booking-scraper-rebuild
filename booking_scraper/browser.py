"""Browser/context creation, overlay handling, scroll helpers."""

import contextlib
import json
import re
import time
from typing import Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, Page, Locator

from .config import (
    ABORT_MAP_RESOURCES,
    CLOSE_BTN_SELS,
    MODAL_WRAPPERS,
    ON_OPEN_ESC_WAIT_MS,
    VIEWPORT_W,
    VIEWPORT_H,
    DEVICE_SCALE,
    DEFAULT_LANG,
    _KEEP_KEYS,
    _UNWANTED_KEYS,
)
from .models import CityCfg
from .logger import log
from .utils import safe_count, safe_visible


# ---------------------------------------------------------------------------
# Overlay / modal helpers
# ---------------------------------------------------------------------------

async def _js_click_force(page: Page, selector: str) -> bool:
    try:
        ok = await page.evaluate("""
        (sel) => {
          const el = document.querySelector(sel);
          if (!el) return false;
          try { el.scrollIntoView({block:'center', inline:'center'}); } catch {}
          try { el.click(); return true; } catch {}
          try {
            el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
            return true;
          } catch {}
          const btn = el.closest('button');
          if (btn) {
            try { btn.click(); return true; } catch {}
            try {
              btn.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
              return true;
            } catch {}
          }
          return false;
        }""", selector)
        log("modals.js_click_force", selector=selector, ok=bool(ok))
        return bool(ok)
    except Exception as e:
        log("modals.js_click_force.error", selector=selector, error=str(e))
        return False


async def wait_modal_closed_fast(page: Page, timeout_ms: int = 1500) -> bool:
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        open_any = False
        for sel in MODAL_WRAPPERS:
            try:
                if await page.locator(sel).first.is_visible():
                    open_any = True
                    break
            except:
                continue
        if not open_any:
            log("modals.wait_closed", ms=int((time.time() - t0) * 1000))
            return True
        await page.wait_for_timeout(80)
    log("modals.wait_closed.timeout", ms=timeout_ms)
    return False


async def kill_overlays(page: Page):
    for _ in range(3):
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)

    with contextlib.suppress(Exception):
        b = page.locator("#onetrust-accept-btn-handler").first
        if await b.is_visible():
            await b.click(timeout=800)

    for s in ("[data-modal-close]", ".bui-modal__close", "button[aria-label*='Cerrar' i]"):
        with contextlib.suppress(Exception):
            el = page.locator(s).first
            if await el.is_visible():
                await el.click(force=True, timeout=500)
                await page.wait_for_timeout(80)

    with contextlib.suppress(Exception):
        await page.evaluate("""
        (() => {
          const q = '.map-modal-full, .map_full_overlay, [data-testid="map-full-screen"]';
          const o = document.querySelector(q); if (o) o.style.display='none';
          document.documentElement.style.overflow='';
          document.body.style.overflow='';
          document.body.classList.remove('modal-open','map-open');
        })();""")


async def force_scale_1(page: Page):
    with contextlib.suppress(Exception):
        await page.evaluate("""
        (() => {
          let m = document.querySelector('meta[name=viewport]');
          if(!m){ m=document.createElement('meta'); m.name='viewport'; document.head.appendChild(m); }
          m.content='width=device-width, initial-scale=1, maximum-scale=1';
          document.documentElement.style.zoom='100%';
          document.body.style.zoom='100%';
        })();""")


async def close_any_map(page: Page) -> str:
    map_sel = '.map-modal-full, .map_full_overlay, [data-testid="map-full-screen"]'
    btn_sel = [
        'button[aria-label*="Cerrar" i]',
        'button[aria-label*="Close" i]',
        'button[aria-label*="Fechar" i]',
        'button[aria-label*="Chiudi" i]',
        'button[aria-label*="Schließen" i]',
        '.js-map-modal-close',
        '.map_full_overlay__close',
    ]
    try:
        has_map = await page.evaluate('(s)=>!!document.querySelector(s)', map_sel)
        if not has_map:
            return 'none'
    except:
        pass

    try:
        for _ in range(3):
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(60)
            closed = await page.evaluate('(s)=>!document.querySelector(s)', map_sel)
            if closed:
                await page.evaluate("""
                    () => { document.documentElement.style.overflow=''; document.body.style.overflow='';
                            document.body.classList.remove('modal-open','map-open'); }
                """)
                return 'escape'
    except:
        pass

    try:
        for sel in btn_sel:
            btn = page.locator(sel).first
            if await safe_count(btn):
                with contextlib.suppress(Exception):
                    await btn.click(force=True, timeout=400)
                await page.wait_for_timeout(60)
                closed = await page.evaluate('(s)=>!document.querySelector(s)', map_sel)
                if closed:
                    await page.evaluate("""
                        () => { document.documentElement.style.overflow=''; document.body.style.overflow='';
                                document.body.classList.remove('modal-open','map-open'); }
                    """)
                    return 'force'
    except:
        pass

    try:
        await page.evaluate("""
        (mapSel, btnSelCSV) => {
          const btnSel = btnSelCSV.split(',').map(s=>s.trim());
          for (const s of btnSel) {
            const b = document.querySelector(s);
            if (b) { try { b.click(); } catch {} }
          }
          const overlay = document.querySelector(mapSel);
          if (overlay && overlay.getBoundingClientRect().width > 0) {
            overlay.style.display = 'none';
          }
          document.documentElement.style.overflow='';
          document.body.style.overflow='';
          document.body.classList.remove('modal-open','map-open');
        }""", map_sel, ",".join(btn_sel))
        closed = await page.evaluate('(s)=>!document.querySelector(s)', map_sel)
        if closed:
            return 'js'
    except:
        pass

    return 'none'


async def _count_overlays(page: Page) -> int:
    try:
        return await page.evaluate("""
        () => document.querySelectorAll(
           '.bui-modal, .modal-mask, .modal-background, .lightbox-mask, .bui-modal__overlay, ' +
           '.map-modal-full, .map_full_overlay, [data-testid="modal"], [role="dialog"][aria-modal="true"], [data-testid="rp-content"]'
        ).length
        """)
    except:
        return 0


async def quick_dismiss_overlays(page: Page, rounds: int = 2) -> int:
    total = 0
    for _ in range(rounds):
        before = await _count_overlays(page)
        with contextlib.suppress(Exception):
            await page.keyboard.press('Escape')
        with contextlib.suppress(Exception):
            await page.locator('button[aria-label*="Cerrar" i], [data-modal-close], .bui-modal__close').first.click(force=True)
        await page.wait_for_timeout(120)
        after = await _count_overlays(page)
        if after >= before:
            break
        total += 1
    return total


async def wait_availability(page: Page) -> None:
    sel = '#hprt-table tr[data-block-id], #hprt-table tr[id]'
    try:
        await page.wait_for_selector(sel, timeout=3500, state='visible')
        return
    except:
        pass
    await quick_dismiss_overlays(page, 1)
    await close_any_map(page)
    with contextlib.suppress(Exception):
        await page.evaluate("document.querySelector('#hprt-table')?.scrollIntoView({block:'center'})")
    await page.wait_for_selector(sel, timeout=3500, state='visible')


# ---------------------------------------------------------------------------
# Context creation (shared by both new_context and _new_context_on)
# ---------------------------------------------------------------------------

async def _apply_context_scripts(ctx: BrowserContext) -> None:
    """Apply the common init scripts and route handlers to a browser context."""
    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    await ctx.add_init_script("""
    (() => {
      const keep = new Set(%s);
      const bad = new Set(%s);
      const strip = (url) => {
        try {
          const u = new URL(url, location.origin);
          u.hash = '';
          const kept = new URLSearchParams();
          for (const k of keep) if (u.searchParams.has(k)) kept.set(k, u.searchParams.get(k));
          u.search = kept.toString();
          return u.toString();
        } catch { return url; }
      };
      const origPush = history.pushState.bind(history);
      const origRep  = history.replaceState.bind(history);
      history.pushState = function(state, title, url){
        if (typeof url === 'string') url = strip(url);
        return origPush(state, title, url);
      };
      history.replaceState = function(state, title, url){
        if (typeof url === 'string') url = strip(url);
        return origRep(state, title, url);
      };
      addEventListener('hashchange', () => {
        try {
          const clean = new URL(location.href);
          clean.hash = '';
          history.replaceState(history.state, document.title, clean.toString());
        } catch {}
      }, true);
    })();
    """ % (json.dumps(_KEEP_KEYS), json.dumps(list(_UNWANTED_KEYS))))

    await ctx.add_init_script("""
    (() => {
      const disableMapOpeners = () => {
        for (const sel of ['[data-testid="map-trigger"]','[data-testid="map-opener"]','.map_header__open']) {
          document.querySelectorAll(sel).forEach((el) => {
            if (!el) return;
            el.style.pointerEvents = 'none';
            try { el.setAttribute('aria-disabled','true'); } catch {}
          });
        }
      };
      document.addEventListener('DOMContentLoaded', disableMapOpeners);
      new MutationObserver(disableMapOpeners).observe(document.documentElement, {subtree:true, childList:true});
      document.addEventListener('click', (e) => {
        const t = e.target;
        if (t && (t.closest && t.closest('[data-testid="map-trigger"],[data-testid="map-opener"],.map_header__open'))) {
          e.stopImmediatePropagation();
          e.preventDefault();
        }
      }, true);
    })();
    """)

    if ABORT_MAP_RESOURCES:
        async def route_handler(route, request):
            urlr = request.url
            if any(k in urlr for k in ["googletagmanager", "google-analytics", "doubleclick", "hotjar", "braze"]):
                return await route.abort()
            if re.search(r'/maps?/|mapbox|leaflet|tile|\.map\.js', urlr, re.I):
                return await route.abort()
            return await route.continue_()
        with contextlib.suppress(Exception):
            await ctx.route("**/*", route_handler)
    else:
        async def route_handler(route, request):
            urlr = request.url
            if any(k in urlr for k in ["googletagmanager", "google-analytics", "doubleclick", "hotjar", "braze"]):
                return await route.abort()
            return await route.continue_()
        with contextlib.suppress(Exception):
            await ctx.route("**/*", route_handler)


async def create_context(browser: Browser, city: Optional[CityCfg]) -> BrowserContext:
    """Create a fully-configured browser context on an existing *browser* instance."""
    default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit(537.36) (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ctx = await browser.new_context(
        locale=(city.lang if city else DEFAULT_LANG),
        user_agent=default_ua,
        viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        screen={"width": VIEWPORT_W, "height": VIEWPORT_H},
        device_scale_factor=DEVICE_SCALE,
        java_script_enabled=True,
    )
    await _apply_context_scripts(ctx)
    return ctx
