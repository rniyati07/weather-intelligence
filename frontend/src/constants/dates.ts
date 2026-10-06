/**
 * Date limits — API Spec §11 "Range length".
 *
 * These mirror the backend's validation exactly. The picker enforces them
 * *before* any request is issued, so a user never sees a `400 VALIDATION_ERROR`
 * from normal interaction — if they do, the client-side guard has a bug
 * (FDS §5.2). Dates beyond the horizon are disabled, not merely warned about.
 */

/** Maximum span of one forecast request (the manual planner), inclusive. */
export const MAX_TRIP_LENGTH_DAYS = 16

/**
 * The forecast window, in days *including today* — mirrors the backend's
 * `MAX_FORECAST_HORIZON_DAYS`. Open-Meteo serves today through today + 15
 * and rejects anything later, so the last pickable day is today + 15.
 *
 * This bounds the manual planner, which only ever shows forecast data. It is
 * not a limit on how long a trip may be: a chat trip can run past the window,
 * and only its forecastable days get weather.
 */
export const MAX_FORECAST_HORIZON_DAYS = 16

/** v1 serves the forecast window only; historical-only ranges are rejected. */
export const MIN_TRIP_LENGTH_DAYS = 1

/** Preset ranges offered above the calendar (FDS §6.2). */
export const DATE_RANGE_PRESETS = [
  { id: 'weekend', label: 'This weekend' },
  { id: 'next-3-days', label: 'Next 3 days' },
  { id: 'next-week', label: 'Next week' },
  { id: 'custom', label: 'Custom' },
] as const

export type DateRangePresetId = (typeof DATE_RANGE_PRESETS)[number]['id']

/**
 * Locale used for all user-facing date formatting.
 *
 * `en-US` for its month-first order, which is the form the design specifies
 * throughout — "Aug 1 – 3, 2026" (FDS §8.2). `en-GB` would render "1 Aug" and
 * abbreviate September as "Sept", neither of which matches the mockups.
 */
export const DATE_LOCALE = 'en-US'

/** Named `Intl.DateTimeFormat` options, so date rendering stays consistent. */
export const DATE_FORMATS = {
  /** "1 Aug 2026" */
  medium: { day: 'numeric', month: 'short', year: 'numeric' },
  /** "Aug 1" — within a range, where the year is stated once */
  compact: { day: 'numeric', month: 'short' },
  /** "Sat 1" — timeline card headers */
  weekdayShort: { weekday: 'short', day: 'numeric' },
  /** "Saturday" */
  weekdayLong: { weekday: 'long' },
  /** "Sat" — the forecast ribbon's column heads */
  weekdayAbbr: { weekday: 'short' },
  /** "1 August 2026 at 10:00" — the absolute value behind a relative timestamp */
  absolute: { dateStyle: 'long', timeStyle: 'short' },
} as const satisfies Record<string, Intl.DateTimeFormatOptions>
