import { Droplets, Thermometer, Wind } from 'lucide-react'
import type { ReactNode } from 'react'

import { WeatherIcon } from '@/components/weather/WeatherIcon'
import { cn } from '@/lib/utils'
import { formatDate } from '@/utils/date'
import { formatActivity, formatAdvisory, formatCondition, TONE_CLASSES } from '@/utils/domain'
import { formatPercent, formatTemperature, formatWindSpeed } from '@/utils/format'
import { dayReasons, dayStateLabel, topActivity, type TripDay } from '../trip-intelligence'

export interface DayDetailPanelProps {
  entry: TripDay
}

/**
 * The selected day, explained.
 *
 * Every line is a field the engine produced: the advisory, the risk-factor
 * descriptions (verbatim), the strongest activity and the day's own weather
 * summary. The component authors no guidance of its own.
 */
export function DayDetailPanel({ entry }: DayDetailPanelProps) {
  const { day } = entry
  const advisory = formatAdvisory(day.travelAdvisory)
  const condition = formatCondition(day.summary.condition)
  const reasons = dayReasons(day)
  const best = topActivity(day)

  return (
    <section
      className="rounded-xl bg-surface p-5 shadow-sm md:p-7"
      aria-label={`Details for ${formatDate(entry.date, 'medium')}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="flex min-w-0 items-start gap-4">
          <span className="flex size-12 shrink-0 items-center justify-center rounded-lg bg-primary-subtle">
            <WeatherIcon condition={day.summary.condition} className="size-6 text-primary" />
          </span>
          <div className="min-w-0">
            <p className="text-body-sm font-medium text-muted-foreground">
              {dayStateLabel(entry.state)}
            </p>
            <h3 className="mt-0.5 text-h3 font-semibold text-heading">
              {formatDate(entry.date, 'weekdayLong')}, {formatDate(entry.date, 'compact')}
            </h3>
            <p className="mt-0.5 text-body text-muted-foreground">
              {condition.label}
              {best ? (
                <>
                  , best for{' '}
                  <span className="font-medium text-foreground">
                    {formatActivity(best.activity).toLowerCase()}
                  </span>{' '}
                  <span className="tabular">({best.score}/100)</span>
                </>
              ) : null}
            </p>
          </div>
        </div>

        <span
          className={cn(
            'inline-flex items-center gap-2 rounded-full px-3 py-1 text-body-sm font-semibold',
            TONE_CLASSES[advisory.tone].surface,
            TONE_CLASSES[advisory.tone].text,
          )}
        >
          <span
            className={cn('size-2 rounded-full', TONE_CLASSES[advisory.tone].dot)}
            aria-hidden="true"
          />
          {advisory.label}
        </span>
      </div>

      <dl className="mt-6 grid grid-cols-3 gap-2 md:gap-3">
        <Stat icon={<Thermometer className="size-4" aria-hidden="true" />} label="High / low">
          {formatTemperature(day.summary.tempMaxC)}
          <span className="text-muted-foreground">
            {' '}
            / {formatTemperature(day.summary.tempMinC)}
          </span>
        </Stat>
        <Stat icon={<Droplets className="size-4" aria-hidden="true" />} label="Chance of rain">
          {formatPercent(day.summary.precipitationProbability)}
        </Stat>
        <Stat icon={<Wind className="size-4" aria-hidden="true" />} label="Wind">
          {formatWindSpeed(day.summary.windSpeedKph)}
        </Stat>
      </dl>

      {reasons.length > 0 ? (
        <div className="mt-6">
          <h4 className="text-body-sm font-medium text-muted-foreground">What to keep in mind</h4>
          <ul className="mt-2.5 flex flex-col gap-2">
            {reasons.map((factor) => {
              const tone = factor.severity === 'high' ? 'high' : 'moderate'
              return (
                <li key={factor.rule} className="flex gap-3 text-body text-foreground">
                  <span
                    className={cn(
                      'mt-[0.55rem] size-2 shrink-0 rounded-full',
                      TONE_CLASSES[tone].dot,
                    )}
                    aria-hidden="true"
                  />
                  <span>
                    {factor.description}
                    <span className="sr-only">
                      {' '}
                      ({factor.severity === 'high' ? 'high' : 'moderate'} severity)
                    </span>
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

function Stat({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg bg-background px-3 py-3 md:px-4">
      <dt className="flex items-center gap-1.5 text-caption text-muted-foreground">
        <span className="text-subtle-foreground">{icon}</span>
        <span className="truncate">{label}</span>
      </dt>
      <dd className="tabular mt-1 truncate text-body font-semibold text-heading md:text-h4">
        {children}
      </dd>
    </div>
  )
}
