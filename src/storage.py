"""
Storage for scraped data.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from .models import ScrapedHotel


class Storage:
    def __init__(self, output_dir: Path = Path("data")):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(self, hotels: List[ScrapedHotel], prefix: str = "booking_results") -> Path:
        """Save scraped hotels to a JSON file (proven top-level-list schema)."""
        data = [self._hotel_to_dict(h) for h in hotels]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{timestamp}.json"
        out_path = self.output_dir / filename
        out_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8"
        )
        return out_path

    def save_metadata(self, meta: dict, prefix: str = "booking_results") -> Path:
        """Write the run metadata (status, counts, failures, timing) to a
        companion file next to the data file, using the same timestamp
        granularity the data file uses."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{timestamp}.meta.json"
        out_path = self.output_dir / filename
        payload = dict(meta)
        payload.setdefault("collected_at", datetime.now(timezone.utc).isoformat())
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8"
        )
        return out_path

    def _hotel_to_dict(self, hotel: ScrapedHotel) -> dict:
        """Convert a ScrapedHotel to a dictionary for JSON serialization."""
        return {
            "hotel": {
                "hotel_id": hotel.hotel.hotel_id,
                "hotel_name": hotel.hotel.hotel_name,
                "booking_url": hotel.hotel.booking_url,
                "address": hotel.hotel.address,
                "city": hotel.hotel.city,
                "country": hotel.hotel.country,
                "latitude": hotel.hotel.latitude,
                "longitude": hotel.hotel.longitude,
                "property_type": hotel.hotel.property_type,
                "listing_status": hotel.hotel.listing_status,
                "description": hotel.hotel.description,
                "amenities": hotel.hotel.amenities,
                "amenity_flags": hotel.hotel.amenity_flags,
                "images": hotel.hotel.images,
            },
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "unit_name": unit.unit_name,
                    "price_amount": unit.price_amount,
                    "price_currency": unit.price_currency,
                    "price_text": unit.price_text,
                    "plan_name": unit.plan_name,
                    "cancellation": unit.cancellation,
                    "beds_text": unit.beds_text,
                    "size_m2": unit.size_m2,
                    "amenities": unit.amenities,
                    "images": unit.images,
                    "price_row_text": unit.price_row_text,
                    "header_chips": unit.header_chips,
                    "sections": unit.sections,
                    "long_desc": unit.long_desc,
                    "comfort_score": unit.comfort_score,
                    "unique_url": unit.unique_url,
                }
                for unit in hotel.units
            ],
            "source_url": hotel.source_url,
            "collected_at": hotel.collected_at,
            "raw_data": hotel.raw_data,
        }
