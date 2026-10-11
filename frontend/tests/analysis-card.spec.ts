/**
 * 分析卡 × 噪声带(B8):v1 payload 仍渲染 + series 键在则渲染三态。
 *
 * 版本兼容的机制本身 = 缺席容忍:老 payload(无 series 键)卡片输出
 * 一个字节都不变(band 节根本不渲染);判据是**键在不在,不是版本号**
 * —— 第二组就用 version:1 带 series,照样渲染。卡面与回答 markdown 的
 * 「噪声带」行同源:三态如实、原因译成人话(不出原始键)、
 * 「位置分数(非概率)」限定语不可省。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AnalysisCard from '../src/components/chat/AnalysisCard.vue'
import { useUiStore } from '../src/stores/ui'
import type { AnalysisPayload, AnalysisSeries } from '../src/api/types'

let wrapper: VueWrapper | null = null

/** 完整 v1 payload(无 series 键)—— 与后端 fixture 同形状的旧读端输入。 */
const V1: AnalysisPayload = {
  version: 1,
  kind: 'driver_tree',
  metric: 'profit',
  metric_kind: 'additive',
  labels: { baseline: 'prev_period', baseline_label: '上期' },
  total_delta: -20,
  table: [],
  tree: {
    name: 'profit',
    kind: 'derived',
    current: 80,
    base: 100,
    delta: -20,
    executed: true,
    children: [
      { name: 'revenue', kind: 'leaf', current: 90, base: 130, delta: -40, executed: true },
    ],
  },
  charts: [],
  evidence: { datasource: 'demo', queries: [], truncated: false, degraded: [] },
  partial: false,
}

/** v2 统计节:月块序列 + 可用噪声带,本期超出。 */
const SERIES: AnalysisSeries = {
  grain: 'month',
  mode: 'trailing',
  lookback: 12,
  labels: ['2024-01', '2024-02'],
  values: [40, 42],
  band: { center: 41, scale: 2, lo: 34, hi: 48, n: 12, method: 'robust', degraded: [] },
  current: 70,
  z: 14.5,
  outside: true,
  low_n: false,
  k: 3.5,
  confidence: 1,
}

function mountCard(analysis: AnalysisPayload, lang: 'zh' | 'en' = 'zh') {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = lang
  wrapper = mount(AnalysisCard, {
    props: { analysis },
    global: { plugins: [pinia] },
  })
  return wrapper
}

