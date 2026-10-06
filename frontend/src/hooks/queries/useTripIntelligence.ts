import { tripForecastCoverage } from '@/features/chat/active-trip'
import type { TripContextPayload, WeatherIntelligence } from '@/types'
import { useIntelligence } from './useIntelligence'
import type { QueryResult } from './types'
import type { ApiResult } from '@/types'

/**
 * The deterministic intelligence for a conversation's current trip.
 *
 * A thin binding between `tripContext` and the dashboard's own intelligence
 * query: it builds the `{lat},{lon}` location id at the 4dp the backend's
 * `GeocodedPlace.location_id` uses, and stays disabled until the backend has
 * resolved a destination *and* both dates — so an incomplete trip never fires
 * a request that would `400`. Only the days inside the provider's forecast
 * window are requested: a trip may run past it (or start beyond it, in which
 * case nothing is requested yet), and asking for the whole range would `400`.
 *
 * Deliberately the same query key as the deep-dive dashboard's: opening the
 * full breakdown for a trip already discussed in chat is a cache hit, not a
 * second call.
 */
export function useTripIntelligence(
  trip: TripContextPayload,
): QueryResult<ApiResult<WeatherIntelligence>> {
  const { destination } = trip
  const locationId = destination
    ? `${destination.latitude.toFixed(4)},${destination.longitude.toFixed(4)}`
    : null
  const covered = tripForecastCoverage(trip)?.covered ?? null

  return useIntelligence({
    locationId,
    startDate: covered?.startDate ?? null,
    endDate: covered?.endDate ?? null,
  })
}
