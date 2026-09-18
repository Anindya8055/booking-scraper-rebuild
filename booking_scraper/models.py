"""Data classes used throughout the scraper."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class CityCfg:
    name: str
    dest_id: str
    dest_type: str
    checkin: Optional[str] = None
    checkout: Optional[str] = None
    order: str = "price"
    adults: int = 2
    children: int = 0
    rooms: int = 1
    currency: str = "COP"
    lang: str = "es"
    nights: int = 1


@dataclass
class ParentCtx:
    parent_url: str
    parent_title: str
    parent_desc: Optional[str]
    parent_amenities: List[str]
    parent_amenity_flags: Dict[str, Optional[bool]]
    address: Optional[str]
    city_name: Optional[str]
    country_name: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    currency: str
    listing_status: str
    property_type: str
    city: Optional[CityCfg]


@dataclass
class UnitSections:
    bano_privado: List[str]
    vista: List[str]
    equipamiento: List[str]
    politica_humo: Optional[str]


@dataclass
class UnitRow:
    unit_id: str
    unit_name: Optional[str]
    price_amount: Optional[float]
    price_currency: Optional[str]
    price_text: Optional[str]
    plan_name: Optional[str]
    cancellation: Optional[str]
    beds_text: Optional[str]
    size_m2: Optional[float]
    amenities: List[str]
    images: Optional[List[str]] = None
    sections: Optional[Dict[str, Any]] = None
    header_chips: Optional[List[str]] = None
    long_desc: Optional[str] = None
    comfort_score: Optional[float] = None
    price_row_text: Optional[str] = None