function bandText(): string {
  const el = wrapper?.find('.ana-band')
  if (!el || !el.exists()) throw new Error('band section not rendered')
  return el.text()
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('AnalysisCard — 噪声带', () => {
  it('v1 payload(无 series)照常渲染,噪声带整节不出现', () => {
    mountCard(V1)
    expect(wrapper?.find('.ana-card').exists()).toBe(true)
    expect(wrapper?.find('.ana-tree').exists()).toBe(true) // 老内容原样
    expect(wrapper?.text()).toContain('总变化')
    expect(wrapper?.find('.ana-band').exists()).toBe(false)
  })

  it('version:1 带 series 也渲染 —— 判据是键在不在,不是版本号', () => {
    mountCard({ ...V1, series: SERIES })
    expect(bandText()).toContain('超出噪声带')
  })

  it('超出带:窗口/中位数/尺度/区间/本期/位置分数齐全', () => {
    mountCard({ ...V1, series: SERIES })
    const text = bandText()
    expect(text).toContain('近 12 个月块')
    expect(text).toContain('中位数 41')
    expect(text).toContain('稳健尺度 2')
    expect(text).toContain('带 [34, 48]')
    expect(text).toContain('本期值 70')
    expect(text).toContain('稳健 z 14.50')
    expect(text).toContain('位置分数 1.00（非概率）') // 限定语不可省
  })

  it('落在带内:quiet 档不出 warn 色', () => {
    mountCard({
      ...V1,
      series: { ...SERIES, current: 45, z: 2, outside: false, confidence: 0 },
    })
    expect(bandText()).toContain('落在噪声带内')
    expect(wrapper?.find('.ana-band .ana-chip.warn').exists()).toBe(false)
  })

  it('带不可用:原因译成人话,裁决判不了,原始键不出面', () => {
    mountCard({
      ...V1,
      series: {
        ...SERIES,
        band: {
          center: null,
          scale: null,
          lo: null,
          hi: null,
          n: 3,
          method: 'robust',
          degraded: ['insufficient_n'],
        },
        outside: null,
        z: null,
        confidence: null,
        low_n: true,
      },
    })
    const text = bandText()
    expect(text).toContain('噪声带不可用（样本不足）')
    expect(text).toContain('判不了')
    expect(text).not.toContain('insufficient_n')
  })

  it('本期值缺失:不编裁决,也不编数', () => {
    mountCard({
      ...V1,
      series: { ...SERIES, current: null, z: null, outside: null, confidence: null },
    })
    const text = bandText()
    expect(text).toContain('本期值缺失')
    expect(text).not.toContain('超出噪声带')
    expect(text).not.toContain('判不了')
  })

  it('outside 是坏值(非布尔)→ 判不了,不是抛错', () => {
    mountCard({
      ...V1,
      series: { ...SERIES, outside: 'yes' } as unknown as AnalysisSeries,
    })
    expect(bandText()).toContain('判不了')
  })

  it('低样本:结论照给,「仅供参考」标照挂', () => {
    mountCard({ ...V1, series: { ...SERIES, low_n: true } })
    const text = bandText()
    expect(text).toContain('超出噪声带')
    expect(text).toContain('样本不足，带估计仅供参考')
  })

  it('series 在但 band 整键缺席 → 原因未记录(不吞也不编)', () => {
    mountCard({
      ...V1,
      series: { ...SERIES, band: null } as AnalysisSeries,
    })
    expect(bandText()).toContain('噪声带不可用（原因未记录）')
  })

  it('英文渲染:词序与「非概率」限定语同后端', () => {
    mountCard({ ...V1, series: SERIES }, 'en')
    const text = bandText()
    expect(text).toContain('Noise band')
    expect(text).toContain('over the last 12 month-blocks')
    expect(text).toContain('median 41')
    expect(text).toContain('position score 1.00 (not a probability)')
    expect(text).toContain('outside the noise band')
  })
})

describe('AnalysisCard — 本体显示名（O3）', () => {
  /** 显示名与裸值两侧都在的 payload:labels 带 primary_dimension_display,
   *  贡献表首行与下钻行带 dim_display(第二行刻意不带)。 */
  const O3: AnalysisPayload = {
    ...V1,
    labels: {
      baseline: 'prev_period',
      baseline_label: '上期',
      primary_dimension: 'SH',
      primary_dimension_display: '上海分行',
    },
    table: [
      { dim: 'SH', dim_display: '上海分行', base: 100, current: 80, delta: -20, contribution: 1 },
      { dim: 'BJ', base: 30, current: 42, delta: 12, contribution: -0.5 },
    ],
    drilldown: {
      dimension: 'product',
      table: [{ dim: 'LN', dim_display: '贷款', base: 5, current: 6, delta: 1, contribution: 0.5 }],
    },
  }

  /** 只摘掉显示名键的同形 payload —— 缺席态必须与现状逐字节一致。 */
  const O3_BARE: AnalysisPayload = {
    ...O3,
    labels: { baseline: 'prev_period', baseline_label: '上期', primary_dimension: 'SH' },
    table: [
      { dim: 'SH', base: 100, current: 80, delta: -20, contribution: 1 },
      { dim: 'BJ', base: 30, current: 42, delta: 12, contribution: -0.5 },
    ],
    drilldown: {
      dimension: 'product',
      table: [{ dim: 'LN', base: 5, current: 6, delta: 1, contribution: 0.5 }],
    },
  }

  it('显示名在:头条/贡献表/下钻显示显示名,裸值进 tooltip', () => {
    mountCard(O3)

    const head = wrapper?.find('.ana-headline-dim')
    expect(head?.text()).toContain('上海分行')
    expect(head?.text()).not.toContain('SH')
    expect(head?.attributes('title')).toBe('SH')

    const cells = wrapper?.find('table.ana-table')?.findAll('tbody tr td:first-child') ?? []
    expect(cells[0]?.text()).toBe('上海分行')
    expect(cells[0]?.attributes('title')).toBe('SH')
    // 未标注的行原样:裸值上屏,也不凭空加 tooltip
    expect(cells[1]?.text()).toBe('BJ')
    expect(cells[1]?.attributes('title')).toBeUndefined()

    const drillCell = wrapper?.find('.ana-drill table.ana-table tbody tr td:first-child')
    expect(drillCell?.text()).toBe('贷款')
    expect(drillCell?.attributes('title')).toBe('LN')
  })

  it('显示名键缺席:裸值照旧上屏,一个 title 都不加(与现状一致)', () => {
    mountCard(O3_BARE)

    const head = wrapper?.find('.ana-headline-dim')
    expect(head?.text()).toContain('SH')
    expect(head?.attributes('title')).toBeUndefined()

    const cells = wrapper?.find('table.ana-table')?.findAll('tbody tr td:first-child') ?? []
    expect(cells.map((c) => c.text())).toEqual(['SH', 'BJ'])
    expect(cells.every((c) => c.attributes('title') === undefined)).toBe(true)

    const drillCell = wrapper?.find('.ana-drill table.ana-table tbody tr td:first-child')
    expect(drillCell?.text()).toBe('LN')
    expect(drillCell?.attributes('title')).toBeUndefined()
  })
})

describe('AnalysisCard — 降级记账（partial + evidence.degraded）', () => {
  const withDegraded = (
    degraded: { stage?: string; reason?: string }[],
  ): AnalysisPayload => ({
    ...V1,
    partial: true,
    evidence: { datasource: 'demo', queries: [], truncated: false, degraded },
  })

  async function openEvidence(): Promise<string> {
    await wrapper?.find('.ana-evidence-toggle').trigger('click')
    return wrapper?.text() ?? ''
  }

  it('partial 挂「部分结果」标；降级原因译成人话，原始码不出面', async () => {
    mountCard(withDegraded([{ stage: 'period', reason: 'no_time_context' }]))
    expect(wrapper?.find('.ana-chip.warn').exists()).toBe(true)
    expect(wrapper?.text()).toContain('部分结果')
    const text = await openEvidence()
    expect(text).toContain('时间窗')
    expect(text).toContain('问题里没有可解析的时间窗')
    expect(text).not.toContain('no_time_context')
  })

  it('前端不认得的码露出 slug（不编造）；带细节的原因保留冒号后缀', async () => {
    mountCard(
      withDegraded([
        { stage: 'brand_new_stage', reason: 'brand_new_reason' },
        { stage: 'series', reason: 'compile_miss:no_metric_match' },
      ]),
    )
    const text = await openEvidence()
    expect(text).toContain('brand_new_stage')
    expect(text).toContain('brand_new_reason')
    expect(text).toContain('块序列')
    expect(text).toContain('序列查询编译不出:no_metric_match')
  })

  it('英文渲染:stage 与 reason 都译', async () => {
    mountCard(withDegraded([{ stage: 'period', reason: 'no_time_field' }]), 'en')
    const text = await openEvidence()
    expect(text).toContain('time window')
    expect(text).toContain('no time field found')
  })

  it('queries 与 degraded 都空:证据抽屉整块不出现（无降级行可挂）', () => {
    mountCard(V1) // V1.evidence.queries = [] 且 degraded = []
    expect(wrapper?.find('.ana-evidence-toggle').exists()).toBe(false)
    expect(wrapper?.find('.ana-degraded-row').exists()).toBe(false)
  })
})
