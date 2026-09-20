"""
Orchestrator for the Booking Scraper Rebuild.
"""

from typing import List, Optional
from pathlib import Path

from .client import BookingScraperClient
from .models import HotelSearchParams, ScrapedHotel
from .storage import Storage


class ScrapeOrchestrator:
    def __init__(self, headless: bool = True, output_dir: str = "data"):
        self.headless = headless
        self.storage = Storage(output_dir=Path(output_dir))

    def _print_header(self, params: HotelSearchParams, max_hotels: Optional[int]):
        rooms = params.rooms
        print("Searching Booking.com...")
        print(f"Destination: {params.destination}")
        print(f"Dates: {params.checkin} -> {params.checkout}")
        guests = f"{params.adults} adult{'s' if params.adults != 1 else ''}"
        if params.children:
            guests += f", {params.children} child{'ren' if params.children != 1 else ''}"
        guests += f", {rooms} room{'s' if rooms != 1 else ''}"
        print(f"Guests: {guests}")
        print(f"Currency: {params.currency}")
        print(f"Target hotels: {max_hotels if max_hotels else 'no limit'}")

    async def run(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int] = None,
        prefix: str = "booking_results",
    ) -> str:
        """Run the full scrape (search -> details -> storage) and return the path."""
        self._print_header(params, max_hotels)

        hotels: List[ScrapedHotel] = []
        async with BookingScraperClient(headless=self.headless) as client:
            hotel_urls = await client.search_hotel_urls(params, max_hotels=max_hotels)
            if client.last_resolved:
                r = client.last_resolved
                country = f", {r['country_code']}" if r.get("country_code") else ""
                print(
                    f"Destination resolved: {r['label']}{country} "
                    f"(id {r['dest_id']}, type {r['dest_type']}, {r['source']})"
                )
            print(f"Search phase: {len(hotel_urls)} unique hotels "
                  f"{'(max_hotels=%s reached)' % max_hotels if max_hotels and len(hotel_urls) >= max_hotels else ''}".rstrip())
            print()

            for i, url in enumerate(hotel_urls, start=1):
                detail = await client.get_hotel_detail(url, params)
                if detail:
                    hotels.append(detail)
                    print(f"Found hotel {i}/{len(hotel_urls)}: {detail.hotel.hotel_name}")
                else:
                    print(f"Failed to extract detail for hotel {i}/{len(hotel_urls)} (skipped)")

        path = self.save_results(hotels, prefix=prefix)

        unit_count = sum(len(h.units) for h in hotels)
        print()
        print("Completed.")
        print(f"Hotels: {len(hotels)}")
        print(f"Rooms/units: {unit_count}")
        print(f"Output: {path}")
        return path

    def save_results(self, hotels: List, prefix: str = "booking_results") -> str:
        """
        Save the scraped hotels to storage.
        Returns the path to the saved file.
        """
        if not hotels:
            print("No hotels were saved.")
            return ""
        path = self.storage.save(hotels, prefix=prefix)
        return str(path)
