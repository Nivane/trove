<!--
  OntologyGraph — 本体页的 ECharts graph 画布（前端方案 §3.3/§3.4）。

  刻意的薄壳：option 与样式全部来自 utils/ontologyChart.ts 的纯函数
  （buildGraphOption / ontologyGraphTokens），组件只做四件事——
  挂载实例、把点击翻译成 select 事件、把选中态转成 downplay/highlight、
  卸载时 dispose。零业务判断留在这一层：
  · 「声明/未声明/fan_out/规模」的判据都在纯函数里（可单测，不挂实例）；
  · 空态与错误态由 OntologyView 决定是否渲染本组件（这里不猜）。
-->
<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import * as echarts from 'echarts'
import type { ObjectLinkInfo, ObjectNodeInfo } from '../../api/types'
import {
  buildGraphOption,
  ontologyGraphTokens,
  type OntologySelection,
} from '../../utils/ontologyChart'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const props = defineProps<{
  objects: ObjectNodeInfo[]
  links: ObjectLinkInfo[]
  selected?: OntologySelection | null
}>()

const emit = defineEmits<{ select: [OntologySelection] }>()

const ui = useUiStore()
const el = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

function optionOf(): Record<string, unknown> {
  return buildGraphOption(props.objects, props.links, ontologyGraphTokens(), {
    // fan_out 的解释文案随语言切换——tooltip 不写死任何一种语言。
    fanoutWarn: t('ontologyFanoutWarn', ui.lang),
  })
}

/** 选中态渲染：全量 downplay 后 highlight 目标（幂等，重入安全）。 */
function paintSelection() {
  if (!chart) return
  chart.dispatchAction({ type: 'downplay', seriesIndex: 0 })
  const sel = props.selected
  if (sel) chart.dispatchAction({ type: 'highlight', seriesIndex: 0, name: sel.ref })
}

function render() {
  if (!el.value) return
  if (chart?.getDom() !== el.value) {
    chart?.dispose()
    chart = null
  }
  if (!chart) {
    chart = echarts.init(el.value)
    chart.on('click', (params: unknown) => {
      // 点击只认数据项自带的 onto 摘要（buildGraphOption 写入）——
      // 组件不从 params 的 ECharts 形状里二次推导业务含义。
      const p = params as { data?: { onto?: { kind?: string; ref?: string } } }
      const onto = p?.data?.onto
      if (onto?.kind !== 'object' && onto?.kind !== 'link') return
      if (typeof onto.ref !== 'string') return
      emit('select', { kind: onto.kind, ref: onto.ref })
    })
  }
  chart.setOption(optionOf(), true)
  paintSelection()
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

watch(
  () => [props.objects, props.links],
  () => render(),
  { deep: false },
)

watch(() => props.selected, () => paintSelection())
</script>

<template>
  <div ref="el" class="onto-canvas" role="img" :aria-label="t('ontology', ui.lang)" />
</template>

<style scoped>
.onto-canvas {
  width: 100%;
  height: 560px;
  min-height: 320px;
}
@media (max-width: 1080px) {
  .onto-canvas {
    height: 420px;
  }
}
</style>
