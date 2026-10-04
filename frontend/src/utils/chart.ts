// Chart theming — maps the app's design tokens onto ECharts so charts feel
// native to the UI (light & dark) instead of ECharts' out-of-the-box look.

export interface ChartTheme {
  fontFamily: string
  accent: string
  text: string
  secondary: string
  tertiary: string
  border: string
  raised: string
  muted: string
}

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim()
  return v || fallback
}

/** 同 cssVar，但把「13px」解析成 13 —— ECharts 的 fontSize 只吃数字。 */
function cssVarPx(name: string, fallback: number): number {
  const n = Number.parseFloat(cssVar(name, ''))
  return Number.isFinite(n) ? n : fallback
}

/** A secondary palette for multi-series / pie slices (readable on both themes). */
export const CHART_PALETTE = [
  '#818cf8', // indigo
  '#2dd4bf', // teal
  '#f59e0b', // amber
  '#f472b6', // pink
  '#38bdf8', // sky
  '#a78bfa', // violet
]

/** Read current design tokens (auto-follows the light/dark theme). */
export function chartTheme(): ChartTheme {
  return {
    fontFamily: cssVar('--font-sans', 'ui-sans-serif, system-ui, sans-serif'),
    accent: cssVar('--accent', '#6366f1'),
    text: cssVar('--text-primary', '#18181b'),
    secondary: cssVar('--text-secondary', '#71717a'),
    tertiary: cssVar('--text-tertiary', '#a1a1aa'),
    border: cssVar('--border-subtle', '#e4e4e7'),
    raised: cssVar('--surface-raised', '#ffffff'),
    muted: cssVar('--surface-muted', '#f4f4f5'),
  }
}

export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full =
    h.length === 3
      ? h
          .split('')
          .map((c) => c + c)
          .join('')
      : h
  const num = parseInt(full, 16)
  if (Number.isNaN(num)) return hex
  const r = (num >> 16) & 255
  const g = (num >> 8) & 255
  const b = num & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** Vertical gradient of `color` fading out — for bars and line areas. */
export function fadeGradient(hex: string, from = 0.9, to = 0.08) {
  return {
    type: 'linear' as const,
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    colorStops: [
      { offset: 0, color: withAlpha(hex, from) },
      { offset: 1, color: withAlpha(hex, to) },
    ],
  }
}

/** Shared "chrome": axes, tooltip, legend, grid, typography for any chart. */
export function chartChrome(base: Partial<Record<string, unknown>> = {}) {
  const t = chartTheme()
  return {
    animationDuration: 450,
    animationEasing: 'cubicOut' as const,
    textStyle: { fontFamily: t.fontFamily },
    tooltip: {
      backgroundColor: t.raised,
      borderColor: t.border,
      borderWidth: 1,
      padding: [10, 12],
      textStyle: { color: t.text, fontSize: cssVarPx('--fs-xs', 13) },
      extraCssText:
        'border-radius:10px;box-shadow:0 10px 25px rgba(0,0,0,.12);',
    },
    // containLabel:把轴标签算进 grid 的布局 —— 否则 8 位数的标签
    // (30,000,000) 会被 left:40 的画布边界裁掉左边,只剩「0,000」。
    grid: { left: 8, right: 16, top: 36, bottom: 32, containLabel: true },
    // keep whatever the caller already set for these keys
    ...base,
  }
}

/** Apply themed chrome + palette to an option, preserving provided details. */
export function applyChartTheme(
  opt: Record<string, unknown>,
  base: Record<string, unknown> = {},
): Record<string, unknown> {
  const t = chartTheme()
  const chrome = chartChrome(base)
  const merged: Record<string, unknown> = {
    backgroundColor: 'transparent',
    color: CHART_PALETTE,
    ...chrome,
    ...opt,
    textStyle: { fontFamily: t.fontFamily, ...(opt.textStyle as object) },
    tooltip: { ...(chrome.tooltip as object), ...(opt.tooltip as object) },
  }

  // hide the base axis chrome unless the caller brought its own
  const axisChrome = {
    axisLine: { lineStyle: { color: t.border } },
    axisTick: { show: false },
    axisLabel: {
      color: t.secondary,
      fontSize: cssVarPx('--fs-2xs', 12),
      fontFamily: t.fontFamily,
    },
    splitLine: { lineStyle: { color: t.border, opacity: 0.6 } },
  }
  const mergeAxis = (axis: unknown) =>
    axis
      ? {
          ...axisChrome,
          ...(axis as object),
          axisLabel: {
            ...(axisChrome.axisLabel as object),
            ...((axis as { axisLabel?: object }).axisLabel ?? {}),
          },
        }
      : { ...axisChrome, type: 'category' }
  merged.xAxis = mergeAxis(opt.xAxis)
  merged.yAxis = mergeAxis(opt.yAxis)

  // title top-left, legend top-right — they never collide
  if (opt.title) {
    merged.title = {
      ...(opt.title as object),
      left: 0,
      top: 0,
      textStyle: {
        fontSize: cssVarPx('--fs-sm', 14),
        fontWeight: 600,
        color: t.text,
        fontFamily: t.fontFamily,
        ...((opt.title as { textStyle?: object }).textStyle ?? {}),
      },
    }
  }
  if (opt.legend) {
    merged.legend = {
      ...(opt.legend as object),
      right: 0,
      top: 0,
      textStyle: {
        color: t.secondary,
        fontSize: cssVarPx('--fs-2xs', 12),
        fontFamily: t.fontFamily,
      },
      icon: 'roundRect',
      itemWidth: 12,
      itemHeight: 4,
      itemGap: 20,
    }
  }
  return merged
}

