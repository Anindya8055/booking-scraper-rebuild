"""
Orchestrator for the Booking Scraper Rebuild.
"""

import asyncio
from typing import List, Optional
from .client import BookingScraperClient
from .models import HotelSearchParams
from pathlib import Path
from .storage import Storage


class ScrapeOrchestrator:
    def __init__(self, headless: bool = True, output_dir: str = "data"):
        self.headless = headless
        self.storage = Storage(output_dir=Path(output_dir))

    async def search_and_scrape(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int] = None,
    ) -> List:
        """
        Search for hotels and scrape their details.
        Returns a list of ScrapedHotel.
        """
        async with BookingScraperClient(headless=self.headless) as client:
            # Step 1: Search for hotel URLs
            hotel_urls = await client.search_hotel_urls(params, max_hotels=max_hotels)
            print(f"Found {len(hotel_urls)} hotel URLs")

            # Step 2: Fetch details for each hotel
            hotels: List = []
            for url in hotel_urls:
                print(f"Fetching detail for {url}")
                hotel = await client.get_hotel_detail(url, params)
                if hotel:
                    hotels.append(hotel)
                else:
                    print(f"Failed to fetch detail for {url}")

            return hotels

    def save_results(self, hotels: List, prefix: str = "booking_results") -> str:
        """
        Save the scraped hotels to storage.
        Returns the path to the saved file.
        """
        if not hotels:
            print("No hotels to save")
            return ""
        path = self.storage.save(hotels, prefix=prefix)
        print(f"Saved results to {path}")
        return str(path)

    async def run(
        self,
        params: HotelSearchParams,
        max_hotels: Optional[int] = None,
        prefix: str = "booking_results",
    ) -> str:
        """
        Run the full scrape and save results.
        """
        hotels = await self.search_and_scrape(params, max_hotels=max_hotels)
        return self.save_results(hotels, prefix=prefix)
