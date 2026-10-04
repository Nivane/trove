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
