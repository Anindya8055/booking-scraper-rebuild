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
# Data-quality normalization helpers
# ---------------------------------------------------------------------------

def normalize_ws(s: Optional[str]) -> Optional[str]:
    """Collapse every whitespace run (newlines, tabs, NBSP) to a single space."""
    if not s:
        return None
    return re.sub(r"\s+", " ", s).strip() or None


_REVIEW_METRIC_RE = re.compile(
    r"\b(?:camas c.{0,2}modas|camas confortables|comodidad|limpieza|"
    r"ubicaci.{0,2}n|confort|personal|relaci.{0,2}n calidad-precio|"
    r"valor por dinero|puntaje general)\b",
    re.I,
)
_REVIEW_CITE_RE = re.compile(
    r"seg[uú]n\s+\d+(?:[.,]\d+)?\s+(?:comentarios|reseñas|rese\S*|"
    r"opiniones|reviews|votos)|basado en\s+\d+|based on\s+\d+",
    re.I,
)
_BED_KIND_RE = re.compile(
    r"\d+\s*(?:cama|bed)|"
    r"\b(?:individual|matrimonial|doble|triple|cu[aá]druple|twin|"
    r"single|double|queen|king|cuna|supletoria|extra grande|"
    r"extragrande|de bronce|de hierro)\b",
    re.I,
)


def clean_beds_text(text: Optional[str]) -> Optional[str]:
    """Return genuine room bed info, or None.

    Drops review-score text like 'Camas cómodas: 7,9, según 12 comentarios',
    which is not real bed data. Kept only when the text actually describes
    beds ('1 cama individual y 1 cama doble')."""
    t = normalize_ws(text)
    if not t:
        return None
    if _REVIEW_METRIC_RE.search(t) or _REVIEW_CITE_RE.search(t):
        t = _REVIEW_CITE_RE.sub("", t)
        t = re.sub(r"[:\-]?\s*[\d.,]+(?:[.,]\d+)?\s*,?\s*$", "", t)
        t = re.sub(r"[:\-]\s*$", "", t).strip()
    if not _BED_KIND_RE.search(t):
        return None
    return re.sub(r"\s+", " ", t).strip() or None


def clean_room_name(name: Optional[str]) -> Optional[str]:
    """Normalize a room/unit name, dropping section headings (e.g. the modal
    heading 'En el baño privado:') that are not actually room names."""
    t = normalize_ws(name)
    if not t:
        return None
    if len(t) < 3:
        return None
    if t.endswith((":", "\u00a0")):
        return None
    if re.match(r"^(en|desde|la |el |los |las )(?=\S*:)", t, re.I):
        return None
    return t


_PLAN_PATTERNS = (
    r"\bdesayuno\b",
    r"\bbreakfast\b",
    r"\bmedia pensi[oó]n\b",
    r"\bhalf board\b",
    r"\bpensi[oó]n completa\b",
    r"\bpension completa\b",
    r"\bfull board\b",
    r"\bfullboard\b",
    r"\bsolo alojamiento\b",
    r"\broom only\b",
    r"\bsin desayuno\b",
    r"\bno desayuno\b",
    r"\bbed\b.*\bbreakfast\b",
    r"\bb&b\b",
    r"\bbed and breakfast\b",
    r"\btodo incluido\b",
    r"\ball\s*[- ]?inclusive\b",
    r"\bno reembolsable\b",
    r"\bnon[- ]?refundable\b",
    r"\breembolsable\b",
    r"\brefundable\b",
    r"\bflexible\b",
    r"\btarifa\b",
    r"\br[eé]gimen\b",
)
_PLAN_RE = re.compile("|".join(_PLAN_PATTERNS), re.I)


def genuine_plan_name(text: Optional[str]) -> Optional[str]:
    """Return a cleaned rate/meal/booking-plan label, or None.

    Guards against room amenities leaking into the plan field (e.g. the
    substring 'plan' inside the Spanish 'pantalla plana')."""
    t = normalize_ws(text)
    if not t or len(t) > 80:
        return None
    if "?" in t or "\u00bf" in t:
        return None
    if not _PLAN_RE.search(t):
        return None
    return t.rstrip(".,;:| ").strip() or None


def clean_amenity_text(text: Optional[str]) -> Optional[str]:
    """Normalize a single amenity string: collapse whitespace and strip
    trailing review scores, bullets and punctuation."""
    t = normalize_ws(text)
    if not t:
        return None
    t = re.sub(r"\s+[\d.,]+\s*$", "", t).strip()
    t = re.sub(r"^[\s\u2022\u00b7\u25aa\u203a\-]+\s*", "", t).strip()
    t = t.rstrip(".,:;|").strip()
    return t or None


