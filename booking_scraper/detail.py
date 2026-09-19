"""Detail page scraping: unit extraction, modal parsing, amenity/location extraction."""

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page, Locator

from .config import (
    CLOSE_BTN_SELS,
    JS_RUNNER,
    _KEEP_KEYS,
)
from .models import CityCfg, ParentCtx, UnitRow
from .logger import log
from .utils import (
    clean_amenity_text,
    clean_beds_text,
    clean_room_name,
    genuine_plan_name,
    is_property_amenity,
    norm_amenity_flag,
    normalize_ws,
    parse_money_to_number,
    safe_count,
    safe_visible,
    safe_inner_text,
    safe_text_content,
    safe_all_texts,
    safe_attr,
    safe_scroll_into_view,
    text_or_none,
)
from .browser import (
    _js_click_force,
    wait_modal_closed_fast,
    kill_overlays,
    force_scale_1,
    close_any_map,
    quick_dismiss_overlays,
    wait_availability,
)


# ---------------------------------------------------------------------------
# Comfort score parser
# ---------------------------------------------------------------------------

def _parse_comfort_score(text_like: Optional[str]) -> Optional[float]:
    if not text_like:
        return None
    m = re.search(r"Camas c[oó]modas:\s*([\d.,]+)", text_like, re.I)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
        return v if (v == v) and (abs(v) != float("inf")) else None
    except:
        return None


# ---------------------------------------------------------------------------
# Unit-level amenities
# ---------------------------------------------------------------------------

_SIZE_CHIP_RE = re.compile(r"^[\d.,]+\s*m\u00b2$", re.I)
_BED_AMENITY_RE = re.compile(r"^(?:\d+\s*[×x]?\s*)?camas?\b|camas?\s+(?:dobles?|individuales?|extras?|supletorias?|verticales?|literas?)", re.I)


def _is_bed_amenity(text: str) -> bool:
    return bool(_BED_AMENITY_RE.search(text or ""))


