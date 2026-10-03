import { describe, it, expect } from 'vitest'
import {
  chartTheme,
  applyChartTheme,
  styleBar,
  styleLine,
  stylePie,
  buildWaterfallOption,
  CHART_PALETTE,
} from '../src/utils/chart'

describe('chart theming', () => {
  it('follows design tokens with sane fallbacks', () => {
    const t = chartTheme()
    expect(t.accent).toBe('#6366f1')
    expect(t.fontFamily.length).toBeGreaterThan(4)
  })

  it('styles bars with rounded accent gradient', () => {
    const bar = styleBar({ name: 'x', data: [1, 2] }) as {
      type: string
      barMaxWidth: number
      itemStyle: { borderRadius: number[]; color: { type: string } }
    }
    expect(bar.type).toBe('bar')
    expect(bar.itemStyle.borderRadius[0]).toBe(5)
    expect(bar.itemStyle.color).toHaveProperty('type', 'linear')
  })

  it('styles pie slices with the palette', () => {
    const pie = stylePie({ name: 'p', data: [] }) as {
      radius: string[]
      itemStyle: { color: (p: { dataIndex: number }) => string }
    }
    expect(pie.itemStyle.color({ dataIndex: 0 })).toBe(CHART_PALETTE[0])
    expect(pie.itemStyle.color({ dataIndex: 9 })).toBe(
      CHART_PALETTE[9 % CHART_PALETTE.length],
    )
  })

  it('merges themed chrome without clobbering provided axes', () => {
    const opt = applyChartTheme({
      title: { text: 't' },
      xAxis: { type: 'category', data: ['a', 'b'] },
      series: [styleLine({ name: 'y', data: [1, 2] })],
    })
    expect(opt.xAxis).toMatchObject({ type: 'category' })
    expect((opt.xAxis as { axisLabel: { color: string } }).axisLabel.color).toBe(
      '#71717a',
    )
    expect(opt.tooltip).toHaveProperty('backgroundColor')
    expect((opt.series as unknown[])[0]).toHaveProperty('type', 'line')
  })
})

describe('waterfall option', () => {
  const bars = (opt: Record<string, unknown>) => {
    const series = opt.series as {
      name: string
      silent?: boolean
      data: ({ value: number } | number)[]
    }[]
    return { base: series[0], delta: series[1] }
  }
  const val = (d: { value: number } | number) =>
    typeof d === 'number' ? d : d.value

  it('floats signed deltas on a transparent base, absolutes at the ends', () => {
    // 基期 100 → 华东 -20 → 华南 +5 → 当前 85
    const opt = buildWaterfallOption(
      ['基期', '华东', '华南', '当前'],
      [100, -20, 5, 85],
      { delta: 'Δ', base: '基期', current: '当前' },
    )
    const { base, delta } = bars(opt)
    // 透明底座:首末为 0(绝对柱),中间托起 running 的较低端
    // 100 → 80 时托 80;80 → 85 时托 80(min(running, running+v))
    expect(base.silent).toBe(true)
    expect(base.data.map(val)).toEqual([0, 80, 80, 0])
    // 增量条:绝对值原样,负增量取 |v|(浮在底座之上)
    expect(delta.data.map(val)).toEqual([100, 20, 5, 85])
  })

  it('colors mid deltas by sign and keeps tooltip on original values', () => {
    const opt = buildWaterfallOption(
      ['基期', '华东', '华南', '当前'],
      [100, -20, 5, 85],
      { delta: 'Δ', base: '基期', current: '当前' },
    )
    const { delta } = bars(opt)
    const style = (i: number) =>
      (delta.data[i] as { itemStyle: { color: string } }).itemStyle.color
    // 正负不同色(与升/降直觉一致);首末用主色
    expect(style(1)).not.toBe(style(2))
    expect(style(0)).not.toBe(style(1))
    const fmt = (
      (opt.tooltip as { formatter: (p: unknown) => string }).formatter
    )({ dataIndex: 1 })
    expect(fmt).toContain('华东')
    expect(fmt).toContain('-20') // 原始带符号值,不是堆叠后的底座
    expect(fmt).toContain('Δ')
  })

  it('handles a two-category (base→current) waterfall', () => {
    const opt = buildWaterfallOption(['基期', '当前'], [100, 130], {
      delta: 'Δ',
      base: '基期',
      current: '当前',
    })
    const { base, delta } = bars(opt)
    expect(base.data.map(val)).toEqual([0, 0])
    expect(delta.data.map(val)).toEqual([100, 130])
  })
})