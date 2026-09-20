"""Shared fixtures/helpers for the Phase 3 test suite.

Only application-control logic is unit tested here; the real Booking.com
extraction path is verified separately against live production data.
"""

from datetime import date

from src.models import Hotel, HotelUnit, HotelSearchParams, ScrapedHotel


def make_params(destination: str = "Test") -> HotelSearchParams:
    return HotelSearchParams(
        destination=destination,
        checkin=date(2026, 9, 1),
        checkout=date(2026, 9, 2),
        adults=2,
        children=0,
        rooms=1,
        currency="USD",
    )


def make_hotel(url: str, name: str, n_units: int = 2) -> ScrapedHotel:
    slug = url.rstrip("/").split("/")[-1].split(".")[0]
    hotel = Hotel(
        hotel_id=slug,
        hotel_name=name,
        booking_url=url,
        address="1 Test Street",
        city="Test",
        country="TC",
        latitude=1.0,
        longitude=2.0,
        property_type="Hotel",
        amenities=["WiFi"],
        amenity_flags={"wifi": True},
        images=[],
    )
    units = [
        HotelUnit(
            unit_id=f"{slug}-u{i}",
            unit_name=f"Unit {i}",
            price_amount=float(10 + i),
            price_currency="USD",
            price_text=f"US${10 + i}",
            plan_name=None,
            cancellation=None,
            beds_text="1 double bed",
            size_m2=20.0,
            amenities=["TV"],
            images=[],
            unique_url=f"{url}#unit-{slug}-u{i}",
        )
        for i in range(n_units)
    ]
    return ScrapedHotel(
        hotel=hotel,
        units=units,
        source_url=url,
        collected_at="2026-01-01T00:00:00.000000Z",
    )


def hotel_urls(n: int) -> list:
    return [f"https://www.booking.com/hotel/tc/hotel-{i}.es.html" for i in range(n)]


class FakeScraperClient:
    """Stand-in for BookingScraperClient used by the orchestrator directly.

    Records context open/close so cancellation tests can prove nothing leaked.
    """

    def __init__(self, urls, on_detail, fail_search_once: bool = False):
        self.urls = list(urls)
        self.on_detail = on_detail  # async (url, params) -> ScrapedHotel
        self.fail_search_once = fail_search_once
        self.search_calls = 0
        self.opened = 0
        self.closed = 0
        self.last_resolved = {
            "query": "Test",
            "label": "Test",
            "dest_id": -1,
            "dest_type": "city",
            "country_code": "tc",
            "source": "fake",
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def search_hotel_urls(self, params, max_hotels=None):
        self.search_calls += 1
        if self.fail_search_once and self.search_calls == 1:
            from src.errors import SearchPhaseError

            raise SearchPhaseError("simulated search failure")
        return list(self.urls[: max_hotels] if max_hotels else self.urls)

    async def get_hotel_detail(self, url, params):
        self.opened += 1
        try:
            return await self.on_detail(url, params)
        finally:
            self.closed += 1