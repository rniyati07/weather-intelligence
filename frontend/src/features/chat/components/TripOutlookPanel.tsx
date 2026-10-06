import { AlertTriangle, CircleHelp, ShieldCheck, ShieldAlert, Sun } from 'lucide-react'
import type { ReactNode } from 'react'

import { Tooltip } from '@/components/ui/tooltip'
import { WeatherIcon } from '@/components/weather/WeatherIcon'
import type { Tone } from '@/constants/domain'
import { cn } from '@/lib/utils'
import type { ActivitySuitability, DailyIntelligence, TripSummary } from '@/types'
import { formatDate } from '@/utils/date'
import { formatActivity, formatAdvisory, formatRisk, TONE_CLASSES } from '@/utils/domain'
import { formatConfidence, formatTemperature } from '@/utils/format'

export interface TripOutlookPanelProps {
  summary: TripSummary
  /** Only used to say how many days the outlook is based on. */
  dayCount: number
  bestDay: DailyIntelligence | undefined
  worstDay: DailyIntelligence | undefined
}

const CONFIDENCE_SEGMENTS = 5

const RISK_ICONS: Record<Tone, typeof ShieldCheck> = {
  low: ShieldCheck,
  moderate: AlertTriangle,
  high: ShieldAlert,
  unknown: CircleHelp,
}

/**
 * The trip's headline verdict.
 *
 * Score, risk level, confidence and the best/worst dates are all
 * `tripSummary` fields; the supporting line under each day comes from that
 * day's own strongest activity or worst risk factor, authored by the rule
 * engine. Nothing here ranks or scores anything itself.
 *
 * Laid out as one instrument rather than a row of equal cards: the score is
 * read first, then how risky and how certain it is, then the two days that
 * should change the plan.
 */
