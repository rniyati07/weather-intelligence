import { AssistantMark } from '@/components/common/BrandMark'

/**
 * The turn in progress. A single sweeping bar inside a hairline track — the
 * service is computing a forecast, not typing, so it reads as work in
 * progress rather than a bouncing-dots chat bubble. Capped by the reduced-
 * motion rules in `globals.css` like every other animation.
 */
export function TypingIndicator() {
  return (
    <div className="flex items-center gap-3.5" role="status">
      <AssistantMark />

      <p className="flex items-center gap-3 text-body-sm text-muted-foreground">
        Reading the forecast for your trip
        <span className="inline-flex h-1 w-20 overflow-hidden rounded-full bg-primary-subtle">
          <span className="h-full w-1/3 animate-[loading-sweep_1.1s_ease-in-out_infinite] rounded-full bg-primary" />
        </span>
        <span className="sr-only">Planning your trip…</span>
      </p>
    </div>
  )
}
