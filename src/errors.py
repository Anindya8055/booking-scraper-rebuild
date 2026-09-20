"""
Typed failures for the application orchestration layer.

These distinguish transient failures (worth a bounded retry) from permanent
failures (the page loaded but could not be parsed, or the error is clearly not
going to disappear with another attempt).
"""


class FetchError(Exception):
    """Base class for a failed Booking.com fetch."""


class RetryableError(FetchError):
    """Transient failure: safe to retry a bounded number of times.

    Navigation timeouts, dropped connections, closed pages, etc."""


class NavigationError(RetryableError):
    """A navigation/`goto` failed or timed out for a given page."""


class SearchPhaseError(RetryableError):
    """The search (SERP) phase failed; retrying the whole search may help."""


class PermanentError(FetchError):
    """Non-retryable failure: retrying would not change the outcome."""


class ExtractionError(PermanentError):
    """The page loaded but did not yield the expected data (e.g. no units)."""