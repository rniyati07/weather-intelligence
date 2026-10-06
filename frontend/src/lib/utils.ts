import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

/**
 * `tailwind-merge` only knows Tailwind's built-in font sizes. The design
 * system's own scale (`text-body`, `text-h4`, `text-caption`, …) looks like
 * a *colour* to it, so `text-primary-foreground text-body` silently lost the
 * colour — the primary button rendered dark text on its fill. Registering the
 * scale as font sizes keeps size and colour in separate conflict groups.
 * Keep this list in step with `--text-*` in `styles/globals.css`.
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: [
        'display',
        'h1',
        'h2',
        'h3',
        'h4',
        'body-lg',
        'body',
        'body-sm',
        'caption',
        'metric-lg',
        'metric',
      ],
    },
  },
})

/**
 * Compose class names, resolving Tailwind conflicts last-wins.
 *
 * `clsx` handles conditionals and arrays; `twMerge` resolves collisions, so a
 * caller's `className="p-8"` reliably beats a component's internal `p-6`
 * instead of depending on stylesheet order. Named `cn` because that is the
 * shadcn/ui convention and generated components import it by that name.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

/** Descriptive alias for `cn`, for call sites that read better spelled out. */
export const mergeClasses = cn
