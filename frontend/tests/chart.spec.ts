import { describe, it, expect } from 'vitest'
import {
  chartTheme,
  applyChartTheme,
  styleBar,
  styleLine,
  stylePie,
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