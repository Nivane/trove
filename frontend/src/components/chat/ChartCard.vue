<template>
  <div v-if="option" class="chart-card">
    <div class="chart-title">{{ chart?.title || 'Chart' }}</div>
    <div ref="el" class="chart-canvas"></div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import * as echarts from 'echarts'
import type { ChartSpec } from '../../api/types'

const props = defineProps<{
  chart: ChartSpec | null | undefined
  option: Record<string, unknown> | null | undefined
}>()

const el = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

function fallbackOption(spec: ChartSpec): Record<string, unknown> {
  const dim = spec.dimension || ''
  const cats = spec.categories || []
  const series = (spec.series || []).map((s) => ({ name: s.name || 'value', type: 'bar', data: s.data }))
  return {
    title: { text: spec.title || '' },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: cats },
    yAxis: { type: 'value' },
    series,
  }
}

function render() {
  if (!el.value) return
  if (!chart) chart = echarts.init(el.value)
  const opt = (props.option as Record<string, unknown>) || (props.chart ? fallbackOption(props.chart) : null)
  if (opt) chart.setOption(opt)
}

onMounted(async () => {
  await nextTick()
  render()
  window.addEventListener('resize', resize)
})

onUnmounted(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})

function resize() {
  chart?.resize()
}

watch(() => props.option, () => render())
</script>
