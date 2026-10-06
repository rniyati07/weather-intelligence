import { describe, expect, it } from 'vitest'

import { cn } from './utils'

describe('cn', () => {
  it('keeps a text colour alongside a design-system font size', () => {
    const merged = cn('text-primary-foreground', 'text-body')
    expect(merged).toContain('text-primary-foreground')
    expect(merged).toContain('text-body')
  })

  it('still resolves two font sizes last-wins', () => {
    expect(cn('text-body', 'text-h4')).toBe('text-h4')
  })

  it('still resolves two colours last-wins', () => {
    expect(cn('text-heading', 'text-muted-foreground')).toBe('text-muted-foreground')
  })
})
