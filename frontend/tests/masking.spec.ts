import { describe, it, expect } from 'vitest'
import { maskingBadge, maskedColumnModes, normalizeField } from '../src/utils/masking'

describe('normalizeField', () => {
  it('ignores case, separators and quoting', () => {
    expect(normalizeField('ID_Card')).toBe(normalizeField('id card'))
    expect(normalizeField('"phone"')).toBe('phone')
    expect(normalizeField('c.phone')).toBe('phone')
  })

  it('keeps CJK names intact', () => {
    expect(normalizeField('身份证号')).toBe('身份证号')
    expect(normalizeField('身份证号')).not.toBe(normalizeField('姓名'))
  })
})

describe('maskedColumnModes', () => {
  const report = { fields: { phone: 'partial', id_card: 'hash' }, bypass: false }

  it('marks only the columns that were rewritten', () => {
    expect(maskedColumnModes(['name', 'phone', 'count'], report)).toEqual({
      1: 'partial',
    })
  })

  it('matches a header that differs only in case/separators', () => {
    expect(maskedColumnModes(['ID Card'], report)).toEqual({ 0: 'hash' })
  })

  it('marks nothing without a report', () => {
    expect(maskedColumnModes(['phone'], null)).toEqual({})
  })

  it('does not guess: a report with no matching header marks nothing', () => {
    // 列被 LLM 改了别名 / 走了聚合(后端对函数投影有意不脱敏),名字对不上
    // 就不标 —— 宁可少标一列,不给一列没被改写的打上「已脱敏」。
    expect(maskedColumnModes(['p', 'cnt'], report)).toEqual({})
  })

  it('ignores an empty header', () => {
    expect(maskedColumnModes(['', '  '], { fields: { '': 'hash' } })).toEqual({})
  })
})

describe('maskingBadge', () => {
  it('is null when the step did not run or nothing changed', () => {
    expect(maskingBadge(null)).toBeNull()
    expect(maskingBadge({ fields: {}, bypass: false })).toBeNull()
  })

  it('reports the number of rewritten fields', () => {
    expect(maskingBadge({ fields: { phone: 'partial', id_card: 'hash' } })).toEqual({
      kind: 'masked',
      count: 2,
    })
  })

  it('bypass is its own notice even with an empty field map', () => {
    // 持 pii 的运行 fields 是空的 —— 但它恰恰是最该说一句的那次
    expect(maskingBadge({ fields: {}, bypass: true })).toEqual({
      kind: 'bypass',
      count: 0,
    })
  })
})
