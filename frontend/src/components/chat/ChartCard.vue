<template>
  <div v-if="option || chart" class="chart-card">
    <div class="chart-head">
      <div class="chart-title">{{ titleText }}</div>
      <div v-if="specs.length > 1" class="chart-type-switch" role="group">
        <button
          v-for="tp in specs"
          :key="tp"
          class="chart-type-btn"
          :class="{ active: selected === tp }"
          @click="selected = tp"
        >
          {{ t(chartTypeKey(tp), ui.lang) }}
        </button>
      </div>
    </div>
    <div ref="el" class="chart-canvas" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, computed, nextTick } from 'vue'
import * as echarts from 'echarts'
import { useUiStore } from '../../stores/ui'
import { t, messages } from '../../i18n'
import {
  chartTheme,
  applyChartTheme,
  styleBar,
  styleLine,
  stylePie,
  CHART_PALETTE,
} from '../../utils/chart'
import type { ChartSpec } from '../../api/types'

type I18nKey = keyof typeof messages.zh

const props = defineProps<{
  chart: ChartSpec | null | undefined
  option: Record<string, unknown> | null | undefined
}>()

const ui = useUiStore()
const el = ref<HTMLDivElement>()
let chartInstance: echarts.ECharts | null = null

const ALL_TYPES = ['line', 'bar', 'pie'] as const
type ChartType = (typeof ALL_TYPES)[number]

const selected = ref<ChartType>('bar')

/** Types the switcher offers for the current payload. */
const specs = computed<ChartType[]>(() => {
  const spec = props.chart
  if (!spec) return []
  const hasPie =
    (spec.series || []).length === 1 && (spec.categories || []).length >= 2
  return hasPie ? [...ALL_TYPES] : (['line', 'bar'] as ChartType[])
})

function chartTypeKey(tp: ChartType): I18nKey {
  return `chart${tp[0].toUpperCase()}${tp.slice(1)}` as I18nKey
}

const titleText = computed(() => {
  if (props.chart?.title) return props.chart.title
  const raw = props.option as { title?: { text?: string } } | undefined
  const rawText = raw?.title && typeof raw.title === 'object' ? raw.title.text : ''
  if (rawText) return rawText
  return t('chart', ui.lang)
})

/** Build a theme-styled ECharts option from the compact spec. */
function buildOption(spec: ChartSpec, type: ChartType) {
  const th = chartTheme()
  const cats = spec.categories || []

  if (type === 'pie') {
    const first = (spec.series || [])[0] || {}
    const values = (first.data || []).map((v) =>
      typeof v === 'number' ? v : Number(v) || 0,
    )
    return applyChartTheme({
      legend: { top: 0 },
      series: [
        stylePie({
          name: spec.dimension || spec.measures?.[0] || 'value',
          data: cats.map((c, i) => ({ name: c, value: values[i] ?? 0 })),
        }),
      ],
    })
  }

  const seriesCount = (spec.series || []).length
  const series = (spec.series || []).map((s, idx) => {
    const name = s.name || spec.measures?.[0] || 'value'
    const color =
      seriesCount > 1 ? CHART_PALETTE[idx % CHART_PALETTE.length] : th.accent
    if (type === 'line') {
      return styleLine({ name, data: s.data }, color)
    }
    return styleBar({ name, data: s.data }, color)
  })
  return applyChartTheme({
    legend: seriesCount > 1 ? { top: 0 } : undefined,
    xAxis: { type: 'category', data: cats },
    yAxis: { type: 'value', name: seriesCount > 1 ? '' : spec.measures?.[0] },
    series,
  })
}

function render() {
  if (!el.value) return
  if (chartInstance?.getDom() !== el.value) {
    chartInstance?.dispose()
    chartInstance = null
  }
  if (!chartInstance) chartInstance = echarts.init(el.value)
  const opt = props.chart
    ? buildOption(props.chart, selected.value)
    : props.option
      // 标题只渲染在卡片头部(chart-title),剥掉 option 自带的 title 防重复
      ? applyChartTheme({ ...props.option, title: undefined })
      : null
  if (opt) chartInstance.setOption(opt, true)
}

onMounted(async () => {
  await nextTick()
  render()
  window.addEventListener('resize', resize)
})

onUnmounted(() => {
  window.removeEventListener('resize', resize)
  chartInstance?.dispose()
  chartInstance = null
})

function resize() {
  chartInstance?.resize()
}

watch(
  () => props.chart,
  () => {
    // new answer → reset to the inferred type
    selected.value = (props.chart?.type as ChartType) || 'bar'
    render()
  },
)

watch(selected, () => render())

// theme toggle (light ↔ dark) → re-read tokens and repaint
watch(
  () => ui.theme,
  () => render(),
)
</script>
