"""
Live partial-failure / retry demonstration (real Booking.com data).

Performs a REAL search exactly like the CLI, but wraps the real detail client
with two injected failures:

  * the first detail call raises a transient RetryableError once (recovered by
    the orchestrator's retry mechanism);
  * a later detail call for a different hotel raises a permanent
    ExtractionError (never retried).

Everything else uses the untouched real extraction path, so the recovered and
the healthy hotels hold real current Booking.com data.

Run from the repo root, e.g.:

    .venv/bin/python scripts/partial_failure_demo.py "Dhaka" --max-hotels 3
"""

import argparse
import asyncio
import os
import sys

# Make repo-root modules importable when run by path, e.g.:
#   python scripts/partial_failure_demo.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read tuning knobs BEFORE importing the orchestrator (config values are bound
# at import time), so the demo stays quick and the timeout stays generous.
os.environ.setdefault("DETAIL_MAX_RETRIES", "2")
os.environ.setdefault("DETAIL_RETRY_BACKOFF_S", "1")
os.environ.setdefault("JOB_TIMEOUT_S", "1200")

from datetime import date, timedelta  # noqa: E402

import booking_scraper.logger as scraper_log  # noqa: E402
from src.client import BookingScraperClient  # noqa: E402
from src.errors import ExtractionError, RetryableError  # noqa: E402
from src.models import HotelSearchParams  # noqa: E402
from src.orchestrator import ScrapeOrchestrator  # noqa: E402


def wrap_detail(original):
    """Return a wrapped get_hotel_detail that injects the two demo failures."""
    state = {"first_call": True, "permanent_injected": False}

    async def demo_detail(self, url, params):
        if state["first_call"]:
            state["first_call"] = False
            raise RetryableError(
                "demo: injected transient failure (retried automatically)"
            )
        if not state["permanent_injected"]:
            state["permanent_injected"] = True
            raise ExtractionError("demo: injected permanent failure (not retried)")
        return await original(self, url, params)

    return demo_detail


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python scripts/partial_failure_demo.py",
        description=(
            "Real Booking search that demonstrates retry + partial-failure "
            "recovery by injecting one transient and one permanent failure."
        ),
    )
    parser.add_argument("destination", type=str, help="Destination city or area")
    parser.add_argument("--checkin", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--checkout", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--adults", type=int, default=2)
    parser.add_argument("--rooms", type=int, default=1)
    parser.add_argument("--currency", type=str, default="USD")
    parser.add_argument("--max-hotels", type=int, default=3)
    parser.add_argument("--output-dir", type=str, default="data")
    parser.add_argument("--prefix", type=str, default="demo_results")
    return parser


def main():
    args = build_parser().parse_args()
    checkin = date.fromisoformat(args.checkin) if args.checkin else date.today() + timedelta(days=1)
    checkout = date.fromisoformat(args.checkout) if args.checkout else checkin + timedelta(days=1)
    if checkin >= checkout:
        sys.exit("Check-in must be before check-out")

    params = HotelSearchParams(
        destination=args.destination.strip(),
        checkin=checkin,
        checkout=checkout,
        adults=args.adults,
        children=0,
        rooms=args.rooms,
        currency=args.currency.upper(),
    )

    print("Injecting: one transient failure (recovered by retry) and one")
    print("permanent failure (recorded, hotel skipped). Everything else is real.\n")

    orchestrator = ScrapeOrchestrator(headless=True, output_dir=args.output_dir)

    async def run():
        scraper_log.set_print_enabled(False)
        try:
            path = await orchestrator.run(
                params,
                max_hotels=args.max_hotels,
                prefix=args.prefix,
                progress=print,
            )
        finally:
            scraper_log.set_print_enabled(True)
        return path

    result_path = asyncio.run(run())
    meta = orchestrator.last_run_meta or {}
    print("\n--- LIVE DEMO RESULT ---")
    print(f"status          : {meta.get('status')}")
    print(f"requested       : {meta.get('requested_hotels')}")
    print(f"successful      : {meta.get('successful_hotels')}")
    print(f"failed          : {meta.get('failed_hotels')}")
    print(f"units           : {meta.get('units')}")
    print(f"retries_total   : {meta.get('retries_total')}")
    for f in meta.get("failures", []):
        print(
            f"  - {f.get('hotel_id')}: {f.get('reason')} "
            f"(retries={f.get('retries')}, attempts={f.get('attempts')})"
        )
    if result_path:
        print(f"output          : {result_path}")
    if orchestrator.last_meta_path:
        print(f"metadata        : {orchestrator.last_meta_path}")

    ok = (
        meta.get("successful_hotels", 0) >= 1
        and meta.get("retries_total", 0) >= 1
        and meta.get("failed_hotels", 0) >= 1
    )
    print("\nLIVE DEMO OK" if ok else "\nLIVE DEMO did not hit all checkpoints")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    # Inject the demo failures into the real client class before any run.
    _orig = BookingScraperClient.get_hotel_detail
    BookingScraperClient.get_hotel_detail = wrap_detail(_orig)
    main()