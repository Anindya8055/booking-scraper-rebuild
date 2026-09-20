"""
Data models for the Booking Scraper Rebuild.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from booking_scraper.config import DEFAULT_LANG


@dataclass
class HotelSearchParams:
    """Parameters for a hotel search."""
    destination: str
    checkin: date
    checkout: date
    adults: int = 2
    children: int = 0
    rooms: int = 1
    currency: str = "USD"
    lang: str = DEFAULT_LANG


@dataclass
class Hotel:
    """Normalized hotel information."""
    hotel_id: str
    hotel_name: str
    booking_url: str
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    property_type: Optional[str] = None
    listing_status: Optional[str] = None
    description: Optional[str] = None
    amenities: List[str] = field(default_factory=list)
    amenity_flags: dict = field(default_factory=dict)
    images: List[str] = field(default_factory=list)
    # Guest review score (0-10) read from the search-results card's own
    # accessible aria-label when Booking exposes one; None when absent.
    # See booking_scraper.search.extract_visible_ratings for provenance.
    rating_score: Optional[float] = None
    rating_label: Optional[str] = None
    review_count: Optional[int] = None
    # Booking's own scarcity/urgency text (e.g. "We have 2 left"), only set
    # when genuinely present on the page. Never a fabricated "Available".
    availability: Optional[str] = None


@dataclass
class HotelUnit:
    """Normalized unit/room information."""
    unit_id: str
    unit_name: Optional[str] = None
    price_amount: Optional[float] = None
    price_currency: Optional[str] = None
    price_text: Optional[str] = None
    plan_name: Optional[str] = None
    cancellation: Optional[str] = None
    beds_text: Optional[str] = None
    size_m2: Optional[float] = None
    amenities: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    price_row_text: Optional[str] = None
    header_chips: Optional[List[str]] = None
    sections: Optional[dict] = None
    long_desc: Optional[str] = None
    comfort_score: Optional[float] = None
    unique_url: Optional[str] = None


@dataclass
class ScrapedHotel:
    """A hotel with its units and metadata."""
    hotel: Hotel
    units: List[HotelUnit] = field(default_factory=list)
    # Source/provenance
    source_url: str = ""
    collected_at: str = ""  # ISO timestamp
    # Optional: raw data for debugging
    raw_data: Optional[dict] = None
