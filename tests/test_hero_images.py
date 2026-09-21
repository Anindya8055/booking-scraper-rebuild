"""Regression test for the blurry-main-photo bug.

Root cause: ``Hotel.images`` was never populated during extraction, so the
frontend gallery's "main" image fell back to a room-modal thumbnail (Booking
CDN ``square60`` -- a 60px square meant for a small thumbnail), which reads
as blurry once shown at full gallery width. ``extract_hero_images()`` reads
the property's own real hero photo that Booking already emits on the same
page load (``og:image``, falling back to the JSON-LD ``image`` field) -- a
genuine, higher-resolution Booking CDN URL, with no extra navigation and no
invented URLs.

Uses a static local HTML fixture (set_content, no network), same pattern as
test_dom_extraction.py.
"""

import asyncio

import pytest

playwright_async = pytest.importorskip("playwright.async_api")

from booking_scraper.detail import extract_hero_images  # noqa: E402


OG_IMAGE_HTML = """
<head>
  <meta property="og:image" content="https://cf.bstatic.com/xdata/images/hotel/max1024x768/810347557.jpg?k=abc&o=">
</head>
<body></body>
"""

LDJSON_ONLY_HTML = """
<head>
  <script type="application/ld+json">
  {"@type": "Hotel", "image": "https://cf.bstatic.com/xdata/images/hotel/max1024x768/999.jpg?k=xyz&o="}
  </script>
</head>
<body></body>
"""

LDJSON_LIST_HTML = """
<head>
  <meta property="og:image" content="https://cf.bstatic.com/xdata/images/hotel/max1024x768/1.jpg?k=a&o=">
  <script type="application/ld+json">
  {"@type": "Hotel", "image": ["https://cf.bstatic.com/xdata/images/hotel/max1024x768/1.jpg?k=a&o=",
                                "https://cf.bstatic.com/xdata/images/hotel/max1024x768/2.jpg?k=b&o="]}
  </script>
</head>
<body></body>
"""

NO_IMAGE_HTML = """<head></head><body></body>"""


async def _extract(html: str):
    async with playwright_async.async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(f"<html>{html}</html>")
        try:
            return await extract_hero_images(page)
        finally:
            await browser.close()


def test_og_image_used_as_hero_photo():
    images = asyncio.run(_extract(OG_IMAGE_HTML))
    assert images == [
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/810347557.jpg?k=abc&o="
    ]


def test_falls_back_to_ldjson_image_when_no_og_image():
    images = asyncio.run(_extract(LDJSON_ONLY_HTML))
    assert images == ["https://cf.bstatic.com/xdata/images/hotel/max1024x768/999.jpg?k=xyz&o="]


def test_deduplicates_across_og_image_and_ldjson():
    images = asyncio.run(_extract(LDJSON_LIST_HTML))
    assert images == [
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/1.jpg?k=a&o=",
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/2.jpg?k=b&o=",
    ]


def test_no_image_returns_empty_list_never_fabricated():
    images = asyncio.run(_extract(NO_IMAGE_HTML))
    assert images == []
