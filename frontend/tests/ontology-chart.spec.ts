/**
 * ontologyChart — 「本体」页纯函数（前端方案 §3.4/§8）。
 *
 * 图规格不进 ECharts：option 由纯函数构造，测试直接断言 option 数据，
 * 不挂实例、不用画布 —— 声明/未声明样式、fan_out 警示、标签回退、
 * 布局确定性、规模开关、形状守卫与深链 URL 全部在这里钉死。
 */
import { describe, it, expect } from 'vitest'
import {
  GRAPH_SCALE_LIMIT,
  buildGraphOption,
  graphScaleHintNeeded,
  isFanout,
  isObjectGraph,
  linkLabel,
  objectLabel,
  ontologyEditHref,
  ontologyGraphTokens,
  type OntologyGraphTokens,
} from '../src/utils/ontologyChart'
import type { ObjectLinkInfo, ObjectNodeInfo } from '../src/api/types'

const TOKENS: OntologyGraphTokens = {
  accent: '#1111ff',
  text: '#111111',
  secondary: '#777777',
  border: '#dddddd',
  warn: '#ff8800',
  surface: '#ffffff',
}

const OBJECTS: ObjectNodeInfo[] = [
  {
    dataset: 'loan',
    display_name: '贷款',
    declared: true,
    natural_keys: ['loan_id'],
    aliases: ['借据'],
    metrics: ['total_loan_amount'],
    field_count: 8,
  },
  { dataset: 'account', declared: false },
]

const LINKS: ObjectLinkInfo[] = [
  {
    name: 'loan_to_account',
    from: 'loan',
    to: 'account',
    from_columns: ['account_id'],
    to_columns: ['account_id'],
    cardinality: '1:N',
    fan_out: false,
    display_name: '发放',
  },
  {
    name: 'order_items',
    from: 'account',
    to: 'loan',
    from_columns: ['order_id'],
    to_columns: ['loan_id'],
    cardinality: 'M:N',
    fan_out: true,
  },
]

interface NodeShape {
  name: string
  label: { formatter: string; color: string }
  itemStyle: { color: string; borderType: string; opacity: number }
  onto: { kind: string; ref: string }
}
interface EdgeShape {
  source: string
  target: string
  label: { formatter: string; color: string }
  lineStyle: { color: string; type: string }
  onto: { fanout: boolean }
}
interface GraphSeries {
  type: string
  layout: string
  layoutAnimation: boolean
  force: { initLayout: string }
  data: NodeShape[]
  links: EdgeShape[]
}

function seriesOf(opt: Record<string, unknown>): GraphSeries {
  const series = opt.series as GraphSeries[]
  expect(series).toHaveLength(1)
  return series[0]
}

