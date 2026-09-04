<template>
  <div v-if="option || chart" class="chart-card">
    <div class="chart-head">
      <div class="chart-title">{{ titleText }}</div>
      <div class="chart-head-actions">
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
        <button class="chart-download-btn" :title="t('downloadPng', ui.lang)" @click="downloadPng">
          <Download :size="14" />
        </button>
      </div>
    </div>
    <div ref="el" class="chart-canvas" />
    <div v-if="drillChips.length > 2" class="chart-drill">
      <span class="chart-drill-label">{{ t('chartDrill', ui.lang) }}</span>
      <div class="chart-drill-chips">
        <button
          v-for="c in drillChips"
          :key="c"
          class="chart-drill-chip"
          :title="t('chartDrillHint', ui.lang)"
          @click="drillInto(c)"
        >
          {{ c }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, computed, nextTick } from 'vue'
import * as echarts from 'echarts'
import { Download } from 'lucide-vue-next'
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

const emit = defineEmits<{
  /** 分类下钻:生成追问问题(复用现有 follow-up 补全路由)。 */
  ask: [question: string]
}>()

const ui = useUiStore()
const el = ref<HTMLDivElement>()
let chartInstance: echarts.ECharts | null = null

const ALL_TYPES = ['line', 'bar', 'pie'] as const
type ChartType = (typeof ALL_TYPES)[number]

const selected = ref<ChartType>('bar')

/** 分类下钻 chips:展示最多前 6 个分类,点击生成追问。 */
const drillChips = computed(() =>
  (props.chart?.categories ?? []).slice(0, 6),
)

function drillInto(cat: string) {
  // 带原问题上下文组成完整追问:纯「只看X」无指代词/呢吗,会被当成独立
  // 问题;拼上原问题后走普通 query 路由即可命中。
  emit('ask', `只看${cat}的数据，${titleText.value}`)
}

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

/** 导出当前图表为 PNG(基于当前渲染的画布)。 */
function downloadPng() {
  if (!chartInstance) return
  const url = chartInstance.getDataURL({
    type: 'png',
    pixelRatio: 2,
    backgroundColor: '#fff',
  })
  const a = document.createElement('a')
  a.href = url
  a.download = `trove-chart-${Date.now()}.png`
  a.click()
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
</script>
