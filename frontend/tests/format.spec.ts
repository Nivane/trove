import { describe, it, expect } from 'vitest'
import { fmtVal, trunc, fmtDuration } from '../src/utils/format'

describe('format utils', () => {
  it('formats numbers with locale grouping', () => {
    expect(fmtVal(1234567)).toBe('1,234,567')
    expect(fmtVal(12.567)).toBe('12.57')
  })

  it('handles nullish values as empty string', () => {
    expect(fmtVal(null)).toBe('')
    expect(fmtVal(undefined)).toBe('')
  })

  it('truncates long strings with ellipsis', () => {
    expect(trunc('abcdef', 3)).toBe('abc…')
    expect(trunc('abc', 3)).toBe('abc')
  })

  it('formats durations in ms and seconds', () => {
    expect(fmtDuration(500)).toBe('500ms')
    expect(fmtDuration(5000)).toBe('5.0s')
    expect(fmtDuration(undefined)).toBe('')
  })
})
