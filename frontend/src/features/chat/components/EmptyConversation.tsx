import { CalendarCheck2, CornerDownRight, Gauge, Luggage } from 'lucide-react'

// Illustrate how to phrase a message, never a destination — no place name
// belongs here. A picked prompt only fills the composer, but even an
// editable example must not look like a suggested (or worse, remembered)
// trip.
const PROMPTS = [
  'I’m planning a trip and I’m not sure where yet.',
  'Help me pick a destination for next month.',
  'I want a quiet weekend somewhere warm.',
] as const

// What every analysed trip contains — a description of the product's real
// output, not sample data.
const OUTPUTS = [
  {
    icon: Gauge,
    title: 'A trip outlook',
    body: 'One suitability score for your dates, with the risk level and how confident the forecast is.',
  },
  {
    icon: CalendarCheck2,
    title: 'Your best and watch-out days',
    body: 'Which day suits your plans best, and which one to keep flexible.',
  },
  {
    icon: Luggage,
    title: 'Day by day, places and packing',
    body: 'Each day’s forecast, places matched to the weather, and what to bring.',
  },
] as const

export interface EmptyConversationProps {
  onPick: (text: string) => void
}

/**
 * The chat entry's welcome state — this *is* the landing page now (master
 * prompt, "Product": "Do NOT create a separate marketing homepage").
 *
 * The example prompts are starting points, not the product: picking one
 * fills the composer rather than sending it immediately, so a first-time
 * visitor can still edit before committing.
 */
export function EmptyConversation({ onPick }: EmptyConversationProps) {
  return (
    <div className="flex flex-1 scroll-thin overflow-y-auto">
      <div className="mx-auto my-auto flex w-full max-w-[58rem] flex-col px-5 py-10 md:px-10 md:py-16">
        <h1 className="text-[2.5rem] leading-[2.75rem] type-display text-heading md:text-display md:leading-[3.75rem]">
          Where are you headed?
        </h1>
        <p className="mt-4 max-w-[34rem] text-body-lg text-muted-foreground">
          Tell me where and when you’re travelling, and what you like to do. I’ll read the forecast
          day by day and tell you how the trip looks.
        </p>

        <div className="mt-9">
          <h2 className="text-body-sm font-medium text-muted-foreground">
            Not sure how to start? Try one of these
          </h2>
          <ul className="mt-3 flex flex-col gap-2">
            {PROMPTS.map((prompt) => (
              <li key={prompt}>
                <button
                  type="button"
                  onClick={() => {
                    onPick(prompt)
                  }}
                  className="group flex w-full items-center gap-3 rounded-lg bg-surface px-4 py-3 text-left text-body text-foreground shadow-xs transition-[box-shadow,color] duration-150 ease-out hover:text-heading hover:shadow-sm focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                  <CornerDownRight
                    className="size-4 shrink-0 text-subtle-foreground transition-colors group-hover:text-primary"
                    aria-hidden="true"
                  />
                  {prompt}
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="mt-12 border-t border-border pt-8">
          <h2 className="text-body-sm font-medium text-muted-foreground">
            What you get for every trip
          </h2>
          <ul className="mt-5 grid gap-6 md:grid-cols-3 md:gap-5">
            {OUTPUTS.map(({ icon: Icon, title, body }) => (
              <li key={title} className="flex gap-3 md:flex-col md:gap-2.5">
                <Icon className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden="true" />
                <div>
                  <p className="text-body font-semibold text-heading">{title}</p>
                  <p className="mt-1 text-body-sm text-muted-foreground">{body}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
