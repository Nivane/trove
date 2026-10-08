import { describe, expect, it } from 'vitest'
import { changeStatusTone, impactLines, verdictKey } from '../src/utils/semantic-changes'

describe('semantic changes helpers', () => {
  it('maps statuses to tones', () => {
    expect(changeStatusTone('open')).toBe('warn')
    expect(changeStatusTone('merged')).toBe('ok')
    expect(changeStatusTone('rejected')).toBe('muted')
    expect(changeStatusTone('stale')).toBe('danger')
    expect(changeStatusTone('whatever')).toBe('muted')
  })

  it('maps verdicts to i18n keys, unknown included', () => {
    expect(verdictKey('improves')).toBe('semVerdictImproves')
    expect(verdictKey('neutral')).toBe('semVerdictNeutral')
    expect(verdictKey('regresses')).toBe('semVerdictRegresses')
    expect(verdictKey('not_applicable')).toBe('semVerdictNotApplicable')
    expect(verdictKey('unknown')).toBe('semVerdictUnknown')
    expect(verdictKey('')).toBe('semVerdictUnknown')
  })

  it('renders impact lines with basis suffix and kind labels', () => {
    const lines = impactLines({
      metrics: ['refund_rate'], examples: ['退款率是多少'], rules: ['revenue_drop'],
      lessons: [], topics: ['credits'],
      basis: { refund_rate: 'reference', revenue_drop: 'reference', credits: 'reference', '退款率是多少': 'mention' },
    }, (k: string) => k)
    expect(lines).toEqual([
      'semImpactMetric: refund_rate (reference)',
      'semImpactRule: revenue_drop (reference)',
      'semImpactTopic: credits (reference)',
      'semImpactExample: 退款率是多少 (mention)',
    ])
  })

  it('renders nothing for an empty impact', () => {
    expect(impactLines({ metrics: [], examples: [], rules: [], lessons: [], topics: [], basis: {} },
      (k: string) => k)).toEqual([])
  })
})
