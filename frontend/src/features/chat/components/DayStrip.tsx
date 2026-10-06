import { AlertTriangle, CircleDot, Droplets, Sun, TriangleAlert } from 'lucide-react'
import type { KeyboardEvent } from 'react'

import { WeatherIcon } from '@/components/weather/WeatherIcon'
import { cn } from '@/lib/utils'
import type { IsoDate } from '@/types'
import { formatDate } from '@/utils/date'
import { formatPercent, formatTemperature } from '@/utils/format'
import { dayStateLabel, type DayState, type TripDay } from '../trip-intelligence'

export interface DayStripProps {
  days: TripDay[]
  selectedDate: IsoDate | null
  onSelect: (date: IsoDate) => void
}

const STATE_STYLE: Record<DayState, { text: string; bar: string; icon: typeof Sun }> = {
  best: { text: 'text-risk-low', bar: 'bg-risk-low', icon: Sun },
  'watch-out': { text: 'text-risk-moderate', bar: 'bg-risk-moderate', icon: AlertTriangle },
  caution: { text: 'text-risk-moderate', bar: 'bg-risk-moderate', icon: TriangleAlert },
  steady: { text: 'text-muted-foreground', bar: 'bg-border-strong', icon: CircleDot },
}

/** Height, in px, of the temperature track every day's bar is drawn in. */
const TRACK_PX = 64

/**
 * The trip's days as one connected forecast ribbon.
 *
 * One column per day the backend actually returned, so a 2-day trip shows two
 * and a 9-day trip shows nine; nothing is padded. Each column's high/low bar
 * sits on a scale shared across the whole trip (the coldest low at the bottom
 * of the track, the warmest high at the top), so the trip's shape — the warm
 * spell, the cold snap — reads across the row without comparing numbers.
 * Every value comes from that day's own `dailyIntelligence.summary`.
 *
 * A tablist: arrow keys move between days, and the selected day is marked by
 * position, weight and `aria-selected`, never colour alone.
 */
export function DayStrip({ days, selectedDate, onSelect }: DayStripProps) {
  if (days.length === 0) return null

  const temps = days.flatMap(({ day }) =>
    [day.summary.tempMinC, day.summary.tempMaxC].filter(
      (value): value is number => typeof value === 'number',
    ),
  )
  const floor = temps.length > 0 ? Math.min(...temps) : 0
  const ceiling = temps.length > 0 ? Math.max(...temps) : 1
  const span = Math.max(ceiling - floor, 1)
  const toPx = (celsius: number) => ((celsius - floor) / span) * TRACK_PX

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = days.findIndex((entry) => entry.date === selectedDate)
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
    if (step === 0) return
    event.preventDefault()
    const next = days[Math.min(days.length - 1, Math.max(0, index + step))]
    if (next) {
      onSelect(next.date)
      event.currentTarget.querySelector<HTMLButtonElement>(`[data-date="${next.date}"]`)?.focus()
    }
  }

  return (
    // `min-w-0` keeps the scroll *inside* this row: without it the flex item
    // sizes to its content and the overflow escapes to the page.
    <div className="w-full min-w-0 scroll-thin overflow-x-auto rounded-xl bg-surface shadow-sm">
      <div
        role="tablist"
        aria-label="Days of your trip"
        onKeyDown={handleKeyDown}
        className="flex min-w-full"
      >
        {days.map((entry, index) => {
          const selected = entry.date === selectedDate
          const { tempMaxC, tempMinC, condition, precipitationProbability } = entry.day.summary
          const style = STATE_STYLE[entry.state]
          const StateIcon = style.icon
          const hasRange = typeof tempMaxC === 'number' && typeof tempMinC === 'number'

          return (
            <button
              key={entry.date}
              type="button"
              role="tab"
              data-date={entry.date}
              aria-selected={selected}
              tabIndex={selected ? 0 : -1}
              onClick={() => {
                onSelect(entry.date)
              }}
              className={cn(
                'relative flex min-w-[5.75rem] flex-1 flex-col items-center px-2 pt-4 pb-3.5 text-center transition-colors',
                'focus-visible:z-10 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none focus-visible:ring-inset',
                index > 0 && 'border-l border-border',
                selected ? 'bg-primary-subtle' : 'hover:bg-muted/60',
              )}
            >
              {selected ? (
                <span className="absolute inset-x-0 top-0 h-[3px] bg-primary" aria-hidden="true" />
              ) : null}

              <span
                className={cn(
                  'text-caption',
                  selected ? 'font-semibold text-primary' : 'text-muted-foreground',
                )}
              >
                {formatDate(entry.date, 'weekdayAbbr')}
              </span>
              <span className="mt-0.5 text-body-sm font-semibold text-heading">
                {formatDate(entry.date, 'compact')}
              </span>

              <WeatherIcon
                condition={condition}
                className={cn('mt-3 size-6', selected ? 'text-primary' : 'text-muted-foreground')}
              />

              <span className="tabular mt-3 text-body-sm font-semibold text-heading">
                {formatTemperature(tempMaxC, 'celsius', { withUnit: false })}
              </span>
              <span
                className="relative my-1.5 w-1.5 rounded-full bg-muted"
                style={{ height: TRACK_PX }}
                aria-hidden="true"
              >
                {hasRange ? (
                  <span
                    className={cn('absolute inset-x-0 rounded-full', style.bar)}
                    style={{
                      bottom: toPx(tempMinC),
                      height: Math.max(toPx(tempMaxC) - toPx(tempMinC), 4),
                    }}
                  />
                ) : null}
              </span>
              <span className="tabular text-body-sm text-muted-foreground">
                {formatTemperature(tempMinC, 'celsius', { withUnit: false })}
              </span>

              <span className="mt-2.5 inline-flex items-center gap-1 text-caption text-muted-foreground">
                <Droplets className="size-3" aria-hidden="true" />
                <span className="tabular">{formatPercent(precipitationProbability)}</span>
                <span className="sr-only">chance of rain</span>
              </span>

              <span
                className={cn(
                  'mt-2 inline-flex items-center gap-1 text-caption font-medium',
                  style.text,
                )}
              >
                <StateIcon className="size-3" aria-hidden="true" />
                {dayStateLabel(entry.state)}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
