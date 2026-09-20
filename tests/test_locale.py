"""Regression tests for Booking.com locale/language configuration.

Booking.com used to be forced into Spanish (browser launched with
`--lang=es-ES`, destination resolution hitting `index.es.html`, and hotel
detail URLs hardcoded to a literal `.es.html` suffix regardless of the
configured language). These tests pin the fix: every URL/context builder
must use the configured language (English by default) and currency must
stay a fully independent knob.
"""

from dataclasses import replace

from booking_scraper.config import DEFAULT_LANG
from booking_scraper.models import CityCfg
from booking_scraper.utils import build_detail_url_with_params, build_search_url
from src.models import HotelSearchParams
from datetime import date


def _city(**overrides) -> CityCfg:
    base = CityCfg(
        name="Dubai",
        dest_id="-782831",
        dest_type="city",
        checkin="2026-10-01",
        checkout="2026-10-02",
        currency="USD",
    )
    return replace(base, **overrides) if overrides else base


def test_default_language_is_english_not_spanish():
    assert DEFAULT_LANG != "es"
    assert HotelSearchParams(
        destination="Dubai", checkin=date(2026, 10, 1), checkout=date(2026, 10, 2)
    ).lang == DEFAULT_LANG
    assert CityCfg(name="x", dest_id="1", dest_type="city").lang == DEFAULT_LANG


def test_build_search_url_uses_configured_language_not_spanish():
    city = _city(lang="en-us")
    url = build_search_url(city)
    assert "searchresults.en-us.html" in url
    assert "lang=en-us" in url
    assert ".es.html" not in url
    assert "lang=es" not in url


def test_build_detail_url_forces_target_language_even_if_link_was_spanish():
    # Simulates a SERP card href that (for whatever reason) still carries a
    # Spanish suffix: the detail URL must be re-forced to the requested
    # language, not silently kept in Spanish.
    raw = "/hotel/ae/some-hotel.es.html?checkin=2026-10-01"
    city = _city(lang="en-us")
    out = build_detail_url_with_params(raw, city)
    assert out.endswith(".es.html") is False
    assert ".en-us.html" in out
    assert "lang=en-us" in out
    assert "lang=es" not in out


def test_build_detail_url_with_no_existing_suffix():
    raw = "/hotel/ae/some-hotel.html?checkin=2026-10-01"
    city = _city(lang="en-us")
    out = build_detail_url_with_params(raw, city)
    assert ".en-us.html" in out
    assert "lang=en-us" in out


def test_currency_is_independent_of_language():
    for currency in ("USD", "BDT", "AED"):
        city = _city(lang="en-us", currency=currency)
        search_url = build_search_url(city)
        assert "lang=en-us" in search_url
        assert f"selected_currency={currency}" in search_url

        detail_url = build_detail_url_with_params(
            "/hotel/ae/some-hotel.html", city
        )
        assert "lang=en-us" in detail_url
        assert f"selected_currency={currency}" in detail_url