export function TripOutlookPanel({ summary, dayCount, bestDay, worstDay }: TripOutlookPanelProps) {
  const risk = formatRisk(summary.overallRiskLevel)
  const RiskIcon = RISK_ICONS[risk.tone]
  const confidence = formatConfidence(summary.travelConfidence)
  const filledSegments = Math.round((confidence.value ?? 0) * CONFIDENCE_SEGMENTS)
  const score = Math.max(0, Math.min(100, summary.tripSuitabilityScore))
  const bestDate = summary.bestDays[0]
  const worstDate = summary.worstDays[0]
  // A single-day trip is its own best and worst day; showing both would be a
  // contradiction, so the watch-out half is dropped.
  const sameDay = Boolean(bestDate && worstDate && bestDate === worstDate)

  return (
    <section
      aria-labelledby="trip-outlook-heading"
      className="overflow-hidden rounded-xl bg-surface shadow-sm"
    >
      <div className="grid gap-x-10 gap-y-7 p-5 md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] md:p-7">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <h2
              id="trip-outlook-heading"
              className="text-body-sm font-medium text-muted-foreground"
            >
              Trip outlook
            </h2>
            <InfoTip label="About the trip outlook">
              How well the forecast suits travelling on your dates, from 0 to 100, combined across
              every day of the trip.
            </InfoTip>
          </div>

          <div className="mt-2 flex flex-wrap items-end gap-x-4 gap-y-2">
            <p className="flex items-baseline gap-1.5">
              <span className="text-metric-lg type-display text-heading">{score}</span>
              <span className="text-body text-subtle-foreground">/ 100</span>
            </p>
            <span
              className={cn(
                'mb-2 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-body-sm font-semibold',
                TONE_CLASSES[risk.tone].surface,
                TONE_CLASSES[risk.tone].text,
              )}
            >
              <RiskIcon className="size-4" aria-hidden="true" />
              {risk.label}
            </span>
          </div>

          {/* The score on its own scale — position, not colour, carries it. */}
          <div className="mt-4" aria-hidden="true">
            <div className="relative h-2 rounded-full bg-muted">
              <div
                className={cn(
                  'absolute inset-y-0 left-0 rounded-full',
                  TONE_CLASSES[risk.tone].dot,
                )}
                style={{ width: `${String(score)}%` }}
              />
            </div>
            <div className="mt-1.5 flex justify-between text-caption text-subtle-foreground">
              <span>0</span>
              <span>50</span>
              <span>100</span>
            </div>
          </div>
        </div>

        <div className="min-w-0 md:border-l md:border-border md:pl-10">
          <div className="flex items-center gap-1.5">
            <h3 className="text-body-sm font-medium text-muted-foreground">Travel confidence</h3>
            <InfoTip label="About travel confidence">
              How much to trust this outlook. It is lower for days further ahead, when weather
              sources disagree, or when some data is missing.
            </InfoTip>
          </div>
          <p className="mt-2 text-h3 font-semibold text-heading">{confidence.label}</p>
          <div
            className="mt-3 flex gap-1"
            role="img"
            aria-label={`${String(filledSegments)} of ${String(CONFIDENCE_SEGMENTS)}`}
          >
            {Array.from({ length: CONFIDENCE_SEGMENTS }, (_, index) => (
              <span
                key={index}
                className={cn(
                  'h-2 flex-1 rounded-full',
                  index < filledSegments ? 'bg-primary' : 'bg-muted',
                )}
              />
            ))}
          </div>
          <p className="mt-2.5 text-body-sm text-muted-foreground">
            Based on {dayCount === 1 ? 'a 1-day' : `a ${String(dayCount)}-day`} forecast.
          </p>
        </div>
      </div>

      {bestDate || worstDate ? (
        <div className="grid border-t border-border md:grid-cols-2">
          {bestDate ? (
            <DayDecision
              kind="best"
              label={sameDay ? 'Your travel day' : 'Best day'}
              icon={<Sun className="size-4" aria-hidden="true" />}
              date={bestDate}
              day={bestDay}
              detail={bestDetail(bestDay)}
            />
          ) : null}

          {worstDate && !sameDay ? (
            <DayDecision
              kind="watch-out"
              label="Watch out for"
              icon={<AlertTriangle className="size-4" aria-hidden="true" />}
              date={worstDate}
              day={worstDay}
              detail={worstDetail(worstDay)}
            />
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip content={<span className="block max-w-64">{children}</span>}>
      <button
        type="button"
        aria-label={label}
        className="flex size-6 items-center justify-center rounded-full text-subtle-foreground transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
      >
        <CircleHelp className="size-3.5" aria-hidden="true" />
      </button>
    </Tooltip>
  )
}

/**
 * One of the two days that should shape the plan. A tinted band with an
 * icon and a word, so "good" and "careful" never depend on colour alone.
 */
function DayDecision({
  kind,
  label,
  icon,
  date,
  day,
  detail,
}: {
  kind: 'best' | 'watch-out'
  label: string
  icon: ReactNode
  date: string
  day: DailyIntelligence | undefined
  detail: string | null
}) {
  const tone = kind === 'best' ? TONE_CLASSES.low : TONE_CLASSES.moderate

  return (
    <div
      className={cn(
        'flex gap-4 px-5 py-5 md:px-7',
        kind === 'watch-out' && 'border-t border-border md:border-t-0 md:border-l',
      )}
    >
      <span
        className={cn(
          'flex size-9 shrink-0 items-center justify-center rounded-full',
          tone.surface,
          tone.text,
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className={cn('text-body-sm font-semibold', tone.text)}>{label}</p>
        <p className="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-h4 font-semibold text-heading">
          {formatDate(date, 'weekdayLong')}, {formatDate(date, 'compact')}
          {day ? (
            <span className="inline-flex items-center gap-1 text-body-sm font-normal text-muted-foreground">
              <WeatherIcon condition={day.summary.condition} className="size-4" />
              <span className="tabular">{formatTemperature(day.summary.tempMaxC)}</span>
            </span>
          ) : null}
        </p>
        {detail ? <p className="mt-1 text-body-sm text-muted-foreground">{detail}</p> : null}
      </div>
    </div>
  )
}

/**
 * Why this day is the best one: what it is actually good *for*.
 *
 * The engine ranks days by risk and then by mean activity score, so the
 * honest supporting line for the winner is its own strongest activity — not
 * a risk factor, which a genuinely good day may not have at all.
 */
function bestDetail(day: DailyIntelligence | undefined): string | null {
  if (!day) return null

  const top = day.activitySuitability.reduce<ActivitySuitability | null>(
    (best, candidate) => (best === null || candidate.score > best.score ? candidate : best),
    null,
  )
  if (top === null) return null

  return `Best for ${formatActivity(top.activity).toLowerCase()} (${String(top.score)}/100)`
}

/**
 * Why this day needs watching: the most severe thing the rule engine
 * recorded, verbatim. When it has none (a uniformly calm trip), the advisory
 * carries the meaning instead.
 */
function worstDetail(day: DailyIntelligence | undefined): string | null {
  if (!day) return null

  const factors = day.riskAssessment.riskFactors
  if (factors.length > 0) {
    const high = factors.find((factor) => factor.severity === 'high')
    return (high ?? factors[0])?.description ?? null
  }

  const advisory = formatAdvisory(day.travelAdvisory)
  return `${advisory.label}: the least settled day of the trip`
}
