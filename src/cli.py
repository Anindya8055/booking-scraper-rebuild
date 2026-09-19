"""
Command-line interface for the Booking Scraper Rebuild.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from .orchestrator import ScrapeOrchestrator
from .models import HotelSearchParams


def parse_date(date_str: str) -> datetime.date:
    """Parse a date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: {date_str}. Expected YYYY-MM-DD")


def main():
    parser = argparse.ArgumentParser(description="Scrape Booking.com for hotel data.")
    parser.add_argument(
        "destination",
        type=str,
        help="Destination city or area (e.g., 'Medellin, Colombia')",
    )
    parser.add_argument(
        "--checkin",
        type=parse_date,
        required=True,
        help="Check-in date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--checkout",
        type=parse_date,
        required=True,
        help="Check-out date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--adults",
        type=int,
        default=2,
        help="Number of adults (default: 2)",
    )
    parser.add_argument(
        "--children",
        type=int,
        default=0,
        help="Number of children (default: 0)",
    )
    parser.add_argument(
        "--rooms",
        type=int,
        default=1,
        help="Number of rooms (default: 1)",
    )
    parser.add_argument(
        "--currency",
        type=str,
        default="USD",
        help="Currency for prices (default: USD)",
    )
    parser.add_argument(
        "--max-hotels",
        type=int,
        default=None,
        help="Maximum number of hotels to process (default: no limit)",
    )
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Run browser in headless mode (default: True)",
    )
    headless_group.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run browser with a visible window",
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
        help="Prefix for output filename (default: booking_results)",
    )

    args = parser.parse_args()

    # Validate dates
    if args.checkin >= args.checkout:
        parser.error("Check-in date must be before check-out date")

    params = HotelSearchParams(
        destination=args.destination,
        checkin=args.checkin,
        checkout=args.checkout,
        adults=args.adults,
        children=args.children,
        rooms=args.rooms,
        currency=args.currency,
    )

    orchestrator = ScrapeOrchestrator(
        headless=args.headless,
        output_dir=args.output_dir,
    )

    async def run():
        result_path = await orchestrator.run(
            params,
            max_hotels=args.max_hotels,
            prefix=args.prefix,
        )
        if result_path:
            print(f"Success! Results saved to: {result_path}")
        else:
            print("No results were saved.")
            sys.exit(1)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