_EXACT_AMENITY_JUNK = {
    "inicio", "hoteles", "vuelos", "coches", "alquiler de coches",
    "atracciones", "taxis aeropuerto", "países", "regiones", "ciudades",
    "zonas", "aeropuertos", "español", "ingles", "english", "doble",
    "triple", "cuádruple", "limpieza", "confort", "ubicación", "total",
    "medellín", "medellin", "rionegro", "santa elena", "girardota",
    "bello", "colombia", "antioquia", "la candelaria", "alojamiento",
    "precio", "reservas", "ofrece", "servicios", "ver más", "mostrar",
    "bogotá", "bogota", "cota", "choachí", "choachi", "cundinamarca",
}

_SUBSTR_AMENITY_JUNK = (
    "vuelos", "alquiler de coches", "taxis aeropuerto",
    "lugares de interés", "casas y chalets", "apartamentos", "resorts",
    "villas", "albergues", "bed and breakfasts", "hostales y pensiones",
    "alojamientos únicos", "estancias mensuales", "gestiona tus viajes",
    "atención al cliente", "recursos sobre seguridad", "fidelización",
    "genius", "ofertas de temporada", "ofertas en", "artículos de viajes",
    "business", "traveller review awards", "buscador de vuelos",
    "reservas en restaurantes", "agencias de viajes",
    "política de privacidad", "condiciones del servicio",
    "declaración de accesibilidad", "litigios", "extranet", "afiliado",
    "colaboradores", "sobre booking.com", "cómo trabajamos",
    "sostenibilidad", "centro de prensa", "trabaja con nosotros",
    "contacto corporativo", "inversores", "pautas sobre el contenido",
    "vista general", "info y precios", "normas de la casa",
    "información legal", "comentarios de clientes",
    "instalaciones y servicios", "calidad-precio", "los más populares",
    "cancelación gratis", "sin pago por adelantado",
    "no se necesita tarjeta", "se te cobrará", "pagarás en el alojamiento",
    "descuento", "adelantado", "se sirven desayunos", "se sirve desayuno",
    "puedo aparcar", "horario de check-in", "hay restaurante",
    "hay piscina", "hay spa", "hay habitaciones", "qué restaurantes",
    "servicio de enlace", "todo empieza con", "luego, un viaje",
    "solo se pueden dejar comentarios",
    "agentes de viajes", "registra tu alojamiento",
    "declaración sobre la esclavitud moderna", "declaración sobre derechos humanos",
    "embajada", "consulado",
    "parque el poblado", "plaza botero", "estadio atanasio",
    "aeropuerto internacional", "aeropuerto olaya", "parque barrio",
    "zona colonial", "casco histórico", "centro histórico",
    "nos quedan", "por cancelar", "parque de la",
)

_ROOM_ONLY_PATTERNS = (
    r"baño privado", r"banio privado", r"bañera", r"\bducha\b",
    r"\baseo\b", r"toallas", r"ropa de cama", r"sábanas",
    r"papel higiénico", r"vistas", r"ventilador", r"balcón", r"balcon",
    r"pantalla plana", r"\btv\b", r"caja de seguridad",
    r"canales por cable", r"perchero",
    r"artículos de aseo", r"articulos de aseo", r"cafetera",
    r"microondas", r"nevera", r"frigorífico", r"secador", r"hervidor",
    r"minibar", r"sofá", r"\bcama\b", r"\bcamas\b", r"cuna",
    r"toda la unidad es accesible",
)

_DISTANCE_RE = re.compile(r"\d+\s*(?:m|km)\b", re.I)

_FACILITY_HEADWORDS = {
    "gratis", "gratuito", "privado", "público", "publico", "infantil",
    "acuático", "acuatico", "deportivo", "familiar", "cubierto",
    "accesible", "incluido", "de", "pago", "horas", "disponible",
    "aparcamiento", "estacionamiento", "parking", "garaje", "baño",
    "banio", "ducha", "restaurante", "bar", "cafetería", "cafeteria",
    "gimnasio", "spa", "piscina", "terraza", "jardín", "jardin",
    "lavandería", "recepción", "ascensor", "elevador", "consigna",
    "traslado", "shuttle", "internet", "wifi", "business", "eventos",
}


def _looks_like_proper_name(t: str) -> bool:
    """Reject place/zone/landmark-style nav links such as 'Parque Lleras',
    'Plaza Botero' while keeping real facilities like 'Parque Infantil'."""
    words = [w for w in t.split()]
    if len(words) < 2:
        return False
    if not all(w[:1].isupper() for w in words if w):
        return False
    return not any(w.lower().rstrip(".,:;") in _FACILITY_HEADWORDS for w in words)


def is_property_amenity(text: Optional[str]) -> bool:
    """True only for plausible property-level facility items."""
    t = clean_amenity_text(text)
    if not t or len(t) < 2 or len(t) > 90:
        return False
    if "?" in t or "\u00bf" in t:
        return False
    if "(" in t or ")" in t:
        return False
    if _looks_like_proper_name(t):
        return False
    low = t.lower()
    if low in _EXACT_AMENITY_JUNK:
        return False
    if any(k in low for k in _SUBSTR_AMENITY_JUNK):
        return False
    if _DISTANCE_RE.search(t):
        return False
    if any(re.search(p, low) for p in _ROOM_ONLY_PATTERNS):
        return False
    return True


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
