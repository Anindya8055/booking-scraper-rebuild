"""Unit tests for the rating/availability extraction added via GitHub-sourced
open-source technique research (see search.py and utils.py docstrings for
provenance). These are pure parsing tests -- no network, no Booking.com
access -- and pin the exact real aria-label / scarcity-text formats
observed on live Booking.com output during development.
"""

from booking_scraper.search import _parse_rating_aria
from booking_scraper.utils import is_scarcity_message, is_property_amenity


def test_parse_rating_aria_basic():
    aria = "Scored 4.7, Review score, 72 reviews, Opens Dubai hostel information"
    parsed = _parse_rating_aria(aria)
    assert parsed == {"score": 4.7, "label": None, "count": 72}


def test_parse_rating_aria_with_qualitative_label():
    aria = "Scored 8.7, Wonderful, 1,234 reviews, Opens Some Hotel information"
    parsed = _parse_rating_aria(aria)
    assert parsed["score"] == 8.7
    assert parsed["label"] == "Wonderful"
    assert parsed["count"] == 1234


def test_parse_rating_aria_integer_score():
    aria = "Scored 10, Exceptional, 5 reviews, Opens Y information"
    parsed = _parse_rating_aria(aria)
    assert parsed["score"] == 10.0
    assert parsed["count"] == 5


def test_parse_rating_aria_missing_returns_none():
    assert _parse_rating_aria(None) is None
    assert _parse_rating_aria("") is None
    assert _parse_rating_aria("Opens Some Hotel information") is None


def test_scarcity_message_detected_from_real_observed_text():
    # Exact text this project's own live scrape produced (see conversation
    # history / git log for the run this was captured from).
    assert is_scarcity_message("We have 2 left")
    assert is_scarcity_message("Only 1 room left at this price")
    assert is_scarcity_message("Sold out")


def test_scarcity_message_not_falsely_triggered():
    assert not is_scarcity_message("Free Wifi")
    assert not is_scarcity_message("Air conditioning")
    assert not is_scarcity_message(None)


def test_scarcity_text_excluded_from_property_amenities():
    # Regression: this used to leak into the generic amenities sweep.
    assert not is_property_amenity("We have 2 left")
    assert is_property_amenity("Free Wifi")


def test_review_score_categories_excluded_from_property_amenities():
    # Regression: Booking's own review-score breakdown labels (e.g.
    # "Facilities 8.5") sit in the same list-item sweep as real facilities
    # on English-locale pages; once clean_amenity_text() strips the trailing
    # score, the bare category label used to leak in as a fake "facility",
    # observed live as a run of junk items ahead of any real facility.
    for junk in ("Facilities", "Cleanliness", "Comfort", "Value for money",
                 "Location", "Staff"):
        assert not is_property_amenity(junk), junk
    # Real facilities that happen to share a word with a junk category must
    # still pass.
    assert is_property_amenity("Great location")
    assert is_property_amenity("24-hour front desk")
