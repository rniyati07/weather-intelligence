"""`domain.rules.date_range` — the single source of truth for date-range
business rules, shared by the HTTP layer and `ChatOrchestrator`."""

from datetime import date, timedelta

import pytest

from app.domain.rules.date_range import (
    InvalidDateRangeError,
    forecast_coverage,
    forecast_window_end,
    validate_date_range,
    validate_trip_dates,
)

TODAY = date(2026, 8, 27)


class TestValidRanges:
    def test_single_day_within_horizon(self) -> None:
        result = validate_date_range(TODAY, TODAY, max_horizon_days=16, today=TODAY)
        assert result.start == TODAY
        assert result.end == TODAY

    def test_full_horizon_span_is_accepted(self) -> None:
        end = date(2026, 9, 11)  # 16 days ahead
        result = validate_date_range(TODAY, end, max_horizon_days=16, today=TODAY)
        assert result.end == end

    def test_range_starting_in_past_reaching_today_is_accepted(self) -> None:
        """Mixed range — not historical-only — the spec doesn't exclude it."""
        result = validate_date_range(date(2026, 8, 20), TODAY, max_horizon_days=16, today=TODAY)
        assert result.start == date(2026, 8, 20)


class TestEndBeforeStart:
    def test_reversed_range_is_rejected(self) -> None:
        with pytest.raises(InvalidDateRangeError) as exc_info:
            validate_date_range(
                date(2026, 9, 1), date(2026, 8, 30), max_horizon_days=16, today=TODAY
            )
        assert exc_info.value.reason == "end_before_start"


class TestHistoricalRange:
    def test_range_entirely_in_the_past_is_rejected(self) -> None:
        with pytest.raises(InvalidDateRangeError) as exc_info:
            validate_date_range(
                date(2026, 8, 1), date(2026, 8, 5), max_horizon_days=16, today=TODAY
            )
        assert exc_info.value.reason == "historical_range"

    def test_historical_checked_before_span_when_both_apply(self) -> None:
        """A range that is both historical AND too long — historical wins,
        since "we don't serve history" is the reason the caller can act on."""
        with pytest.raises(InvalidDateRangeError) as exc_info:
            validate_date_range(
                date(2019, 1, 1), date(2019, 12, 31), max_horizon_days=16, today=TODAY
            )
        assert exc_info.value.reason == "historical_range"


class TestSpanExceedsHorizon:
    def test_span_longer_than_horizon_is_rejected(self) -> None:
        with pytest.raises(InvalidDateRangeError) as exc_info:
            validate_date_range(TODAY, date(2026, 9, 20), max_horizon_days=16, today=TODAY)
        assert exc_info.value.reason in ("span_exceeds_horizon", "beyond_horizon")


class TestBeyondHorizon:
    def test_end_date_beyond_horizon_is_rejected(self) -> None:
        with pytest.raises(InvalidDateRangeError) as exc_info:
            validate_date_range(
                date(2026, 9, 10), date(2026, 9, 15), max_horizon_days=16, today=TODAY
            )
        assert exc_info.value.reason == "beyond_horizon"
        assert exc_info.value.days_ahead == (date(2026, 9, 15) - TODAY).days


class TestTripDatesHaveNoLengthLimit:
    """Trip duration and forecast availability are separate questions."""

    def test_a_trip_longer_than_the_forecast_window_is_valid(self) -> None:
        result = validate_trip_dates(TODAY, date(2026, 10, 30), today=TODAY)
        assert result.end == date(2026, 10, 30)

    def test_a_trip_entirely_beyond_the_window_is_valid(self) -> None:
        validate_trip_dates(date(2026, 12, 20), date(2026, 12, 25), today=TODAY)

    def test_reversed_and_historical_trips_are_still_invalid(self) -> None:
        with pytest.raises(InvalidDateRangeError):
            validate_trip_dates(date(2026, 9, 2), date(2026, 9, 1), today=TODAY)
        with pytest.raises(InvalidDateRangeError):
            validate_trip_dates(date(2026, 8, 1), date(2026, 8, 5), today=TODAY)


class TestForecastWindowBoundary:
    """Open-Meteo, verified live: 16 days including today — today + 15 is
    served, today + 16 is rejected with HTTP 400."""

    def test_window_end_is_today_plus_window_minus_one(self) -> None:
        assert forecast_window_end(today=TODAY, window_days=16) == TODAY + timedelta(days=15)

    def test_rest_accepts_the_last_forecast_day(self) -> None:
        validate_date_range(TODAY, TODAY + timedelta(days=15), max_horizon_days=16, today=TODAY)

    def test_rest_rejects_the_day_after_the_window(self) -> None:
        with pytest.raises(InvalidDateRangeError) as exc_info:
            validate_date_range(TODAY, TODAY + timedelta(days=16), max_horizon_days=16, today=TODAY)
        assert exc_info.value.reason in ("span_exceeds_horizon", "beyond_horizon")


class TestForecastCoverage:
    WINDOW_END = TODAY + timedelta(days=15)

    def test_trip_inside_the_window_is_fully_covered(self) -> None:
        cov = forecast_coverage(
            TODAY + timedelta(days=2), TODAY + timedelta(days=6), today=TODAY, window_days=16
        )
        assert cov.is_complete and cov.first_uncovered_day is None

    def test_trip_ending_on_the_last_forecast_day_is_fully_covered(self) -> None:
        cov = forecast_coverage(TODAY, self.WINDOW_END, today=TODAY, window_days=16)
        assert cov.is_complete
        assert cov.covered_end == self.WINDOW_END

    def test_a_20_day_trip_is_split_at_the_window(self) -> None:
        start = TODAY + timedelta(days=5)
        end = start + timedelta(days=19)
        cov = forecast_coverage(start, end, today=TODAY, window_days=16)
        assert (cov.covered_start, cov.covered_end) == (start, self.WINDOW_END)
        assert cov.first_uncovered_day == self.WINDOW_END + timedelta(days=1)
        assert not cov.is_complete
        # The first uncovered day enters the window 15 days before it.
        assert cov.forecast_available_from(16) == cov.first_uncovered_day - timedelta(days=15)
        # The whole trip is covered once the window reaches its last day.
        assert cov.fully_covered_from(16) == end - timedelta(days=15)

    def test_one_day_past_the_window(self) -> None:
        cov = forecast_coverage(
            TODAY, self.WINDOW_END + timedelta(days=1), today=TODAY, window_days=16
        )
        assert cov.covered_end == self.WINDOW_END
        assert cov.first_uncovered_day == self.WINDOW_END + timedelta(days=1)

    def test_a_trip_entirely_beyond_the_window_has_no_forecast(self) -> None:
        start = self.WINDOW_END + timedelta(days=1)
        cov = forecast_coverage(start, start + timedelta(days=5), today=TODAY, window_days=16)
        assert not cov.has_forecast
        assert cov.first_uncovered_day == start

    def test_a_trip_under_way_is_covered_from_today(self) -> None:
        cov = forecast_coverage(
            TODAY - timedelta(days=3), TODAY + timedelta(days=2), today=TODAY, window_days=16
        )
        assert cov.covered_start == TODAY
        assert cov.is_complete
