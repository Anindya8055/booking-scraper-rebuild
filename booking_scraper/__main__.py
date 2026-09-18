"""Entry point for running the package with ``python -m booking_scraper``."""

import asyncio

from .runner import main

if __name__ == "__main__":
    asyncio.run(main())
