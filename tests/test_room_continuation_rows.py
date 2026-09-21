"""Regression test for blank "Room" rows with no photo.

Booking's #hprt-table gives the room name/link its own <td rowspan="N">
cell on only the FIRST <tr> of a room that has multiple rate-plan rows
(e.g. a non-refundable vs. a flexible plan for the same physical room).
The following rows share that name visually via the rowspan but have no
name cell (and no room-type link) of their own in the DOM.

extract_units() used to extract those continuation rows with unit_name=None
and no images/amenities, which the frontend then rendered as a bare "Room"
card with a bed icon instead of a photo -- observed live on a real Delhi
search. This test pins the fix: a continuation row (no name of its own)
must inherit the name/photo/amenities of the room's first row rather than
being extracted as an empty, unnamed room.

Uses a static local HTML fixture (set_content, no network), same pattern as
test_dom_extraction.py. Neither row has a room-type *link*
(`a.hprt-roomtype-link`), only a `.hprt-roomtype-name` span, so extract_units
never attempts to open a detail modal -- keeping this test fast and focused
purely on the row-to-row inheritance logic.
"""

import asyncio

import pytest

playwright_async = pytest.importorskip("playwright.async_api")

from booking_scraper.detail import extract_units  # noqa: E402


TWO_RATE_PLANS_HTML = """
<table id="hprt-table">
  <tr data-block-id="RD100" id="RD100">
    <td><span class="hprt-roomtype-name">Deluxe Double Room</span></td>
    <td class="hprt-roomtype-bed">1 queen bed</td>
  </tr>
  <tr data-block-id="RD101" id="RD101">
    <td class="hprt-roomtype-bed">1 queen bed</td>
  </tr>
</table>
"""

THREE_ROOMS_MIXED_HTML = """
<table id="hprt-table">
  <tr data-block-id="RD200" id="RD200">
    <td><span class="hprt-roomtype-name">Single Room</span></td>
  </tr>
  <tr data-block-id="RD201" id="RD201">
    <td><span class="hprt-roomtype-name">Twin Room</span></td>
  </tr>
  <tr data-block-id="RD202" id="RD202">
    <td class="hprt-roomtype-bed">continuation of Twin Room</td>
  </tr>
</table>
"""


async def _extract(html: str):
    async with playwright_async.async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(f"<html><body>{html}</body></html>")
        try:
            return await extract_units(page)
        finally:
            await browser.close()


def test_continuation_row_inherits_name_from_first_row():
    units = asyncio.run(_extract(TWO_RATE_PLANS_HTML))
    assert len(units) == 2
    assert units[0].unit_name == "Deluxe Double Room"
    # Regression: this used to be None (rendered as a bare "Room" card).
    assert units[1].unit_name == "Deluxe Double Room"


def test_continuation_row_inherits_from_its_own_room_not_an_earlier_one():
    units = asyncio.run(_extract(THREE_ROOMS_MIXED_HTML))
    assert len(units) == 3
    assert units[0].unit_name == "Single Room"
    assert units[1].unit_name == "Twin Room"
    # Must inherit from the immediately preceding named row (Twin Room),
    # not the first room in the table (Single Room).
    assert units[2].unit_name == "Twin Room"
