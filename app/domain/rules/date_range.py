"""The one place `end >= start`, the no-history rule, and the forecast
horizon are decided (API Spec §11). Both `interface/http/dependencies.py`
(HTTP layer, all weather/narrative endpoints) and `ChatOrchestrator`
(application layer, chat) call this — neither re-implements the rules.

Two different questions live here and must not be confused:

- **Is this a valid trip?** (`validate_trip_dates`) — the end is not before
  the start and the trip is not entirely in the past. Nothing about length:
  a 20-day trip is a perfectly good trip.
- **How much of it can be forecast?** (`forecast_coverage`) — the weather
  provider serves a fixed window of days starting today
  (`MAX_FORECAST_HORIZON_DAYS`; Open-Meteo, the primary provider, serves 16
  days including today and answers HTTP 400 for a range ending later —
  verified live). A trip running past that window still gets analysis for
  the days inside it; the rest are reported as not yet forecastable.

`validate_date_range` is the stricter rule the REST forecast endpoints use:
they serve forecast data only, so a range they are asked for must lie inside
the window.

Pure: no clock read, `today` is always supplied by the caller.
"""

from dataclasses import dataclass
from datetime import date, timedelta


class InvalidDateRangeError(Exception):
    """A date range fails a business rule.

    `reason` is a stable code (`end_before_start` | `historical_range` |
    `span_exceeds_horizon` | `beyond_horizon`) a caller can switch on without
    parsing message text; `span_days`/`days_ahead` carry the numbers behind
    the two horizon reasons so each caller can phrase its own user-facing
    copy (an HTTP `ErrorDetailSchema` and a conversational clarification
    sentence are not the same shape of text, even though they enforce the
    same rule).
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        span_days: int | None = None,
        days_ahead: int | None = None,
    ) -> None:
        self.reason = reason
        self.span_days = span_days
        self.days_ahead = days_ahead
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ValidatedDateRange:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class ForecastCoverage:
    """Which part of a trip falls inside the provider's forecast window.

    `covered_start`/`covered_end` are both `None` when the whole trip lies
    beyond the window (nothing can be forecast yet); otherwise they bound the
    days that can be analysed. `window_end` is the last day forecasts exist
    for at all.
    """

    trip_start: date
    trip_end: date
    window_end: date
    covered_start: date | None
    covered_end: date | None

    @property
    def has_forecast(self) -> bool:
        return self.covered_start is not None

    @property
    def is_complete(self) -> bool:
        """Every remaining day of the trip is inside the window."""
        return self.covered_end is not None and self.covered_end == self.trip_end

    @property
    def first_uncovered_day(self) -> date | None:
        """The first trip day past the window, or `None` if there is none."""
        if self.trip_end <= self.window_end:
            return None
        return max(self.trip_start, self.window_end + timedelta(days=1))

    def forecast_available_from(self, window_days: int) -> date:
        """The day the trip's first uncovered day enters the window."""
        first = self.first_uncovered_day or self.trip_start
        return first - timedelta(days=window_days - 1)

    def fully_covered_from(self, window_days: int) -> date:
        """The day the window first reaches the trip's last day — from then
        on, the whole (remaining) trip can be forecast."""
        return self.trip_end - timedelta(days=window_days - 1)


def forecast_window_end(*, today: date, window_days: int) -> date:
    """Last day the provider serves a forecast for: `window_days` days
    *including* today. Open-Meteo with 16: today through today + 15."""
    return today + timedelta(days=window_days - 1)


def forecast_coverage(
    start: date, end: date, *, today: date, window_days: int
) -> ForecastCoverage:
    """Split a (valid) trip into the forecastable part and the rest.

    Days already past are not forecast either: a trip in progress is
    analysed from today on, the same "forecast only" rule the REST
    endpoints apply.
    """
    window_end = forecast_window_end(today=today, window_days=window_days)
    covered_start = max(start, today)
    covered_end = min(end, window_end)
    if covered_start > covered_end:
        return ForecastCoverage(start, end, window_end, None, None)
    return ForecastCoverage(start, end, window_end, covered_start, covered_end)


def validate_trip_dates(start: date, end: date, *, today: date) -> ValidatedDateRange:
    """Is this a plannable trip? Deliberately says nothing about length or
    how far ahead it is — see the module docstring.

    Checked in this order deliberately: for a range like 2019-01-01 to
    2019-12-31 that is also reversed or long, "we don't serve history" is
    the reason the caller can actually act on.
    """
    if end < start:
        raise InvalidDateRangeError(
            "endDate must be on or after startDate.", reason="end_before_start"
        )

    if end < today:
        raise InvalidDateRangeError(
            "Historical date ranges are not served by the forecast endpoints.",
            reason="historical_range",
        )
    return ValidatedDateRange(start=start, end=end)


def validate_date_range(
    start: date, end: date, *, max_horizon_days: int, today: date
) -> ValidatedDateRange:
    """The REST forecast endpoints' rule: a valid trip (above) that also lies
    entirely inside the forecast window, since those endpoints serve forecast
    data and nothing else.

    `max_horizon_days` is the window size *including today*, so the last
    accepted end date is `today + max_horizon_days - 1` — the last day the
    provider actually returns. (An earlier version accepted one day more,
    which the provider then rejected with an upstream 400.)
    """
    validate_trip_dates(start, end, today=today)

    span_days = (end - start).days + 1
    if span_days > max_horizon_days:
        raise InvalidDateRangeError(
            f"The requested range exceeds the maximum of {max_horizon_days} days.",
            reason="span_exceeds_horizon",
            span_days=span_days,
        )

    days_ahead = (end - today).days
    if end > forecast_window_end(today=today, window_days=max_horizon_days):
        raise InvalidDateRangeError(
            f"endDate is beyond the supported {max_horizon_days}-day forecast window.",
            reason="beyond_horizon",
            days_ahead=days_ahead,
        )

    return ValidatedDateRange(start=start, end=end)


__all__ = [
    "ForecastCoverage",
    "InvalidDateRangeError",
    "ValidatedDateRange",
    "forecast_coverage",
    "forecast_window_end",
    "validate_date_range",
    "validate_trip_dates",
]
