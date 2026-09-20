"""
Command-line interface for the Booking Scraper Rebuild.

Run a real Booking.com hotel search from the command line. The destination is
given as a plain city or area string; Booking's own live autocomplete is used
to resolve it to the destination Booking needs internally.
"""

import argparse
import asyncio
import signal
import sys
from datetime import date, datetime, timedelta

import booking_scraper.logger as scraper_log
from .orchestrator import (
    ScrapeOrchestrator,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PARTIAL,
)
from .models import HotelSearchParams


def parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date format: {date_str}. Expected YYYY-MM-DD"
        )


def default_checkin() -> date:
    return date.today() + timedelta(days=1)


def default_checkout() -> date:
    return default_checkin() + timedelta(days=1)


def _positive_int(minimum: int = 1):
    def _check(value: str) -> int:
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Expected an integer, got '{value}'")
        if n < minimum:
            raise argparse.ArgumentTypeError(
                f"Must be at least {minimum}, got {n}"
            )
        return n

    return _check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Scrape real Booking.com hotel search results and save normalized JSON.",
        epilog=(
            "Example:\n"
            "  python -m src.cli \"Bogota\" --checkin 2026-09-20 --checkout 2026-09-21 \\\n"
            "      --adults 2 --max-hotels 3 --currency COP\n\n"
            "The destination string is resolved against Booking.com's live "
            "destination lookup; no manual city database is required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "destination",
        type=str,
        help="Destination city or area to search, e.g. 'Bogota', 'Medellin, Colombia'",
    )
    parser.add_argument(
        "--checkin",
        type=parse_date,
        default=None,
        help=f"Check-in date (YYYY-MM-DD, default: {default_checkin()})",
    )
    parser.add_argument(
        "--checkout",
        type=parse_date,
        default=None,
        help=f"Check-out date (YYYY-MM-DD, default: {default_checkout()})",
    )
    parser.add_argument(
        "--adults",
        type=_positive_int(1),
        default=2,
        help="Number of adults (default: 2)",
    )
    parser.add_argument(
        "--children",
        type=_positive_int(0),
        default=0,
        help="Number of children (default: 0)",
    )
    parser.add_argument(
        "--rooms",
        type=_positive_int(1),
        default=1,
        help="Number of rooms (default: 1)",
    )
    parser.add_argument(
        "--currency",
        type=str,
        default="USD",
        help="Currency for prices, ISO 4217 code (default: USD)",
    )
    parser.add_argument(
        "--max-hotels",
        type=_positive_int(1),
        default=5,
        help="Stop after this many hotels (default: 5)",
    )
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Run the browser in headless mode (default: True)",
    )
    headless_group.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run the browser with a visible window",
    )
    parser.set_defaults(headless=True)
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data",
        help="Output directory for results (default: data)",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="booking_results",
        help="Prefix for the output filename (default: booking_results)",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    checkin = args.checkin or default_checkin()
    checkout = args.checkout or default_checkout()

    if checkin >= checkout:
        parser.error("Check-in date must be before check-out date")
    if checkin < date.today():
        parser.error(
            f"Check-in date {checkin} is in the past. Booking.com requires a "
            f"valid future stay."
        )

    if not args.destination.strip():
        parser.error("Destination cannot be empty")

    params = HotelSearchParams(
        destination=args.destination.strip(),
        checkin=checkin,
        checkout=checkout,
        adults=args.adults,
        children=args.children,
        rooms=args.rooms,
        currency=args.currency.upper(),
    )

    orchestrator = ScrapeOrchestrator(
        headless=args.headless,
        output_dir=args.output_dir,
    )

    async def run():
        # Keep the noisy scraper detail on stdout out of the demo run; it
        # still goes to the debug log file.
        scraper_log.set_print_enabled(False)
        cancel_event = asyncio.Event()
        installed = False
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, cancel_event.set)
            installed = True
        except (NotImplementedError, RuntimeError):
            pass  # signal registration unavailable; cancellation stays optional
        try:
            result_path = await orchestrator.run(
                params,
                max_hotels=args.max_hotels,
                prefix=args.prefix,
                cancel_event=cancel_event,
            )
        finally:
            if installed:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
            scraper_log.set_print_enabled(True)
        return result_path

    try:
        result_path = asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    meta = orchestrator.last_run_meta or {}
    status = meta.get("status")
    if status == STATUS_CANCELLED:
        print("Search cancelled by the user.")
        if result_path:
            print(f"Partial results kept: {result_path}")
            print(f"Hotels: {meta.get('successful_hotels')}, " f"Rooms/units: {meta.get('units')}")
        else:
            print("No results were available before cancellation.")
        sys.exit(130)
    if status == STATUS_FAILED:
        print(f"Search failed: {meta.get('failure_reason') or 'no results'}")
        print("Technical details are in data/logs/booking_debug.log.")
        sys.exit(1)

    if not result_path:
        print("No results were saved.")
        sys.exit(1)
    if status == STATUS_PARTIAL:
        print(
            f"Partial results: {meta.get('successful_hotels')}/{meta.get('requested_hotels')} "
            f"hotels completed; {meta.get('failed_hotels')} failed. "
            "Details in metadata file."
        )


if __name__ == "__main__":
    main()