def _dedupe_preserve(items: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for it in items:
        key = (it or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _unit_amenities(chips: Optional[List[str]],
                    sections: Optional[Dict[str, Any]] = None) -> List[str]:
    """Combine room-level chips/badges and room facility sections into the
    unit amenity list (never property/page-level items)."""
    items: List[str] = []
    for c in (chips or []):
        c2 = clean_amenity_text(c)
        if c2 and not _SIZE_CHIP_RE.match(c2) and not _is_bed_amenity(c2):
            items.append(c2)
    sec = sections or {}
    for key in ("bano_privado", "vista", "equipamiento"):
        for t in (sec.get(key) or []):
            c2 = clean_amenity_text(t)
            if c2 and not _is_bed_amenity(c2):
                items.append(c2)
    if sec.get("politica_humo"):
        c2 = clean_amenity_text(sec.get("politica_humo"))
        if c2 and not _is_bed_amenity(c2):
            items.append(c2)
    return _dedupe_preserve(items)


def _unit_id_for(raw_id: str, taken: set) -> str:
    """Normalize a Booking block id to the room id.

    Booking block ids look like ``{roomid}_{rateid}_{adults}_{children}_{extras}``.
    Collapse to the room id; keep the full id only to disambiguate rows that
    would otherwise collide (same room, distinct rate plans)."""
    base = raw_id
    if base and "_" in base:
        base = base.split("_")[0]
    if base in taken and base != raw_id:
        base = raw_id
    taken.add(base)
    return base


# ---------------------------------------------------------------------------
# Price index builder
# ---------------------------------------------------------------------------

async def build_price_index(page: Page) -> Dict[str, Dict[str, Optional[str]]]:
    js = r"""
    (() => {
      const norm = s => (s||'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
      const priceNumber = s => {
        const only = (s||'').replace(/[^\d.,]/g,'');
        if (!only) return NaN;
        const normalized = only.replace(/\.(?=\d{3}(?:\D|$))/g, '').replace(',', '.');
        const n = Number(normalized);
        return Number.isFinite(n) ? n : NaN;
      };
      const rows = Array.from(document.querySelectorAll('#hprt-table tr[data-block-id], tr.js-rt-block-row'));
      const out = {};
      for (const tr of rows) {
        let id = tr.getAttribute('data-block-id') || '';
        if (!id) continue;
        if (!/^RD/i.test(id)) {
          const m = id.match(/(\d{6,})/);
          if (m) id = `RD${m[1]}`;
        }
        const spans = Array.from(tr.querySelectorAll('.prco-valign-middle-helper'));
        const spanTexts = spans.map(s => norm(s.textContent)).filter(Boolean);
        let best = null;
        if (spanTexts.length) {
          const nums = spanTexts.map(priceNumber);
          let bestIdx = -1, bestNum = Infinity;
          nums.forEach((n, i) => { if (Number.isFinite(n) && n < bestNum) { bestNum = n; bestIdx = i; }});
          best = bestIdx >= 0 ? spanTexts[bestIdx] : spanTexts[0];
        } else {
          const txt = norm(tr.innerText||'');
          const m = txt.match(/(?:COP|USD|EUR|ARS|MXN)\s*[\d.,]+|\$[\s\S]*?[\d.,]+/i);
          best = m ? norm(m[0]) : null;
        }
        const rowText = spanTexts.length ? spanTexts.join(' / ') : null;
        out[id] = {best_price_text: best || null, row_text: rowText || null};
      }
      return out;
    })()
    """
    try:
        data = await page.evaluate(js)
        return data or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# JS-based unit extraction
# ---------------------------------------------------------------------------

async def extract_units_via_js(page: Page) -> List[UnitRow]:
    try:
        await page.evaluate(JS_RUNNER)
        data = await page.evaluate("window.bookingExtract || null")
        if not data or "rooms" not in data:
            return []
        out: List[UnitRow] = []
        rooms = data.get("rooms") or []
        for r in rooms:
            price_text = (r.get("price_text") or r.get("price_best")) or None
            price_amount = parse_money_to_number(price_text) if price_text else None
            price_currency = None
            if price_text:
                m = re.search(r"(COP|USD|EUR|MXN|ARS|CLP|PEN|COL\$|US\$|€|\$)", price_text, re.I)
                if m:
                    price_currency = m.group(1).upper().replace("COL$", "COP").replace("US$", "USD")
            sections = r.get("sections") or {}
            header_chips = r.get("header_chips") or []
            images = r.get("images") or []
            beds_text = None
            beds = r.get("beds")
            if isinstance(beds, list) and beds:
                beds_text = " | ".join(beds[:6])
            raw_amenities = r.get("amenities") or []
            amenities = []
            for a in raw_amenities:
                c = clean_amenity_text(a)
                if c and not _is_bed_amenity(c) and c.lower() not in [x.lower() for x in amenities]:
                    amenities.append(c)
            out.append(UnitRow(
                unit_id=str(r.get("unit_id") or ""),
                unit_name=text_or_none(r.get("unit_name")),
                price_amount=price_amount,
                price_currency=price_currency,
                price_text=text_or_none(price_text),
                plan_name=genuine_plan_name(r.get("plan_name")),
                cancellation=text_or_none(r.get("cancellation")),
                beds_text=clean_beds_text(beds_text),
                size_m2=(float(r["size_m2"]) if (r.get("size_m2") is not None) else None),
                amenities=amenities,
                images=list(images),
                sections=sections,
                header_chips=list(header_chips) if header_chips else None,
                long_desc=text_or_none(r.get("long_desc")),
                comfort_score=(float(r["comfort_score"]) if (r.get("comfort_score") is not None) else None),
                price_row_text=text_or_none(r.get("price_row_text")),
            ))
        return [u for u in out if u.unit_id]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Modal open / close helpers
# ---------------------------------------------------------------------------

async def _collect_img_sources(gal: Any, max_count: int = 12) -> List[str]:
    """Collect image URLs from a set of <img> nodes, covering lazy-loading
    attributes (data-src, srcset) used inside room modals."""
    imgs: List[str] = []
    try:
        n = await gal.count()
        for i in range(min(n, max_count)):
            img = gal.nth(i)
            with contextlib.suppress(Exception):
                src = (await img.get_attribute("src")) or ""
                if not src.startswith("http"):
                    src = (await img.get_attribute("data-src")) or ""
                if not src.startswith("http"):
                    srcset = (await img.get_attribute("srcset")) or ""
                    if srcset:
                        cand = srcset.split(",")[0].strip().split(" ")[0]
                        if cand.startswith("http"):
                            src = cand
                if src and src.startswith("http"):
                    imgs.append(src)
    except Exception:
        pass
    return imgs


async def wait_for_modal_open(page: Page, timeout: int = 9000) -> Optional[Locator]:
    t0 = time.time()
    log("modals.wait_open.start", timeout_ms=timeout)
    while (time.time() - t0) * 1000 < timeout:
        rp = page.locator('[data-testid="rp-content"]').first
        if await safe_visible(rp):
            log("modals.wait_open.ok", type="rp-content", ms=int((time.time() - t0) * 1000))
            return rp
        aria = page.locator('[role="dialog"][aria-modal="true"]').first
        if await safe_visible(aria):
            log("modals.wait_open.ok", type="aria-modal", ms=int((time.time() - t0) * 1000))
            return aria
        legacy = page.locator("#b2hotelPage .modal-wrapper.hprt-lightbox--wrapper").first
        if await safe_visible(legacy):
            log("modals.wait_open.ok", type="legacy", ms=int((time.time() - t0) * 1000))
            return legacy
        await page.wait_for_timeout(80)
    log("modals.wait_open.timeout", timeout_ms=timeout)
    return None


async def wait_for_modal_close(page: Page, timeout: int = 4000) -> bool:
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout:
        rp = await safe_visible(page.locator('[data-testid="rp-content"]').first)
        aria = await safe_visible(page.locator('[role="dialog"][aria-modal="true"]').first)
        legacy = await safe_visible(page.locator("#b2hotelPage .modal-wrapper.hprt-lightbox--wrapper").first)
        if not rp and not aria and not legacy:
            log("modals.wait_close.ok", ms=int((time.time() - t0) * 1000))
            return True
        await page.wait_for_timeout(80)
    log("modals.wait_close.timeout", timeout_ms=timeout)
    return False


async def close_modal_like(page: Page, root: Locator):
    log("modals.close.start")
    for sel in CLOSE_BTN_SELS:
        try:
            loc = page.locator(sel).first if sel.startswith("#b2hotelPage") else root.locator(sel).first
            cnt = await safe_count(loc)
            vis = await safe_visible(loc) if cnt else False
            log("modals.close.try_btn", selector=sel, count=cnt, visible=vis)
            if not cnt or not vis:
                continue
            try:
                tag = (await loc.evaluate("(n)=>n.tagName")).lower()
            except Exception:
                tag = "button"
            target = loc if tag == "button" else loc.locator("xpath=ancestor::button[1]")
            try:
                if await safe_visible(target):
                    await target.click(force=True)
                else:
                    await loc.click(force=True)
                ok = await wait_for_modal_close(page, 4000)
                log("modals.close.btn_click", selector=sel, ok=ok)
                if ok:
                    await page.wait_for_timeout(80)
                    log("modals.close.ok")
                    return
            except Exception as e:
                log("modals.close.btn_click.error", selector=sel, error=str(e))
            js_ok = await _js_click_force(page, sel)
            if js_ok:
                ok = await wait_for_modal_close(page, 1500)
                log("modals.close.btn_js", selector=sel, ok=ok)
                if ok:
                    await page.wait_for_timeout(80)
                    log("modals.close.ok_js")
                    return
        except Exception as e:
            log("modals.close.try_btn.error", selector=sel, error=str(e))
    try:
        await page.keyboard.press("Escape")
        ok = await wait_for_modal_close(page, 1200)
        log("modals.close.escape", ok=ok)
        if ok:
            await page.wait_for_timeout(80)
            log("modals.close.ok_escape")
            return
    except Exception as e:
        log("modals.close.escape.error", error=str(e))
    await page.wait_for_timeout(100)
    log("modals.close.end")


# ---------------------------------------------------------------------------
# Modal content parsers
# ---------------------------------------------------------------------------

async def parse_unit_in_modal_or_rp(page: Page, modal_root: Locator) -> Dict[str, Any]:
    rp = modal_root.locator('[data-testid="rp-content"]').first
    if await safe_count(rp) and await safe_visible(rp):
        return await _parse_rp_content(rp, page)
    rp_page = page.locator('[data-testid="rp-content"]').first
    if await safe_count(rp_page) and await safe_visible(rp_page):
        return await _parse_rp_content(rp_page, page)
    return await _parse_legacy_modal(modal_root)


async def _parse_rp_content(rp: Locator, page: Page) -> Dict[str, Any]:
    def _norm_list(texts: List[str]) -> List[str]:
        return [t.strip() for t in texts if t and t.strip()]

    res: Dict[str, Any] = {}

    name = await safe_text_content(rp.locator('[data-testid="room-name"]').first, timeout=1200)
    if not name:
        name = await safe_text_content(rp.locator('[class*="room_name"], [class*="rt-name"]').first, timeout=1200)
    res["unit_name"] = clean_room_name(name)

    chips = await rp.locator('[data-testid*="badge"], [class*="Chip"], [class*="badge"]').all_text_contents() if await safe_count(rp.locator('[data-testid*="badge"], [class*="Chip"], [class*="badge"]')) else []
    res["header_chips"] = _norm_list(chips)

    long_desc = None
    desc_selectors = [
        '[data-testid="rp-description"]',
        '[data-testid*="description"]',
        '[class*="description"]',
        '[class*="room-description"]',
        '[class*="room_description"]',
        '.room-info-description',
        '.hp-description',
        'p[class*="desc"]',
        'div[class*="desc"]'
    ]
    for sel in desc_selectors:
        try:
            desc_loc = rp.locator(sel).first
            if await safe_count(desc_loc):
                long_desc = await safe_text_content(desc_loc, timeout=1200)
                if long_desc and len(long_desc.strip()) > 10:
                    break
        except:
            pass

    if not long_desc or len(long_desc.strip()) < 10:
        try:
            all_paragraphs = await rp.locator('p, div[class*="text"]').all()
            for p in all_paragraphs:
                text = await safe_text_content(p, timeout=500)
                if text and len(text.strip()) > 50:
                    long_desc = text
                    break
        except:
            pass

    res["long_desc"] = text_or_none(long_desc)

    price_text = await safe_text_content(rp.locator('[data-testid*="price"], [class*="price"], [aria-label*="precio"]').first, timeout=1200)
    res["price_text"] = text_or_none(price_text)

    plan = await safe_text_content(rp.locator('[data-testid*="mealplan"], [data-testid*="plan"], [class*="plan"]').first, timeout=800)
    cancel = await safe_text_content(rp.locator('[data-testid*="cancellation"], [class*="cancellation"]').first, timeout=800)
    res["plan_name"] = genuine_plan_name(plan)
    res["cancellation"] = text_or_none(cancel)

    info_blob = await safe_text_content(rp.locator('[data-testid*="bed"], [data-testid*="size"], [class*="bed"], [class*="size"]').first, timeout=800)
    beds_text = None
    size_m2 = None
    if info_blob:
        beds_text = info_blob
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\u00b2", info_blob, re.I)
        if m:
            with contextlib.suppress(Exception):
                size_m2 = float(m.group(1).replace(",", "."))
    beds_alt = await rp.locator('[data-testid*="bed"], [class*="bed"]').all_text_contents() if await safe_count(rp.locator('[data-testid*="bed"], [class*="bed"]')) else []
    if beds_alt:
        beds_text = " | ".join(_norm_list(beds_alt))[:300]
    size_alt = await safe_text_content(rp.locator(':text("m\u00b2")').first, timeout=300)
    if (size_m2 is None) and size_alt:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\u00b2", size_alt, re.I)
        if m:
            with contextlib.suppress(Exception):
                size_m2 = float(m.group(1).replace(",", "."))

    res["beds_text"] = clean_beds_text(beds_text)
    res["size_m2"] = size_m2

    sections: Dict[str, Any] = {"bano_privado": [], "vista": [], "equipamiento": [], "politica_humo": None}
    lists = rp.locator('ul li, [role="listitem"]').all() if await safe_count(rp.locator('ul li, [role="listitem"]')) else []
    if lists:
        try:
            texts = [await l.text_content() for l in lists]
            texts = _norm_list(texts)
            for t in texts:
                tl = t.lower()
                if "ba\u00f1o" in tl or "ba\u00f1o" in tl:
                    sections["bano_privado"].append(t)
                if "vista" in tl:
                    sections["vista"].append(t)
                if any(k in tl for k in ["cocina", "microondas", "nevera", "tv", "wifi", "sof\u00e1", "secador", "cafetera", "aire", "calefacci\u00f3n"]):
                    sections["equipamiento"].append(t)
                if "fumar" in tl or "no fum" in tl:
                    sections["politica_humo"] = t
        except:
            pass
    res["sections"] = sections

    imgs = []
    gal = rp.locator('img')
    if await safe_count(gal):
        imgs = await _collect_img_sources(gal)
    res["images"] = imgs
    res["unit_amenities"] = _unit_amenities(res.get("header_chips"), sections)

    any_text_blob = " ".join([
        text_or_none(long_desc) or "",
        text_or_none(beds_text) or "",
        " ".join(res.get("header_chips") or [])
    ])
    res["comfort_score"] = _parse_comfort_score(any_text_blob)

    return res


async def _parse_legacy_modal(modal_root: Locator) -> Dict[str, Any]:
    res: Dict[str, Any] = {}

    name_selectors = [
        '.rt-lightbox-title',
        '#hp_rt_room_gallery_modal_room_name',
        '.hprt-lightbox-room-name',
        '.hprt-roomtype-name',
        'h1', 'h2', 'h3'
    ]
    name = None
    for sel in name_selectors:
        name = await safe_text_content(modal_root.locator(sel).first, timeout=800)
        if name and len(name.strip()) > 0:
            break
    res["unit_name"] = text_or_none(name)

    price_selectors = [
        '.hprt-lightbox-book-price',
        '.hprt-lightbox-reserve .hprt-lightbox-book-price',
        '.prco-ltr-right-align-helper',
        '[class*="price"]'
    ]
    price_text = None
    for sel in price_selectors:
        price_text = await safe_text_content(modal_root.locator(sel).first, timeout=800)
        if price_text and ('COP' in price_text or '$' in price_text or any(c.isdigit() for c in price_text)):
            break
    res["price_text"] = text_or_none(price_text)

    beds_selectors = [
        'ul.room-config li.bedroom_bed_type',
        '.appartment-bed-types-wrapper li',
        '.hprt-lightbox-bed-configuration',
        '.hprt-roomtype-bed'
    ]
    beds_text = None
    for sel in beds_selectors:
        beds_loc = modal_root.locator(sel)
        if await safe_count(beds_loc):
            beds_list = await beds_loc.all()
            if len(beds_list) > 1:
                texts = []
                for bed in beds_list:
                    t = await safe_text_content(bed, timeout=500)
                    if t:
                        texts.append(t.strip())
                if texts:
                    beds_text = " | ".join(texts)
                    break
            else:
                beds_text = await safe_text_content(beds_loc.first, timeout=800)
                if beds_text:
                    break
    res["beds_text"] = clean_beds_text(beds_text)

    size_m2 = None
    size_badge = await safe_text_content(modal_root.locator('.bui-badge:has-text("m\u00b2"), span:has-text("m\u00b2")').first, timeout=500)
    if size_badge:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\u00b2", size_badge, re.I)
        if m:
            with contextlib.suppress(Exception):
                size_m2 = float(m.group(1).replace(",", "."))

    if size_m2 is None:
        blob = " ".join([t for t in [
            await safe_text_content(modal_root.locator(".hprt-room-facilities-block").first, timeout=500),
            await safe_text_content(modal_root.locator('h2:has-text("Tama\u00f1o")').first, timeout=500),
            beds_text or ""
        ] if t])
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\u00b2", blob, re.I)
        if m:
            with contextlib.suppress(Exception):
                size_m2 = float(m.group(1).replace(",", "."))
    res["size_m2"] = size_m2

    badge_selectors = [
        '.bui-badge.bui-badge--outline',
        '.room_highlight_badge--without_borders',
        '.hprt-facilities-facility span.bui-badge',
        '[class*="badge"]',
        '[class*="Chip"]'
    ]
    chips = []
    for sel in badge_selectors:
        badge_loc = modal_root.locator(sel)
        if await safe_count(badge_loc):
            texts = await badge_loc.all_text_contents()
            chips.extend([t.strip() for t in texts if t and t.strip() and t.strip() not in chips])
    res["header_chips"] = chips[:15]

    plan = None
    for plan_sel in (':text("desayuno")', ':text("meal")', ':text("r\u00e9gimen")',
                     ':text("regimen")', '.hprt-lightbox-rateplan', '[data-testid*="mealplan"]'):
        p_loc = modal_root.locator(plan_sel).first
        if await safe_count(p_loc):
            cand = await safe_text_content(p_loc, timeout=500)
            cand = genuine_plan_name(cand)
            if cand:
                plan = cand
                break
    cancel = await safe_text_content(modal_root.locator(':text("cancel"), :text("cancelaci\u00f3n"), :text("reembolso")').first, timeout=500)
    res["plan_name"] = plan
    res["cancellation"] = text_or_none(cancel)

    unit_amenities = [c for c in (chips or [])]
    fac_loc = modal_root.locator(
        '.hprt-room-facilities-block li, [data-testid*="facility"] li, '
        '[class*="facilities"] li, [class*="amenities"] li'
    )
    if await safe_count(fac_loc):
        for t in await safe_all_texts(fac_loc):
            unit_amenities.append(t)
    res["unit_amenities"] = _unit_amenities(unit_amenities)

    imgs = []
    gal = modal_root.locator('img')
    if await safe_count(gal):
        imgs = await _collect_img_sources(gal)
    res["images"] = imgs

    long_desc = None

    try:
        right_container = modal_root.locator('.hprt-lightbox-right-container')
        if await safe_count(right_container):
            paragraphs = await right_container.locator('p').all()
            for p in paragraphs:
                text = await safe_text_content(p, timeout=500)
                if text and len(text.strip()) > 50:
                    if not any(keyword in text.lower() for keyword in ['<!--', 'dataLayer', 'function', 'script']):
                        long_desc = text.strip()
                        break
    except:
        pass

    if not long_desc or len(long_desc) < 10:
        legacy_desc_selectors = [
            '.hprt-lightbox-description',
            '[class*="room-description"]',
            '.room-info-description',
            'p[class*="desc"]',
            'div[class*="desc"]',
            '.hp-description',
            '[class*="description"]'
        ]
        for sel in legacy_desc_selectors:
            try:
                desc_loc = modal_root.locator(sel).first
                if await safe_count(desc_loc):
                    long_desc = await safe_text_content(desc_loc, timeout=800)
                    if long_desc and len(long_desc.strip()) > 10:
                        break
            except:
                pass

    if not long_desc or len(long_desc.strip()) < 10:
        try:
            all_paragraphs = await modal_root.locator('p').all()
            for p in all_paragraphs:
                text = await safe_text_content(p, timeout=500)
                if text and len(text.strip()) > 50:
                    lower_text = text.lower()
                    if not any(kw in lower_text for kw in ['copyright', 'cookie', 'pol\u00edtica', 'terms', 'condiciones']):
                        long_desc = text.strip()
                        break
        except:
            pass

    res["long_desc"] = text_or_none(long_desc)

    comfort_score = None
    try:
        comfort_texts = [
            await safe_text_content(modal_root.locator('.bui-spacer--medium:has-text("Camas c\u00f3modas"), .bui-spacer--medium:has-text("c\u00f3modas")').first, timeout=500),
            await safe_text_content(modal_root.locator('.bui-text--variant-body_2:has-text("Camas"), .bui-text--variant-body_2:has-text("c\u00f3modas")').first, timeout=500),
        ]
        for comfort_text in comfort_texts:
            if comfort_text:
                comfort_score = _parse_comfort_score(comfort_text)
                if comfort_score:
                    break
    except:
        pass

    if comfort_score is None:
        any_text_blob = " ".join(filter(None, [long_desc or "", beds_text or "", " ".join(chips)]))
        comfort_score = _parse_comfort_score(any_text_blob)

    res["comfort_score"] = comfort_score

    return res


# ---------------------------------------------------------------------------
# Main unit extraction (orchestrates JS, legacy, modern, card-based)
# ---------------------------------------------------------------------------

async def extract_units(page: Page) -> List[UnitRow]:
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await wait_modal_closed_fast(page, 600)

    js_units = await extract_units_via_js(page)
    if js_units:
        return js_units

    price_index = await build_price_index(page)

    legacy = page.locator("#hprt-table, #available_rooms, table.hprt-table").first
    if await safe_count(legacy):
        rows = await legacy.locator("tr[data-block-id], tr[id]").all()
        out: List[UnitRow] = []
        taken_ids: set = set()
        for tr in rows:
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")
                await wait_modal_closed_fast(page, 400)

            unit_id = (await tr.get_attribute("data-block-id")) or (await tr.get_attribute("id")) or ""
            unit_id = (unit_id or "").strip()
            if not unit_id:
                continue
            unit_id = _unit_id_for(unit_id, taken_ids)
            name = text_or_none(await safe_inner_text(tr.locator("a.hprt-roomtype-link, .hprt-roomtype-name, .hprt-roomtype").first))
            left = (await safe_inner_text(tr.locator(".hprt-roomtype-bed, .hprt-roomtype-block, .hprt-facilities-block").first)) or ""
            beds_text = text_or_none(left)
            m2 = None
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\u00b2", left or "", flags=re.I)
            if m:
                m2 = float(m.group(1).replace(",", "."))
            price_text = None
            price_amount = None
            price_currency = None
            lookup_id = unit_id.strip()
            if not lookup_id.upper().startswith("RD"):
                mrd = re.search(r"(\d{6,})", lookup_id)
                if mrd:
                    lookup_id = f"RD{mrd.group(1)}"
            pinfo = price_index.get(lookup_id)
            if pinfo and pinfo.get("best_price_text"):
                price_text = pinfo["best_price_text"]
                price_amount = parse_money_to_number(price_text)
                mcur = re.search(r"(COP|USD|EUR|MXN|ARS|CLP|PEN|COL\$|US\$|\u20ac|\$)", price_text, re.I)
                price_currency = mcur.group(1).upper().replace("COL$", "COP").replace("US$", "USD") if mcur else None
            else:
                price_node = tr.locator('[data-testid="price-for-x-nights"], [data-testid="price-subtitle"], [data-testid*="price"], .prd-taxes-and-charges, td span[class*="price"]').first
                _txt = (await safe_inner_text(price_node)) or ""
                if _txt:
                    price_text = _txt
                    price_amount = parse_money_to_number(_txt)
                    mcur = re.search(r"(COP|USD|EUR|MXN|ARS|CLP|PEN|COL\$|US\$|\u20ac|\$)", _txt, re.I)
                    price_currency = mcur.group(1).upper().replace("COL$", "COP").replace("US$", "USD") if mcur else None

            modal_data: Dict[str, Any] = {}
            link = tr.locator("a.hprt-roomtype-link").first
            if await safe_count(link):
                await safe_scroll_into_view(link)
                with contextlib.suppress(Exception):
                    await page.keyboard.press("Escape")
                    await wait_modal_closed_fast(page, 500)
                    await link.click(delay=20)
                    modal_root = await wait_for_modal_open(page, 9000)
                    log("modal.opened", where="DETAIL/extract_units", ok=bool(modal_root))
                    if modal_root:
                        modal_data = await parse_unit_in_modal_or_rp(page, modal_root)
                        await close_modal_like(page, modal_root)
                        log("modal.closed.requested", where="DETAIL/extract_units")

            out.append(UnitRow(
                unit_id=unit_id,
                unit_name=text_or_none(modal_data.get("unit_name") or name),
                price_amount=price_amount,
                price_currency=price_currency,
                price_text=text_or_none(modal_data.get("price_text") or price_text),
                plan_name=modal_data.get("plan_name"),
                cancellation=modal_data.get("cancellation"),
                beds_text=clean_beds_text(modal_data.get("beds_text") or beds_text),
                size_m2=modal_data.get("size_m2") or m2,
                amenities=modal_data.get("unit_amenities") or [],
                images=modal_data.get("images") or [],
                sections=modal_data.get("sections"),
                header_chips=modal_data.get("header_chips"),
                long_desc=modal_data.get("long_desc"),
                comfort_score=modal_data.get("comfort_score"),
                price_row_text=pinfo.get("row_text") if pinfo else None,
            ))
        if out:
            return out

    modern_root = page.locator("#rooms_table .roomstable")
    if await safe_count(modern_root):
        out: List[UnitRow] = []
        taken_ids = set()
        links = await page.locator('#rooms_table .roomstable a[data-testid="rt-name-link"]').all()
        for a in links:
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")
                await wait_modal_closed_fast(page, 400)
            href = (await safe_attr(a, "href")) or ""
            unit_id = href.replace("#", "").strip() or f"room-{len(out)}"
            unit_id = _unit_id_for(unit_id, taken_ids)
            base_name = text_or_none(await safe_inner_text(a))
            lookup_id = unit_id.strip()
            if not lookup_id.upper().startswith("RD"):
                mrd = re.search(r"(RD\d{6,})", lookup_id, re.I)
                if mrd:
                    lookup_id = mrd.group(1).upper()
            pinfo = price_index.get(lookup_id)
            price_text = pinfo.get("best_price_text") if pinfo else None
            price_amount = parse_money_to_number(price_text) if price_text else None
            price_currency = None
            if price_text:
                mcur = re.search(r"(COP|USD|EUR|MXN|ARS|CLP|PEN|COL\$|US\$|\u20ac|\$)", price_text, re.I)
                price_currency = mcur.group(1).upper().replace("COL$", "COP").replace("US$", "USD") if mcur else None

            modal_data: Dict[str, Any] = {}
            with contextlib.suppress(Exception):
                await safe_scroll_into_view(a)
                await page.keyboard.press("Escape")
                await wait_modal_closed_fast(page, 500)
                await a.click(delay=20)
                modal_root = await wait_for_modal_open(page, 9000)
                log("modal.opened", where="DETAIL/extract_units", ok=bool(modal_root))
                if modal_root:
                    modal_data = await parse_unit_in_modal_or_rp(page, modal_root)
                    await close_modal_like(page, modal_root)
                    log("modal.closed.requested", where="DETAIL/extract_units")

            out.append(UnitRow(
                unit_id=unit_id,
                unit_name=text_or_none(modal_data.get("unit_name") or base_name),
                price_amount=price_amount,
                price_currency=price_currency,
                price_text=text_or_none(modal_data.get("price_text")),
                plan_name=modal_data.get("plan_name"),
                cancellation=modal_data.get("cancellation"),
                beds_text=clean_beds_text(modal_data.get("beds_text")),
                size_m2=modal_data.get("size_m2"),
                amenities=modal_data.get("unit_amenities") or [],
                images=modal_data.get("images") or [],
                sections=modal_data.get("sections"),
                header_chips=modal_data.get("header_chips"),
                long_desc=modal_data.get("long_desc"),
                comfort_score=modal_data.get("comfort_score"),
                price_row_text=pinfo.get("row_text") if pinfo else None,
            ))
        if out:
            return out

    cards = page.locator('[data-testid*="room-card"], [data-component*="rooms"], [class*="roomCard"], [class*="hprt-room"]')
    out: List[UnitRow] = []
    n = await safe_count(cards)
    for i in range(n):
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
            await wait_modal_closed_fast(page, 400)
        card = cards.nth(i)
        unit_id = (await safe_attr(card, "data-block-id")) or (await safe_attr(card, "data-room-id")) or (await safe_attr(card, "id")) or f"room-{i}"
        unit_name = text_or_none(await safe_inner_text(card.locator('[data-testid*="room-name"], h3, h4, .hprt-roomtype-name').first))
        lookup_id = unit_id.strip()
        if not lookup_id.upper().startswith("RD"):
            mrd = re.search(r"(RD\d{6,})", lookup_id, re.I)
            if mrd:
                lookup_id = mrd.group(1).upper()
        pinfo = price_index.get(lookup_id)
        price_text = None
        price_amount = None
        cur = None
        if pinfo and pinfo.get("best_price_text"):
            price_text = pinfo["best_price_text"]
            price_amount = parse_money_to_number(price_text)
            mcur = re.search(r"(COP|USD|EUR|MXN|ARS|CLP|PEN|COL\$|US\$|\u20ac|\$)", price_text, re.I)
            cur = mcur.group(1).upper().replace("COL$", "COP").replace("US$", "USD") if mcur else None
            price_row_text = pinfo.get("row_text")
        else:
            price_text = text_or_none(await safe_inner_text(card.locator('[data-testid="price-and-discounted-price"], [data-testid*="final-price"], [data-testid*="price"], .prd-taxes-and-charges, .price').first))
            price_amount = parse_money_to_number(price_text)
            price_row_text = None
            if price_text:
                m = re.search(r"(COP|USD|EUR|MXN|ARS|CLP|PEN|COL\$|US\$|\u20ac|\$)", price_text, re.I)
                if m:
                    cur = m.group(1).upper().replace("COL$", "COP").replace("US$", "USD")
        out.append(UnitRow(
            unit_id=unit_id,
            unit_name=unit_name,
            price_amount=price_amount,
            price_currency=cur,
            price_text=price_text,
            plan_name=None,
            cancellation=None,
            beds_text=None,
            size_m2=None,
            amenities=[],
            price_row_text=price_row_text
        ))
    return out


# ---------------------------------------------------------------------------
# Lat / lon extraction
# ---------------------------------------------------------------------------

async def extract_lat_lon_from_ldjson(page: Page) -> Tuple[Optional[float], Optional[float]]:
    lat: Optional[float] = None
    lon: Optional[float] = None

    scripts = await page.locator('script[type="application/ld+json"]').all()
    for h in scripts:
        raw = await h.text_content()
        if not raw:
            continue
        with contextlib.suppress(Exception):
            jd = json.loads(raw)
            center_str = None
            if isinstance(jd, dict):
                if isinstance(jd.get("hasMap"), dict) and jd["hasMap"].get("center"):
                    center_str = str(jd["hasMap"]["center"])
                elif isinstance(jd.get("hasMap"), str):
                    mm = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", jd["hasMap"])
                    if mm:
                        center_str = f"{mm.group(1)},{mm.group(2)}"
                if jd.get("geo") and jd["geo"].get("latitude") and jd["geo"].get("longitude"):
                    lat = float(jd["geo"]["latitude"])
                    lon = float(jd["geo"]["longitude"])
            if (lat is None or lon is None) and center_str:
                parts = center_str.split(",")
                if len(parts) == 2:
                    lat = float(parts[0])
                    lon = float(parts[1])

    if lat is None or lon is None:
        with contextlib.suppress(Exception):
            html_content = await page.content()
            patterns = [
                r'"latitude":\s*(-?\d+\.?\d*)',
                r'"longitude":\s*(-?\d+\.?\d*)',
                r'"lat":\s*(-?\d+\.?\d*)',
                r'"lng":\s*(-?\d+\.?\d*)',
                r'"lon":\s*(-?\d+\.?\d*)',
                r'latitude=(-?\d+\.?\d*)',
                r'longitude=(-?\d+\.?\d*)',
                r'@(-?\d+\.\d+),(-?\d+\.\d+)',
            ]
            for pattern in patterns:
                if 'latitude' in pattern or 'lat' in pattern:
                    matches = re.findall(pattern, html_content)
                    if matches and lat is None:
                        lat = float(matches[0])
                elif 'longitude' in pattern or 'lng' in pattern or 'lon' in pattern:
                    matches = re.findall(pattern, html_content)
                    if matches and lon is None:
                        lon = float(matches[0])
                elif '@' in pattern:
                    matches = re.findall(pattern, html_content)
                    if matches:
                        lat = float(matches[0][0])
                        lon = float(matches[0][1])
                        break

    if lat is None or lon is None:
        with contextlib.suppress(Exception):
            lat_elem = await page.locator('[data-lat], [data-latitude]').first
            lon_elem = await page.locator('[data-lon], [data-lng], [data-longitude]').first
            if lat_elem:
                lat_val = await lat_elem.get_attribute('data-lat') or await lat_elem.get_attribute('data-latitude')
                if lat_val:
                    lat = float(lat_val)
            if lon_elem:
                lon_val = await lon_elem.get_attribute('data-lon') or await lon_elem.get_attribute('data-lng') or await lon_elem.get_attribute('data-longitude')
                if lon_val:
                    lon = float(lon_val)

    if lat is None or lon is None:
        with contextlib.suppress(Exception):
            map_links = await page.locator('a[href*="maps"], a[href*="showMap"], a[id*="map"]').all()
            for link in map_links:
                href = await link.get_attribute('href')
                if href:
                    match = re.search(r'[@&?](-?\d+\.\d+),(-?\d+\.\d+)', href)
                    if match:
                        lat = float(match.group(1))
                        lon = float(match.group(2))
                        break

    return lat, lon


# ---------------------------------------------------------------------------
# City / country extraction
# ---------------------------------------------------------------------------

_COUNTRY_CODE_TO_NAME = {
    "CO": "Colombia", "AR": "Argentina", "MX": "M\u00e9xico", "ES": "Espa\u00f1a",
    "US": "Estados Unidos", "GB": "Reino Unido", "PE": "Per\u00fa",
    "CL": "Chile", "BR": "Brasil", "UY": "Uruguay", "EC": "Ecuador",
    "VE": "Venezuela", "FR": "Francia", "IT": "Italia", "DE": "Alemania",
    "PT": "Portugal", "CA": "Canad\u00e1", "CR": "Costa Rica",
    "PA": "Panam\u00e1", "DO": "Rep\u00fablica Dominicana", "GT": "Guatemala",
}
_COUNTRY_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_STREETISH_RE = re.compile(
    r"\d|\b(?:calle|carrera|cra\.?|avenida|av\.?|transversal|diagonal|"
    r"circular|autopista|carretera|km|v[aí]a)\b|#",
    re.I,
)


def _clean_locality(text: Optional[str]) -> Optional[str]:
    t = normalize_ws(text)
    if not t:
        return None
    if _STREETISH_RE.search(t):
        return None
    return t


async def extract_city_country_from_ldjson(page: Page) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract city, country and street address from Booking.com structured
    data (JSON-LD `address`).

    Booking sometimes places the street address inside ``addressLocality``
    (e.g. '52135 Calle 19'); in that case the real city is recovered from
    ``streetAddress`` ('52135 Calle 19, Medellín, Colombia')."""
    city: Optional[str] = None
    country: Optional[str] = None
    street: Optional[str] = None
    scripts = await page.locator('script[type="application/ld+json"]').all()
    for h in scripts:
        raw = await h.text_content()
        if not raw:
            continue
        with contextlib.suppress(Exception):
            jd = json.loads(raw)
            nodes = jd if isinstance(jd, list) else [jd]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                addr = node.get("address")
                if not isinstance(addr, dict):
                    continue
                if city is None:
                    cand = _clean_locality(addr.get("addressLocality"))
                    if cand:
                        city = cand
                if street is None:
                    sa = addr.get("streetAddress")
                    if isinstance(sa, str) and sa.strip():
                        street = sa.strip() or None
                if country is None:
                    ctry = addr.get("addressCountry")
                    if isinstance(ctry, str) and ctry.strip():
                        country = ctry.strip()
    if country is None and street:
        last = street.split(",")[-1].strip()
        if last and not _STREETISH_RE.search(last):
            country = last
    if city is None and street:
        parts = [p.strip() for p in street.split(",") if p.strip()]
        if parts and country and (parts[-1].lower() == country.lower()
                                  or _COUNTRY_CODE_RE.match(parts[-1])):
            parts = parts[:-1]
        for part in reversed(parts):
            cand = part
            toks = cand.split()
            if toks and toks[0].isdigit() and len(toks[0]) >= 3:
                cand = " ".join(toks[1:]).strip() or None
            if cand and not _STREETISH_RE.search(cand):
                city = cand
                break
    if country and _COUNTRY_CODE_RE.match(country):
        country = _COUNTRY_CODE_TO_NAME.get(country, country)
    return text_or_none(city), text_or_none(country), text_or_none(street)


# ---------------------------------------------------------------------------
# Property type extraction
# ---------------------------------------------------------------------------

_KNOWN_PROPERTY_TYPES = {
    "Hotel", "Apartment", "Resort", "Hostel", "GuestHouse", "BedAndBreakfast",
    "Motel", "Aparthotel", "Inn", "Ryokan", "Villa", "House", "Bungalow",
    "Cottage", "Cabin", "Chalet", "FarmStay", "Campground",
}


async def extract_property_type_from_ldjson(page: Page) -> Optional[str]:
    """Return the property type from Booking.com structured data.

    Only values that Booking actually emits as a property type (``@type`` of the
    accommodation entity) are accepted; no inference or fallback is applied."""
    scripts = await page.locator('script[type="application/ld+json"]').all()
    for h in scripts:
        raw = await h.text_content()
        if not raw:
            continue
        with contextlib.suppress(Exception):
            jd = json.loads(raw)
            nodes: List[Any] = []
            if isinstance(jd, list):
                nodes.extend(jd)
            elif isinstance(jd, dict):
                graph = jd.get("@graph")
                if isinstance(graph, list):
                    nodes.extend(graph)
                else:
                    nodes.append(jd)
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                typ = node.get("@type")
                if isinstance(typ, list):
                    for t in typ:
                        if isinstance(t, str) and t in _KNOWN_PROPERTY_TYPES:
                            return t
                elif isinstance(typ, str) and typ in _KNOWN_PROPERTY_TYPES:
                    return typ
    return None


# ---------------------------------------------------------------------------
# Amenities extraction
# ---------------------------------------------------------------------------

_FACILITY_ROOT_SELECTORS = [
    'section[data-testid="property-facilities"]',
    '[data-testid="property-facilities"]',
    '[data-testid="property-amenities"]',
    'div[id="facilities"]',
    'section[id="facilities"]',
    '#facilities',
]


async def _facility_roots(page: Page) -> List[Locator]:
    roots: List[Locator] = []
    for sel in _FACILITY_ROOT_SELECTORS:
        loc = page.locator(sel).first
        if await safe_count(loc) and await safe_visible(loc):
            roots.append(loc)
    cls = page.locator('[class*="facilities"], [class*="amenities"]')
    n = await safe_count(cls)
    for i in range(min(n, 8)):
        node = cls.nth(i)
        try:
            text = (await node.inner_text() or "")
            if not text.strip():
                continue
            if await safe_count(node.locator("li")) == 0:
                continue
            low = text[:400].lower()
            if any(k in low for k in ("instalaciones", "facilities", "amenities", "servicios")):
                roots.append(node)
        except Exception:
            continue
    return roots


async def extract_amenities(page: Page) -> List[str]:
    """Property-level facilities/services only.

    Scopes extraction to the hotel facilities section and filters out
    breadcrumbs, navigation, footer links, review scores, FAQ text, nearby
    attractions and room-level amenities."""
    try:
        roots = await _facility_roots(page)
        if not roots:
            roots = [page.locator("body")]
        collected: List[str] = []
        seen: set[str] = set()
        for root in roots:
            lis = root.locator("li")
            if await safe_count(lis) == 0:
                continue
            for t in await safe_all_texts(lis):
                if not is_property_amenity(t):
                    continue
                clean = clean_amenity_text(t)
                if not clean:
                    continue
                key = clean.lower()
                if key in seen:
                    continue
                seen.add(key)
                collected.append(clean)
        return collected
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Availability loader
# ---------------------------------------------------------------------------

async def ensure_availability_loaded(page: Page):
    from .browser import wait_availability as _wait_availability

    async def _accept_cookies(page: Page):
        with contextlib.suppress(Exception):
            btn = page.locator("#onetrust-accept-btn-handler")
            if await safe_count(btn):
                await btn.click()
                await page.wait_for_timeout(400)

    await _accept_cookies(page)
    await quick_dismiss_overlays(page, 1)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=15_000)

    see = page.locator(
        'button:has-text("Ver disponibilidad"), a:has-text("Ver disponibilidad"), '
        'button:has-text("Mostrar precios"), a:has-text("Mostrar precios"), '
        'button:has-text("See availability"), a:has-text("See availability")'
    )
    if await safe_count(see):
        btn = see.first
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
            await wait_modal_closed_fast(page, 500)
        with contextlib.suppress(Exception):
            await btn.click(delay=20)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=8_000)

    try:
        await _wait_availability(page)
        log("availability ready", winner="#hprt-table rows")
        return
    except:
        pass

    candidates = [
        "#hprt-table tr[data-block-id], #hprt-table tr[id]",
        "#rooms_table .roomstable",
        '[data-testid*="room-card"], [data-component*="rooms"], [class*="roomCard"]',
    ]
    for _ in range(10):
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await safe_visible(loc):
                    log("availability ready", winner=sel)
                    return
                await loc.wait_for(state="visible", timeout=300)
                log("availability ready", winner=sel)
                return
            except Exception:
                continue
        await page.wait_for_timeout(500)
    log("availability not detected (continuing with fallbacks)")


# ---------------------------------------------------------------------------
# Helper: get meta tag content
# ---------------------------------------------------------------------------

async def get_meta(page: Page, sel: str) -> Optional[str]:
    with contextlib.suppress(Exception):
        loc = page.locator(sel)
        if await safe_count(loc):
            val = await loc.first.get_attribute("content")
            return text_or_none(val)
    return None


async def safe_first_text(page_or_loc, sel: str, timeout: int = 1500) -> Optional[str]:
    with contextlib.suppress(Exception):
        loc = page_or_loc.locator(sel).first
        if await safe_count(loc):
            t = await safe_text_content(loc, timeout=timeout)
            return text_or_none(t)
    return None


# ---------------------------------------------------------------------------
# URL sanitization in page
# ---------------------------------------------------------------------------

async def sanitize_url_in_page(page: Page):
    try:
        await page.evaluate("""
        (keep) => {
          try {
            const u = new URL(location.href);
            const kept = new URLSearchParams();
            for (const k of keep) if (u.searchParams.has(k)) kept.set(k, u.searchParams.get(k));
            u.search = kept.toString();
            u.hash = '';
            history.replaceState(history.state, document.title, u.toString());
          } catch {}
        }
        """, _KEEP_KEYS)
        log("url.sanitized", ok=True)
    except Exception as e:
        log("url.sanitized.error", error=str(e))
