import { APP_WORDMARK } from '@/constants/app'
import { cn } from '@/lib/utils'

/** A sun meeting a horizon line: weather and travel in one glyph. */
function HorizonGlyph() {
  return (
    <svg viewBox="0 0 20 20" className="size-4" fill="none">
      <path d="M5 12a5 5 0 0 1 10 0" stroke="currentColor" strokeWidth="1.8" />
      <path d="M2.5 15h15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path
        d="M10 3.5V5.2M4.4 6.1l1.2 1.2M15.6 6.1l-1.2 1.2"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  )
}

export interface BrandMarkProps {
  /** `dark` sits on the navy rail; `light` on a white bar. */
  tone: 'dark' | 'light'
  className?: string
  /** Extra classes for the wordmark text — e.g. hiding it on a narrow bar. */
  wordmarkClassName?: string
}

/**
 * The product mark beside the two-part wordmark. The glyph is decorative;
 * the wordmark text is the accessible name.
 */
export function BrandMark({ tone, className, wordmarkClassName }: BrandMarkProps) {
  return (
    <span className={cn('inline-flex items-center gap-2.5', className)}>
      <span
        className="flex size-7 shrink-0 items-center justify-center rounded-[9px] bg-primary text-primary-foreground"
        aria-hidden="true"
      >
        <HorizonGlyph />
      </span>
      <span className={cn('text-h4 leading-none type-display', wordmarkClassName)}>
        <span className={tone === 'dark' ? 'text-rail-heading' : 'text-heading'}>
          {APP_WORDMARK.primary}
        </span>
        <span className={tone === 'dark' ? 'text-rail-muted' : 'text-muted-foreground'}>
          {APP_WORDMARK.secondary}
        </span>
      </span>
    </span>
  )
}

/** The assistant's avatar in the transcript — the same glyph at small size,
 * so a reply is attributed by the product's own mark rather than a generic
 * chatbot icon. */
export function AssistantMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'flex size-7 shrink-0 items-center justify-center rounded-[9px] bg-primary-subtle text-primary',
        className,
      )}
      aria-hidden="true"
    >
      <HorizonGlyph />
    </span>
  )
}
