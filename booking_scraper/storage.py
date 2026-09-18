"""Local storage: accumulates scrape results and writes them to JSON."""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CRAWLER_DIR
from .models import ParentCtx, UnitRow
from .logger import log
from .utils import hotel_key


class DB:
    def __init__(self, source: str = "booking"):
        self.source = source
        self._results: List[Dict[str, Any]] = []
        self._run_id: Optional[int] = None

    def run_start(self) -> int:
        self._run_id = int(time.time())
        self._results = []
        log(f"{self.source} run_start run_id={self._run_id}")
        return self._run_id

    def add_rental(self, run_id: int, parent_url: str, unit: UnitRow, parent: ParentCtx) -> int:
        result: Dict[str, Any] = {
            "unit_id": unit.unit_id,
            "unit_name": unit.unit_name,
            "price_amount": unit.price_amount,
            "price_currency": unit.price_currency or parent.currency,
            "price_text": unit.price_text,
            "price_row_text": unit.price_row_text,
            "plan_name": unit.plan_name,
            "cancellation": unit.cancellation,
            "beds_text": unit.beds_text,
            "size_m2": unit.size_m2,
            "amenities_parent": parent.parent_amenities,
            "amenities_unit": list(unit.amenities or []),
            "header_chips": unit.header_chips,
            "sections": unit.sections,
            "long_desc": unit.long_desc,
            "comfort_score": unit.comfort_score,
            "images": unit.images or [],
            "listing_status": parent.listing_status,
            "property_type": parent.property_type,
            "address": parent.address,
            "city": parent.city_name,
            "country": parent.country_name,
            "latitude": parent.lat,
            "longitude": parent.lon,
            "_source": {
                "url": f"{parent_url}#unit-{unit.unit_id}",
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        }

        unique_url = f"{parent_url}#unit-{unit.unit_id}"

        tracking = {
            "price": {
                "amount": unit.price_amount,
                "currency": unit.price_currency or parent.currency,
                "text": unit.price_text,
            },
            "description": unit.long_desc or unit.unit_name or "",
        }

        detail = {
            "run_id": run_id,
            "unique_url": unique_url,
            "latitude": parent.lat,
            "longitude": parent.lon,
            "data": result,
            "data_version": 1,
            "changes_tracking_data": tracking,
            "upload_status": "pending",
        }

        self._results.append(detail)
        row_id = len(self._results)
        log(f"{self.source} detail_saved", unique_url=unique_url, row_id=row_id)
        return row_id

    def run_end(self) -> Optional[Path]:
        if not self._results:
            log(f"{self.source} run_end: no results to save")
            return None
        data_dir = CRAWLER_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_path = data_dir / f"booking_results_{self._run_id}.json"
        out_path.write_text(
            json.dumps(self._results, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log(f"{self.source} run_end: saved {len(self._results)} results to {out_path}")
        return out_path
