"""
Orchestrator for the Booking Scraper Rebuild.
"""

from typing import Callable, List, Optional
from pathlib import Path

from .client import BookingScraperClient
from .models import HotelSearchParams, ScrapedHotel
from .storage import Storage

ProgressCallback = Optional[Callable[[str], None]]


class ScrapeOrchestrator:
    def __init__(self, headless: bool = True, output_dir: str = "data"):
        self.headless = headless
        self.storage = Storage(output_dir=Path(output_dir))

    @staticmethod
    def _emit(progress: ProgressCallback, msg: str) -> None:
        """Deliver a status line to the progress callback when present,
        otherwise mirror the current CLI behaviour of printing it."""
        if progress is not None:
            progress(msg)
        else:
            print(msg)

    def _print_header(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int],
        progress: ProgressCallback,
    ):
        rooms = params.rooms
        self._emit(progress, "Searching Booking.com...")
        self._emit(progress, f"Destination: {params.destination}")
        self._emit(progress, f"Dates: {params.checkin} -> {params.checkout}")
        guests = f"{params.adults} adult{'s' if params.adults != 1 else ''}"
        if params.children:
            guests += f", {params.children} child{'ren' if params.children != 1 else ''}"
        guests += f", {rooms} room{'s' if rooms != 1 else ''}"
        self._emit(progress, f"Guests: {guests}")
        self._emit(progress, f"Currency: {params.currency}")
        self._emit(progress, f"Target hotels: {max_hotels if max_hotels else 'no limit'}")

    async def run(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int] = None,
        prefix: str = "booking_results",
        progress: ProgressCallback = None,
    ) -> str:
        """Run the full scrape (search -> details -> storage) and return the path.

        `progress` is an optional callback receiving one status line at a time.
        When omitted each line is printed, matching the CLI experience."""
        self._print_header(params, max_hotels, progress)

        hotels: List[ScrapedHotel] = []
        async with BookingScraperClient(headless=self.headless) as client:
            hotel_urls = await client.search_hotel_urls(params, max_hotels=max_hotels)
            if client.last_resolved:
                r = client.last_resolved
                country = f", {r['country_code']}" if r.get("country_code") else ""
                self._emit(
                    progress,
                    f"Destination resolved: {r['label']}{country} "
                    f"(id {r['dest_id']}, type {r['dest_type']}, {r['source']})",
                )
            self._emit(
                progress,
                f"Search phase: {len(hotel_urls)} unique hotels "
                f"{'(max_hotels=%s reached)' % max_hotels if max_hotels and len(hotel_urls) >= max_hotels else ''}".rstrip(),
            )
            self._emit(progress, "")

            for i, url in enumerate(hotel_urls, start=1):
                detail = await client.get_hotel_detail(url, params)
                if detail:
                    hotels.append(detail)
                    self._emit(
                        progress,
                        f"Found hotel {i}/{len(hotel_urls)}: {detail.hotel.hotel_name}",
                    )
                else:
                    self._emit(
                        progress,
                        f"Failed to extract detail for hotel {i}/{len(hotel_urls)} (skipped)",
                    )

        path = self.save_results(hotels, prefix=prefix, progress=progress)

        unit_count = sum(len(h.units) for h in hotels)
        self._emit(progress, "")
        self._emit(progress, "Completed.")
        self._emit(progress, f"Hotels: {len(hotels)}")
        self._emit(progress, f"Rooms/units: {unit_count}")
        self._emit(progress, f"Output: {path}")
        return path

    def save_results(
        self,
        hotels: List,
        prefix: str = "booking_results",
        progress: ProgressCallback = None,
    ) -> str:
        """
        Save the scraped hotels to storage.
        Returns the path to the saved file.
        """
        if not hotels:
            self._emit(progress, "No hotels were saved.")
            return ""
        path = self.storage.save(hotels, prefix=prefix)
        return str(path)
