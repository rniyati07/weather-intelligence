import { CalendarDays, MapPin } from 'lucide-react'

import type { TripContextPayload } from '@/types'
import { formatDateRange, inclusiveDayCount, parseIsoDate, today } from '@/utils/date'
import { titleCaseSlug } from '@/utils/format'
import { TripContextPopover } from './TripContextPopover'

export interface TripIdentityHeaderProps {
  trip: TripContextPayload
}

/** "Starts in 4 days" / "Starts tomorrow" / "Under way" / "Finished" — plain
 * date arithmetic on the trip's own dates, nothing forecast-related. */
function timingLabel(startDate: string, endDate: string): string | null {
  const start = parseIsoDate(startDate)
  const end = parseIsoDate(endDate)
  if (!start || !end) return null
  const now = today()
  const msPerDay = 86_400_000
  const untilStart = Math.round((start.getTime() - now.getTime()) / msPerDay)
  const untilEnd = Math.round((end.getTime() - now.getTime()) / msPerDay)
  if (untilEnd < 0) return 'Finished'
  if (untilStart > 1) return `Starts in ${String(untilStart)} days`
  if (untilStart === 1) return 'Starts tomorrow'
  if (untilStart === 0) return 'Starts today'
  return 'Under way'
}

/**
 * The workspace's identity band: whose trip this is, at a glance.
 *
 * Every value is read from the conversation's own `tripContext` — the
 * destination name the backend geocoded, the dates it validated, the
 * interests it extracted. Nothing is derived from the assistant's prose, and
 * a field the backend hasn't established simply doesn't render.
 */
export function TripIdentityHeader({ trip }: TripIdentityHeaderProps) {
  const destination = trip.destination
  if (!destination) return null

  const interests = trip.interests ?? []
  const { startDate, endDate } = trip
  const start = startDate ? parseIsoDate(startDate) : null
  const end = endDate ? parseIsoDate(endDate) : null
  const days = start && end ? inclusiveDayCount(start, end) : null
  const timing = startDate && endDate ? timingLabel(startDate, endDate) : null
  // "Goa, India" → "Goa" set large, with the country beneath it.
  const [place, ...rest] = destination.displayName.split(', ')
  const region = rest.join(', ')

  return (
    <header className="border-b border-border bg-background">
      <div className="mx-auto flex w-full max-w-[58rem] flex-wrap items-end justify-between gap-x-6 gap-y-4 px-4 pt-5 pb-5 md:px-10 md:pt-7">
        <div className="min-w-0">
          {region ? (
            <p className="flex items-center gap-1.5 text-body-sm font-medium text-muted-foreground">
              <MapPin className="size-3.5 shrink-0" aria-hidden="true" />
              {region}
            </p>
          ) : null}
          <h1 className="mt-1 truncate text-[2rem] leading-[2.25rem] type-display text-heading md:text-h1">
            {place}
            {region ? <span className="sr-only">, {region}</span> : null}
          </h1>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {startDate && endDate ? (
              <span className="inline-flex h-7 items-center gap-1.5 rounded-full bg-surface px-3 text-body-sm font-medium text-heading shadow-xs">
                <CalendarDays className="size-3.5 shrink-0 text-primary" aria-hidden="true" />
                {formatDateRange(startDate, endDate)}
                {days ? (
                  <span className="ml-1 border-l border-border pl-2 font-normal text-muted-foreground">
                    {days} {days === 1 ? 'day' : 'days'}
                  </span>
                ) : null}
              </span>
            ) : null}
            {timing ? (
              <span className="inline-flex h-7 items-center rounded-full bg-primary-subtle px-3 text-body-sm font-medium text-primary">
                {timing}
              </span>
            ) : null}
            {interests.length > 0 ? (
              <ul className="flex flex-wrap gap-1.5" aria-label="Interests">
                {interests.map((interest) => (
                  <li
                    key={interest}
                    className="inline-flex h-7 items-center rounded-full border border-input px-2.5 text-body-sm text-muted-foreground"
                  >
                    {titleCaseSlug(interest)}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>

        <TripContextPopover trip={trip} />
      </div>
    </header>
  )
}