describe('buildGraphOption — 图规格', () => {
  it('声明对象实心主色；未声明虚线弱化且标签回退数据集名', () => {
    const s = seriesOf(buildGraphOption(OBJECTS, LINKS, TOKENS))
    const declared = s.data.find((n) => n.name === 'loan')!
    expect(declared.label.formatter).toBe('贷款')
    expect(declared.itemStyle).toMatchObject({
      color: TOKENS.accent,
      borderType: 'solid',
      opacity: 1,
    })
    const undeclared = s.data.find((n) => n.name === 'account')!
    expect(undeclared.label.formatter).toBe('account') // display_name 缺席 → 数据集名
    expect(undeclared.itemStyle.borderType).toBe('dashed')
    expect(undeclared.itemStyle.opacity).toBeLessThan(1)
    // 结构契约：每个节点指回数据集（tooltip / 点击事件只认这一份摘要）。
    expect(s.data.map((n) => n.onto)).toEqual([
      { kind: 'object', ref: 'loan', label: '贷款', dataset: 'loan' },
      { kind: 'object', ref: 'account', label: 'account', dataset: 'account' },
    ])
  })

  it('边：display_name 缀基数；缺 display_name 回退列连接文案；方向 from → to', () => {
    const s = seriesOf(buildGraphOption(OBJECTS, LINKS, TOKENS))
    const named = s.links.find((l) => l.source === 'loan')!
    expect(named.label.formatter).toBe('发放 · 1:N')
    expect(named.target).toBe('account')
    const fallback = s.links.find((l) => l.source === 'account')!
    expect(fallback.label.formatter).toBe('order_id → loan_id · M:N')
  })

  it('fan_out 边：警示色 + 虚线；普通边用边框色实线', () => {
    const s = seriesOf(buildGraphOption(OBJECTS, LINKS, TOKENS))
    const risky = s.links.find((l) => l.onto.fanout)!
    expect(risky.lineStyle).toMatchObject({ color: TOKENS.warn, type: 'dashed' })
    expect(risky.label.color).toBe(TOKENS.warn)
    const plain = s.links.find((l) => !l.onto.fanout)!
    expect(plain.lineStyle).toMatchObject({ color: TOKENS.border, type: 'solid' })
  })

  it('布局确定性：force + initLayout circular + layoutAnimation/animation 关闭', () => {
    const opt = buildGraphOption(OBJECTS, LINKS, TOKENS)
    const s = seriesOf(opt)
    expect(s.type).toBe('graph')
    expect(s.layout).toBe('force')
    expect(s.force.initLayout).toBe('circular')
    expect(s.layoutAnimation).toBe(false)
    expect(opt.animation).toBe(false)
  })

  it('tooltip：fan_out 边带解释行（文案由调用方注入 —— 不写死语言）', () => {
    const opt = buildGraphOption(OBJECTS, LINKS, TOKENS, { fanoutWarn: '扇出风险' })
    const formatter = (opt.tooltip as { formatter: (p: unknown) => string }).formatter
    const risky = seriesOf(opt).links.find((l) => l.onto.fanout)!
    const plain = seriesOf(opt).links.find((l) => !l.onto.fanout)!
    expect(formatter({ data: risky })).toContain('扇出风险')
    expect(formatter({ data: plain })).not.toContain('扇出风险')
    // 节点 tooltip = 标签 + 数据集名。
    const node = seriesOf(opt).data[0]
    expect(formatter({ data: node })).toContain('贷款')
    expect(formatter({ data: node })).toContain('loan')
    // 无摘要 → 空串（不抛）。
    expect(formatter({})).toBe('')
    // 未注入文案 → 无解释行（但不报错）。
    const bare = buildGraphOption(OBJECTS, LINKS, TOKENS)
    const bareFmt = (bare.tooltip as { formatter: (p: unknown) => string }).formatter
    expect(bareFmt({ data: risky })).not.toContain('扇出风险')
  })
})

describe('fan_out 归一化', () => {
  it('布尔直传；模型原值（空串）按编译器判据（空串 + M:N = 有风险）收敛', () => {
    expect(isFanout({ fan_out: true })).toBe(true)
    expect(isFanout({ fan_out: false })).toBe(false)
    expect(isFanout({ fan_out: '', cardinality: 'M:N' })).toBe(true)
    expect(isFanout({ fan_out: '', cardinality: '1:N' })).toBe(false)
    // 唯一已实现的豁免是 dedup（与编译器 _classify_edges 同口径）。
    expect(isFanout({ fan_out: 'dedup', cardinality: 'M:N' })).toBe(false)
    // bridge:<dataset> 豁免未实现 → 保守仍判有风险。
    expect(isFanout({ fan_out: 'bridge:account', cardinality: 'M:N' })).toBe(true)
  })

  it('M:N 判定是归一化拼写闭集（照 compiler._is_many_to_many）：N:M / M2M 不漏', () => {
    // 偏离字面 'M:N' 的拼写仍须认出 M:N（漏判 = 警示静默消失，错方向）。
    expect(isFanout({ fan_out: '', cardinality: 'N:M' })).toBe(true)
    expect(isFanout({ fan_out: '', cardinality: 'M2M' })).toBe(true)
    expect(isFanout({ fan_out: 'bridge:ds', cardinality: 'M2M' })).toBe(true)
    // 归一化后是 M:N 但已 dedup 豁免 → 无风险。
    expect(isFanout({ fan_out: 'dedup', cardinality: 'M2M' })).toBe(false)
    // 非 M:N → false（含拼写变体）。
    expect(isFanout({ fan_out: '', cardinality: 'N:1' })).toBe(false)
    expect(isFanout({ fan_out: '', cardinality: 'many-to-one' })).toBe(false)
  })
})

