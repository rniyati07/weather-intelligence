import { CalendarClock, ChevronRight, CloudOff, RefreshCw } from 'lucide-react'
import { Link } from 'react-router'

import { Button } from '@/components/ui/button'
import { SkeletonText } from '@/components/ui/skeleton'
import { buildTripPath } from '@/constants/routes'
import type { IsoDate, WeatherIntelligence } from '@/types'
import { formatDate, formatDateRange } from '@/utils/date'
import type { TripForecastCoverage } from '../active-trip'
import { defaultSelectedDate, toTripDays } from '../trip-intelligence'
import { DayDetailPanel } from './DayDetailPanel'
import { DayStrip } from './DayStrip'
import { TripOutlookPanel } from './TripOutlookPanel'

export interface TripWorkspacePanelProps {
  intelligence: WeatherIntelligence | null
  isPending: boolean
  isError: boolean
  onRetry: () => void
  locationId: string
  /** The trip's own dates. */
  startDate: IsoDate
  endDate: IsoDate
  /** How much of the trip the weather provider can forecast; `intelligence`
   * covers only `coverage.covered`. */
  coverage: TripForecastCoverage | null
  selectedDate: IsoDate | null
  onSelectDate: (date: IsoDate) => void
}

/**
 * The structured half of the workspace: outlook, best/watch-out, the day
 * strip and the selected day's detail.
 *
 * Purely presentational over the deterministic `GET .../intelligence` payload
 * its parent fetched — which is what keeps the sidebar's glance card and this
 * panel speaking about the same day, from one query rather than two.
 */
export function TripWorkspacePanel({
  intelligence,
  isPending,
  isError,
  onRetry,
  locationId,
  startDate,
  endDate,
  coverage,
  selectedDate,
  onSelectDate,
}: TripWorkspacePanelProps) {
  const through = coverage?.availableThrough ?? null

  // The whole trip is still beyond the forecast window: a fact about the
  // provider, not a failure — so a plain statement, not the error/retry box.
  if (coverage && !coverage.covered) {
    return (
      <div className="flex gap-4 rounded-xl bg-surface p-5 shadow-sm md:p-7">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-primary-subtle text-primary">
          <CalendarClock className="size-5" aria-hidden="true" />
        </span>
        <div>
          <h2 className="text-h4 font-semibold text-heading">Too early to forecast</h2>
          <p className="mt-1 max-w-[60ch] text-body text-muted-foreground">
            {through ? `Weather forecasts currently reach ${formatDate(through)}, so ` : 'So far, '}
            {formatDateRange(startDate, endDate)} can’t be forecast yet. Your trip is saved, and the
            outlook appears here once your first day is inside the forecast range.
          </p>
        </div>
      </div>
    )
  }

  const covered = coverage?.covered ?? { startDate, endDate }
  const uncoveredNote =
    coverage?.uncoveredFrom && through ? (
      <p className="flex gap-2.5 rounded-lg bg-primary-subtle px-4 py-3 text-body-sm text-foreground">
        <CalendarClock className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
        <span>
          Weather below covers {formatDateRange(covered.startDate, covered.endDate)}. Forecasts
          currently reach {formatDate(through)}, so{' '}
          {formatDateRange(coverage.uncoveredFrom, endDate)}{' '}
          {coverage.uncoveredFrom === endDate ? 'is' : 'are'} not forecast yet.
        </span>
      </p>
    ) : null

  if (isPending) {
    return (
      // Shaped like what is coming (the outlook instrument, then the ribbon)
      // so the page does not jump when it lands.
      <div className="flex flex-col gap-5" aria-label="Loading trip outlook" role="status">
        <div className="rounded-xl bg-surface p-5 shadow-sm md:p-7">
          <div className="h-3 w-24 animate-pulse rounded-full bg-muted" />
          <div className="mt-4 h-14 w-40 animate-pulse rounded-lg bg-muted" />
          <div className="mt-5 h-2 w-full animate-pulse rounded-full bg-muted" />
          <div className="mt-6">
            <SkeletonText lines={2} />
          </div>
        </div>
        <div className="h-52 animate-pulse rounded-xl bg-surface shadow-sm" />
        <span className="sr-only">Loading the forecast for your trip…</span>
      </div>
    )
  }

  // Scoped and recoverable rather than silent: the trip is still active and
  // the conversation still works, so the workspace states what is missing and
  // offers a retry instead of vanishing and taking the trip's identity with
  // it.
  if (isError || !intelligence) {
    return (
      <div className="flex flex-wrap items-center gap-4 rounded-xl bg-surface p-5 shadow-sm md:p-6">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <CloudOff className="size-5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-body font-semibold text-heading">The trip outlook didn’t load</p>
          <p className="text-body-sm text-muted-foreground">
            The conversation still works. Try loading the forecast again.
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={onRetry}>
          <RefreshCw aria-hidden="true" />
          Try again
        </Button>
      </div>
    )
  }

  const days = toTripDays(intelligence)
  const active = selectedDate ?? defaultSelectedDate(days, intelligence.tripSummary)
  const selected = days.find((entry) => entry.date === active)
  const byDate = new Map(days.map((entry) => [entry.date, entry.day]))

  return (
    <div className="flex min-w-0 flex-col gap-5">
      {uncoveredNote}
      <TripOutlookPanel
        summary={intelligence.tripSummary}
        dayCount={days.length}
        bestDay={byDate.get(intelligence.tripSummary.bestDays[0] ?? '')}
        worstDay={byDate.get(intelligence.tripSummary.worstDays[0] ?? '')}
      />

      {days.length > 1 ? (
        <section aria-labelledby="your-days-heading" className="flex min-w-0 flex-col gap-3">
          <div className="flex items-baseline justify-between gap-4">
            <h2 id="your-days-heading" className="text-h4 font-semibold text-heading">
              Day by day
              <span className="ml-2 hidden text-body-sm font-normal text-muted-foreground md:inline">
                Select a day for details
              </span>
            </h2>
            <Link
              to={buildTripPath({
                locationId,
                startDate: covered.startDate,
                endDate: covered.endDate,
              })}
              className="inline-flex shrink-0 items-center gap-1 rounded-md text-body-sm font-medium text-primary transition-colors hover:text-primary-hover focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              Full breakdown
              <ChevronRight className="size-4" aria-hidden="true" />
            </Link>
          </div>

          <DayStrip days={days} selectedDate={active} onSelect={onSelectDate} />
        </section>
      ) : null}

      {selected ? <DayDetailPanel entry={selected} /> : null}
    </div>
  )
}
