/**
 * ontologyIdentity — 类型身份（物化层）的确定性派生（纯函数，零 DOM）。
 *
 * 「声明即物化」这条产品判断的测试面：声明过的对象拿到图标 + 身份色，
 * 未声明保持灰色「表」。派生必须**确定性**（同一份模型在任何进程、任何
 * 一次渲染里都是同一个身份），所以哈希要钉死具体值、词表命中要钉死优先级、
 * 兜底色只能落在分类调色板里 —— 一次随机摇色就会让「同一张图」的承诺落空。
 */
import { describe, it, expect } from 'vitest'
import {
  GLYPHS,
  GLYPH_STROKE_WIDTH,
  IDENTITY_PALETTE,
  UNDECLARED_COLOR,
  hashName,
  objectIdentity,
  tileFillOpacity,
  type IconName,
} from '../src/utils/ontologyIdentity'

describe('声明状态决定身份的两档', () => {
  it('未声明 → 灰色「表」，名字里带什么业务词都不参与词表', () => {
    expect(objectIdentity({ dataset: 'account', declared: false })).toEqual({
      declared: false,
      icon: 'table',
      color: UNDECLARED_COLOR,
    })
    // null / undefined 也走未声明档（渲染层不必先判空）。
    expect(objectIdentity(null).icon).toBe('table')
    expect(objectIdentity(undefined).color).toBe(UNDECLARED_COLOR)
  })

  it('已声明 → 词表命中（中英文都认）', () => {
    const cases: [string, IconName][] = [
      ['account', 'bank'],
      ['客户', 'person'],
      ['customer_profile', 'person'],
      ['district', 'pin'],
      ['loan', 'money'],
      ['贷款明细', 'money'],
      ['order', 'receipt'],
      ['transaction', 'swap'],
      ['card', 'card'],
    ]
    for (const [name, icon] of cases) {
      const idn = objectIdentity({ dataset: name, declared: true })
      expect(idn.icon, name).toBe(icon)
      expect(idn.declared).toBe(true)
    }
  })

  it('显示名 / 别名参与匹配（数据集名是代号时也能认出业务概念）', () => {
    expect(objectIdentity({ dataset: 't_001', display_name: '客户', declared: true }).icon).toBe('person')
    expect(
      objectIdentity({ dataset: 't_002', declared: true, aliases: ['贷款合同'] }).icon,
    ).toBe('money')
  })

  it('词表未命中 → 哈希兜底：固定立方体 + 分类调色板里的色（确定性可复现）', () => {
    const a = objectIdentity({ dataset: 'payment', declared: true })
    const b = objectIdentity({ dataset: 'payment', declared: true })
    expect(a).toEqual(b)
    expect(a.icon).toBe('cube')
    expect(IDENTITY_PALETTE).toContain(a.color)
    // 逐字钉住 djb2 的结果（换哈希实现 = 全库身份色重排，测试必须先响）。
    expect(hashName('payment')).toBe(2531075807)
    expect(a.color).toBe(IDENTITY_PALETTE[2531075807 % IDENTITY_PALETTE.length])
  })

  it('身份色不等于状态色：调色板是分类色，不含 ok/warn/danger 族', () => {
    expect(new Set(IDENTITY_PALETTE).size).toBe(IDENTITY_PALETTE.length)
    // warn（amber 系）已被 gold 顶掉：调色板里没有 amber-500/amber-700。
    expect(IDENTITY_PALETTE).not.toContain('#d97706')
    expect(IDENTITY_PALETTE).not.toContain('#b45309')
  })
})

describe('图元与身份砖', () => {
  it('每个图标名都有几何（缺一个 = 画面上一个空白砖，静默的坏）', () => {
    for (const [name, glyph] of Object.entries(GLYPHS)) {
      const count =
        (glyph.paths?.length ?? 0) + (glyph.circles?.length ?? 0) + (glyph.rects?.length ?? 0)
      expect(count, name).toBeGreaterThan(0)
    }
    // 30×30 视野内作画（身份砖 viewBox 与之同源）。
    expect(GLYPH_STROKE_WIDTH).toBeGreaterThan(1)
  })

  it('底色透明度：已声明淡（0.13），未声明更实（0.35）—— 灰「表」要看得见', () => {
    expect(tileFillOpacity({ declared: true, icon: 'cube', color: '#000' })).toBe(0.13)
    expect(tileFillOpacity({ declared: false, icon: 'table', color: UNDECLARED_COLOR })).toBe(0.35)
  })
})