/**
 * Waterfall option from an attribution spec: 首/末为绝对柱(基期/当前),
 * 中间为带符号增量。经典 ECharts 瀑布技法 —— 一条透明底座托起可见的
 * 增量条,正负用语义色区分(与升/降的直觉一致)。
 */
export function buildWaterfallOption(
  categories: string[],
  data: number[],
  labels: { delta: string; base: string; current: string },
): Record<string, unknown> {
  const t = chartTheme()
  const up = cssVar('--ok', '#15803d')
  const down = cssVar('--danger', '#dc2626')
  const n = data.length
  const base: number[] = []
  const bars: Record<string, unknown>[] = []
  let running = 0
  data.forEach((v, i) => {
    if (i === 0 || i === n - 1) {
      base.push(0)
      bars.push({
        value: v,
        itemStyle: { color: withAlpha(t.accent, 0.9), borderRadius: [5, 5, 0, 0] },
      })
      if (i === 0) running = v
      return
    }
    base.push(Math.min(running, running + v))
    bars.push({
      value: Math.abs(v),
      itemStyle: {
        color: withAlpha(v >= 0 ? up : down, 0.88),
        borderRadius: v >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4],
      },
    })
    running += v
  })
  return applyChartTheme({
    tooltip: {
      // 悬浮显示**原始带符号值**(不是堆叠后的两段),基期/当前标各自的名字
      formatter: (params: unknown) => {
        const p = (Array.isArray(params) ? params[0] : params) as
          | { dataIndex?: number }
          | undefined
        const i = p?.dataIndex ?? 0
        const name = i === 0 ? labels.base : i === n - 1 ? labels.current : labels.delta
        return `${categories[i]}<br/>${name}: ${data[i]}`
      },
    },
    legend: { show: false },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value' },
    series: [
      {
        name: 'wf-base',
        type: 'bar',
        stack: 'wf',
        silent: true,
        itemStyle: { color: 'transparent' },
        emphasis: { disabled: true },
        tooltip: { show: false },
        data: base,
      },
      {
        name: labels.delta,
        type: 'bar',
        stack: 'wf',
        barMaxWidth: 32,
        data: bars,
      },
    ],
  })
}

/** Series-level styling per chart type (built on top of a compact spec). */
export function styleBar(
  series: Record<string, unknown>,
  color = chartTheme().accent,
) {
  return {
    ...series,
    type: 'bar',
    barMaxWidth: 32,
    itemStyle: {
      borderRadius: [5, 5, 2, 2],
      color: fadeGradient(color, 1, 0.55),
    },
    emphasis: { itemStyle: { color } },
  }
}

export function styleLine(
  series: Record<string, unknown>,
  color = chartTheme().accent,
) {
  return {
    ...series,
    type: 'line',
    smooth: 0.35,
    symbol: 'none',
    lineStyle: { width: 2.5, color },
    itemStyle: { color, borderColor: chartTheme().raised, borderWidth: 2 },
    areaStyle: { color: fadeGradient(color, 0.32, 0.02) },
    emphasis: { focus: 'series' },
  }
}

export function stylePie(series: Record<string, unknown>) {
  const t = chartTheme()
  return {
    ...series,
    type: 'pie',
    radius: ['42%', '70%'],
    padAngle: 2,
    itemStyle: {
      borderRadius: 6,
      borderColor: t.raised,
      borderWidth: 3,
      color: (params: { dataIndex: number }) =>
        CHART_PALETTE[params.dataIndex % CHART_PALETTE.length],
    },
    label: {
      color: t.secondary,
      fontFamily: t.fontFamily,
      formatter: '{b} {d}%',
    },
    emphasis: {
      scaleSize: 6,
      itemStyle: { shadowBlur: 12, shadowColor: 'rgba(0,0,0,.18)' },
    },
    labelLine: { lineStyle: { color: t.border } },
  }
}
