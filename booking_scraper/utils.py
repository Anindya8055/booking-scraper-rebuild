"""Pure utility functions (no Playwright dependency except type annotations)."""

import contextlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qs
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import Locator

from .config import (
    BOOKING_FORCE_TOMORROW,
    SLUG_RE,
    _KEEP_KEYS,
    _UNWANTED_KEYS,
    CITIES_CFG_PATH,
)
from .models import CityCfg, UnitRow
from .logger import log


# ---------------------------------------------------------------------------
# Async safe wrappers for Playwright locators
# ---------------------------------------------------------------------------

async def safe_count(locator: Locator) -> int:
    try:
        return await locator.count()
    except:
        return 0


async def safe_visible(locator: Locator) -> bool:
    try:
        return await locator.is_visible()
    except:
        return False


async def safe_inner_text(locator: Locator) -> Optional[str]:
    try:
        t = await locator.inner_text()
        return t.strip()
    except:
        return None


async def safe_text_content(locator: Locator, timeout: Optional[int] = None) -> Optional[str]:
    try:
        if timeout is None:
            txt = await locator.text_content()
        else:
            txt = await locator.text_content(timeout=timeout)
        return txt.strip() if txt else None
    except:
        return None


async def safe_attr(locator: Locator, name: str) -> Optional[str]:
    try:
        return await locator.get_attribute(name)
    except:
        return None


async def safe_all_texts(locator: Locator) -> List[str]:
    try:
        return [t.strip() for t in await locator.all_text_contents()]
    except:
        return []


async def safe_scroll_into_view(locator: Locator):
    try:
        await locator.scroll_into_view_if_needed()
    except:
        pass


# ---------------------------------------------------------------------------
# Text / string helpers
# ---------------------------------------------------------------------------

def text_or_none(s: Optional[str]) -> Optional[str]:
    s = (s or "").strip()
    return s or None


def clean_url(href: str) -> str:
    try:
        u = urlparse(urljoin("https://www.booking.com", href))
        return urlunparse((u.scheme, u.netloc, u.path, "", "", ""))
    except Exception:
        return href


def hotel_key(href: str) -> str:
    m = SLUG_RE.search(href)
    return m.group(1) if m else href


def build_search_url(c: CityCfg) -> str:
    params = {
        "dest_id": c.dest_id,
        "dest_type": c.dest_type,
        "group_adults": str(c.adults),
        "group_children": str(c.children),
        "no_rooms": str(c.rooms),
        "checkin": c.checkin,
        "checkout": c.checkout,
        "order": c.order,
        "lang": c.lang,
        "selected_currency": c.currency,
    }
    return f"https://www.booking.com/searchresults.{c.lang}.html?{urlencode(params)}"


def build_detail_url_with_params(raw_url: str, c: Optional[CityCfg]) -> str:
    try:
        base = urljoin("https://www.booking.com", raw_url)
        u = urlparse(base)

        path = u.path
        if path.endswith(".html") and not path.endswith(".es.html"):
            path = path[:-5] + ".es.html"

        src_qs = parse_qs(u.query)
        params: Dict[str, str] = {}
        for k in _KEEP_KEYS:
            if k in src_qs and src_qs[k]:
                params[k] = src_qs[k][-1]

        if c:
            defaults = {
                "checkin": c.checkin,
                "checkout": c.checkout,
                "group_adults": str(c.adults),
                "group_children": str(c.children),
                "no_rooms": str(c.rooms),
                "lang": c.lang,
                "selected_currency": c.currency,
            }
            for k, v in defaults.items():
                if v and not params.get(k):
                    params[k] = v

        query = urlencode([(k, params[k]) for k in _KEEP_KEYS if k in params and params[k] is not None])
        return urlunparse((u.scheme or "https", u.netloc or "www.booking.com", path, "", query, ""))
    except Exception:
        return raw_url


def parse_money_to_number(txt: Optional[str]) -> Optional[float]:
    if not txt:
        return None
    clean = re.sub(r"[^\d.,-]", "", txt).replace(".", "").replace(",", ".")
    with contextlib.suppress(Exception):
        return float(clean)
    return None


def norm_amenity_flag(name: str) -> Optional[str]:
    x = name.lower()
    if ("aire acondicionado" in x) or ("air conditioning" in x):
        return "has_air_conditioning"
    if ("ascensor" in x) or ("elevator" in x):
        return "has_elevator"
    if ("jardin" in x) or ("jardín" in x) or ("garden" in x):
        return "has_garden"
    if ("parrilla" in x) or ("barbacoa" in x) or ("bbq" in x):
        return "has_bbq_area"
    if ("chimenea" in x) or ("fireplace" in x):
        return "has_fireplace"
    if ("balcon" in x) or ("balcón" in x) or ("balcony" in x):
        return "has_balcony"
    if ("bodega" in x) or ("depósito" in x) or ("storage" in x):
        return "has_storage_room"
    if ("vista" in x) or ("view" in x):
        return "has_view"
    if ("seguridad" in x) or ("24 h" in x) or ("24h" in x):
        return "has_security_24h"
    if ("oficina" in x) or ("workspace" in x):
        return "has_office_space"
    if ("piscina" in x) or ("pool" in x):
        return "has_private_pool"
    return None


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _force_tomorrow_dates(c: CityCfg) -> CityCfg:
    tz = ZoneInfo("America/Bogota")
    today = datetime.now(tz).date()
    checkin = today + timedelta(days=1)
    checkout = checkin + timedelta(days=max(1, c.nights))
    c.checkin = checkin.isoformat()
    c.checkout = checkout.isoformat()
    return c


def ensure_dates(c: CityCfg) -> CityCfg:
    if BOOKING_FORCE_TOMORROW:
        return _force_tomorrow_dates(c)
    if not c.checkin or not c.checkout:
        return _force_tomorrow_dates(c)
    return c


# ---------------------------------------------------------------------------
# City config loader
# ---------------------------------------------------------------------------

def load_cities() -> List[CityCfg]:
    """Load city configurations from the JSON file, creating a sample if absent."""
    cfg_path = CITIES_CFG_PATH
    if not cfg_path.exists():
        example = {
            "cities": [
                {
                    "name": "Colombia",
                    "dest_id": "47",
                    "dest_type": "country",
                    "checkin": "2025-11-19",
                    "checkout": "2025-11-20",
                    "order": "price",
                    "adults": 1,
                    "children": 0,
                    "rooms": 1,
                    "currency": "COP",
                    "lang": "es",
                }
            ]
        }
        cfg_path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
        log("Se creo cities.booking.json de ejemplo en la carpeta del crawler. Edita los IDs y vuelve a ejecutar.")
        return []

    cities_data = json.loads(cfg_path.read_text(encoding="utf-8"))
    cities = [CityCfg(**c) for c in cities_data.get("cities", [])]
    return cities
