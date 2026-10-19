import { describe, expect, it } from 'vitest'
import { tierBadge } from '../src/components/kb/evalsetTier'

const t = (k: string) => k

describe('evalset tier badge', () => {
  it('maps known tiers to i18n keys and distinct classes', () => {
    expect(tierBadge('verified', t)).toEqual({ label: 'kbEvalsetTierVerified', className: 'tier-verified' })
    expect(tierBadge('semantic', t).className).toBe('tier-semantic')
    expect(tierBadge('synthetic', t).className).toBe('tier-synthetic')
    expect(tierBadge('benchmark', t).className).toBe('tier-benchmark')
  })
  it('shows unknown tier verbatim instead of guessing a color', () => {
    expect(tierBadge('future-tier', t)).toEqual({ label: 'future-tier', className: 'tier-unknown' })
  })
})
