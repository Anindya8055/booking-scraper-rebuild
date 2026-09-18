"""Configuration: environment variables, constants, and path definitions."""

import json
import os
import re
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CRAWLER_DIR = Path(__file__).resolve().parent.parent
CITIES_CFG_PATH = CRAWLER_DIR / "cities.booking.json"

# ---------------------------------------------------------------------------
# Environment-driven settings
# ---------------------------------------------------------------------------
BROWSER_PER_DETAIL = os.getenv("BROWSER_PER_DETAIL", "1").strip().lower() in ("1", "true", "yes")
ON_OPEN_ESC_WAIT_MS = int(os.getenv("ON_OPEN_ESC_WAIT_MS", "3000"))
ABORT_MAP_RESOURCES = os.getenv("ABORT_MAP_RESOURCES", "0").strip().lower() in ("1", "true", "yes")

DETAIL_WORKERS = int(os.getenv("DETAIL_WORKERS", "3"))
MAX_IDLE_WAVES = int(os.getenv("MAX_IDLE_WAVES", "5"))
SCROLL_STEP_PX = int(os.getenv("SCROLL_STEP_PX", "1200"))
SCROLL_JITTER_PX = int(os.getenv("SCROLL_JITTER_PX", "320"))
SCROLL_NEAR_BOTTOM_PAD = int(os.getenv("SCROLL_NEAR_BOTTOM_PAD", "1600"))

VIEWPORT_W = int(os.getenv("VIEWPORT_W", "1920"))
VIEWPORT_H = int(os.getenv("VIEWPORT_H", "1080"))
DEVICE_SCALE = float(os.getenv("DEVICE_SCALE", "1"))
BOOKING_FORCE_TOMORROW = os.getenv("BOOKING_FORCE_TOMORROW", "1").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
SLUG_RE = re.compile(r"/hotel/[^/]+/([^/.]+)")

# ---------------------------------------------------------------------------
# CSS / XPath selector banks
# ---------------------------------------------------------------------------
CARD_SELECTORS = [
    '[data-testid="property-card"]',
    'div[data-testid="property-card"]',
    'div[data-component="search/list/property"]',
]

TITLE_ANCHOR_SELECTORS = [
    'a[data-testid="title-link"]',
    '[data-testid="property-card"] a[href*="/hotel/"]',
    'a[href*="/hotel/"]',
]

USER_LOAD_MORE_CSS = [
    "#bodyconstraint-inner > div > div > div.f75d3e1279 > div.c39d0c9226 > div.df80b34405 > div.d3ef0e3593.c7bdc96a10 > div.cca574b93c > div.c3bdfd4ac2.a084498e74 > button > span",
    "#bodyconstraint-inner > div > div > div.f75d3e1279 > div.c39d0c9226 > div.df80b34405 > div.d3ef0e3593.c7bdc96a10 > div.cca574b93c > div.c3bdfd4ac2.a084498e74 > button",
]

GENERIC_LOAD_MORE_CSS = [
    'button[aria-label*="Mostrar"]',
    'button[aria-label*="Cargar"]',
    'button[aria-label*="Ver "]',
    'a[aria-label*="Mostrar"]',
    'a[aria-label*="Cargar"]',
    'a[aria-label*="Más"]',
    'button[data-testid*="pagination"]',
    'a[data-testid*="pagination"]',
    'nav[aria-label*="Pag"] a[rel="next"]',
    'a[aria-label*="Next"]',
    'button[aria-label*="Next"]',
]

GENERIC_LOAD_MORE_XPATH = [
    "//button[normalize-space()[contains(., 'Cargar más') or contains(., 'Mostrar más') or contains(., 'Ver más')]]",
    "//a[normalize-space()[contains(., 'Cargar más') or contains(., 'Mostrar más') or contains(., 'Ver más')]]",
    "//button[normalize-space()[contains(., 'Load more') or contains(., 'Show more') or contains(., 'More') or contains(., 'Next')]]",
    "//a[normalize-space()[contains(., 'Load more') or contains(., 'Show more') or contains(., 'More') or contains(., 'Next')]]",
]

OVERLAY_CLOSE = [
    "button[aria-label*='Cerrar']",
    "button[aria-label*='Close']",
    "button[aria-label*='Fechar']",
    "button[aria-label*='Chiudi']",
    "button[aria-label*='Schließen']",
    "button.modal-mask-closeBtn",
    ".bui-modal__close",
    "[data-modal-close]",
    "#b2hotelPage > div.modal-wrapper.hprt-lightbox--wrapper.hprt-lightbox--two_column_layout.hprt-lightbox--wrapperwider > button",
    "#b2hotelPage > div.modal-wrapper.hprt-lightbox--wrapper.hprt-lightbox--two_column_layout.hprt-lightbox--wrapperwider.modal-pos-to-fix > button",
]

CLOSE_BTN_SELS = [
    "button.modal-mask-closeBtn",
    "[data-modal-close]",
    ".bui-modal__close",
    "button[aria-label*='Cerrar' i]",
    "button[aria-label*='Close' i]",
    "button[aria-label*='Fechar' i]",
    "button[aria-label*='Chiudi' i]",
    "button[aria-label*='Schließen' i]",
    "#b2hotelPage > div.modal-wrapper.hprt-lightbox--wrapper.hprt-lightbox--two_column_layout.hprt-lightbox--wrapperwider > button",
    "#b2hotelPage > div.modal-wrapper.hprt-lightbox--wrapper.hprt-lightbox--two_column_layout.hprt-lightbox--wrapperwider > button > span",
    "#b2hotelPage > div.modal-wrapper.hprt-lightbox--wrapper.hprt-lightbox--two_column_layout.hprt-lightbox--wrapperwider.modal-pos-to-fix > button",
    "#b2hotelPage > div.modal-wrapper.hprt-lightbox--wrapper.hprt-lightbox--two_column_layout.hprt-lightbox--wrapperwider.modal-pos-to-fix > button > span",
]

MODAL_WRAPPERS = [
    "[data-testid='rp-content']",
    "[role='dialog'][aria-modal='true']",
    "#b2hotelPage .modal-wrapper.hprt-lightbox--wrapper",
]

# ---------------------------------------------------------------------------
# URL param helpers
# ---------------------------------------------------------------------------
_UNWANTED_KEYS = {"sb", "src", "src_elem", "force_referer", "chal_t", "aid", "ucfs", "lang_clicked", "ss", "ssne"}
_KEEP_KEYS = ["checkin", "checkout", "group_adults", "group_children", "no_rooms", "lang", "selected_currency"]

# ---------------------------------------------------------------------------
# JS runner placeholder
# ---------------------------------------------------------------------------
JS_RUNNER = r"""
// Conserva tu extractor original aquí si lo estás usando.
// window.bookingExtract = { rooms: [...] }
"""

# ---------------------------------------------------------------------------
# Data / log directories (created at import time)
# ---------------------------------------------------------------------------
PROJECT_ROOT = CRAWLER_DIR
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
