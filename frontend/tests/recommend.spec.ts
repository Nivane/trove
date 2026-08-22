import { describe, it, expect } from 'vitest'
import { mergeRecommendations } from '../src/utils/recommend'

describe('mergeRecommendations', () => {
  it('dedupes and caps, examples first', () => {
    const out = mergeRecommendations(
      ['平均成绩是多少', '各地区贷款额', '平均成绩是多少'],
      [{ title: '贷款最多的地区' }, { title: '' }, { title: undefined }],
      2,
    )
    expect(out).toEqual(['平均成绩是多少', '各地区贷款额'])
  })

  it('falls back to recent session titles when no examples', () => {
    const out = mergeRecommendations([], [{ title: '哪个地区最高' }], 4)
    expect(out).toEqual(['哪个地区最高'])
  })
})