describe('规模开关与形状守卫', () => {
  it('节点数 > 60 才提示（不聚合）', () => {
    expect(GRAPH_SCALE_LIMIT).toBe(60)
    expect(graphScaleHintNeeded(60)).toBe(false)
    expect(graphScaleHintNeeded(61)).toBe(true)
  })

  it('形状守卫：缺 objects/links 一律拒绝；空数组是合法空图', () => {
    expect(isObjectGraph({ objects: [], links: [] })).toBe(true)
    expect(isObjectGraph({ objects: [{ dataset: 'a', declared: false }], links: [] })).toBe(true)
    expect(isObjectGraph({ objects: [] })).toBe(false)
    expect(isObjectGraph({ links: [] })).toBe(false)
    expect(isObjectGraph({ objects: {}, links: [] })).toBe(false)
    expect(isObjectGraph(null)).toBe(false)
    expect(isObjectGraph('nope')).toBe(false)
  })
})

describe('标签与深链', () => {
  it('objectLabel / linkLabel 的回退链是逐字回退，不是猜测', () => {
    expect(objectLabel({ dataset: 'loan', display_name: '  贷款 ' })).toBe('贷款')
    expect(objectLabel({ dataset: 'loan', display_name: null })).toBe('loan')
    expect(linkLabel({ name: 'x', from: 'a', to: 'b' })).toBe('a → b')
    expect(
      linkLabel({ name: 'x', from: 'a', to: 'b', from_columns: ['c1'], to_columns: ['c2'], cardinality: '' }),
    ).toBe('c1 → c2')
  })

  it('深链：对象带 tab=datasets + q；链接进 tab=relationships；页内无第二编辑面', () => {
    expect(ontologyEditHref('object', 'loan', 'demo')).toBe(
      '/admin/semantic?ds=demo&tab=datasets&q=loan',
    )
    expect(ontologyEditHref('link', 'loan_to_account', 'demo')).toBe(
      '/admin/semantic?ds=demo&tab=relationships',
    )
    // ds 缺席 → 不编造参数。
    expect(ontologyEditHref('object', 'loan', '')).toBe(
      '/admin/semantic?tab=datasets&q=loan',
    )
  })
})

describe('ontologyGraphTokens', () => {
  it('jsdom 下回退到设计 token 的缺省值（与 chart.spec 同款）', () => {
    const tokens = ontologyGraphTokens()
    expect(tokens.accent).toBe('#6366f1')
    expect(tokens.warn.length).toBeGreaterThan(3)
  })

  it('明/暗两档：token 逐字进 option（配色不新增色板，只读 token）', () => {
    const dark: OntologyGraphTokens = {
      accent: '#a5b4fc',
      text: '#f4f4f5',
      secondary: '#a1a1aa',
      border: '#3f3f46',
      warn: '#fbbf24',
      surface: '#18181b',
    }
    const light = seriesOf(buildGraphOption(OBJECTS, LINKS, TOKENS))
    const night = seriesOf(buildGraphOption(OBJECTS, LINKS, dark))
    expect(light.data[0].itemStyle.color).toBe(TOKENS.accent)
    expect(night.data[0].itemStyle.color).toBe(dark.accent)
    expect(light.data[0].label.color).toBe(TOKENS.text)
    expect(night.data[0].label.color).toBe(dark.text)
    expect(light.links[0].lineStyle.color).toBe(TOKENS.border)
    expect(night.links[0].lineStyle.color).toBe(dark.border)
  })
})
