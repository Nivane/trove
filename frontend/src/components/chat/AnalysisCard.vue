<!--
  分析卡(分析柱 P1):归因分析的**结构化**视图 —— 瀑布图 / 维度贡献表 /
  比率三效应 / 驱动器树 / 证据抽屉。

  与回答 markdown 里的分析区块同源(后端 attribution 节点双写:
  state.attribution 供 markdown,state.analysis 供卡片)。四条纪律:
  1. 拿不到就不渲染 —— 每节按缺席整节跳过,不编数;
  2. 说话算数 —— 树节点残差不精确/组件缺失如实标注(宁可不拆,不造恒等式);
  3. 降级必须说 —— partial 时挂提示条并把 degraded 原因列进证据;
  4. 只读展示 —— 证据抽屉只贴已落盘的 SQL 与样例行,不重跑查询。
-->
<script setup lang="ts">
import { computed } from 'vue'
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import { t } from '../../i18n'
import SqlBlock from './SqlBlock.vue'
import { useUiStore } from '../../stores/ui'
import { buildWaterfallOption } from '../../utils/chart'
import { fmtVal } from '../../utils/format'
import type {
  AnalysisContributionRow,
  AnalysisPayload,
  AnalysisQueryEvidence,
  AnalysisTreeNode,
} from '../../api/types'

const props = defineProps<{ analysis: AnalysisPayload | null | undefined }>()

const ui = useUiStore()
const a = computed(() => props.analysis ?? null)

const kindLabel = computed(() => {
  const kind = a.value?.kind
  if (kind === 'combined') return t('anaKindCombined', ui.lang)
  if (kind === 'driver_tree') return t('anaKindTree', ui.lang)
  return t('anaKindAttribution', ui.lang)
})

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** 数值格式化:大数走 fmtVal(与结果表格同一套),缺失 → "—"(不编 0)。 */
function fv(v: unknown): string {
  return isNum(v) ? fmtVal(v) : '—'
}

function fpct(v: unknown): string {
  return isNum(v) ? `${(v * 100).toFixed(1)}%` : '—'
}

function fsign(v: unknown): string {
  return isNum(v) ? `${v > 0 ? '+' : ''}${fmtVal(v)}` : '—'
}

/** 变化量着色:涨/跌用语义色(与瀑布同色系)。 */
function deltaClass(v: unknown): string {
  if (!isNum(v) || v === 0) return ''
  return v > 0 ? 'up' : 'down'
}

const isRatio = computed(() => a.value?.metric_kind === 'ratio')
const rows = computed<AnalysisContributionRow[]>(() => a.value?.table ?? [])

type I18nKey = Parameters<typeof t>[0]

/** 比率三效应(within/composition/interaction):缺哪项就不显示哪项。 */
const effectItems = computed<{ label: I18nKey; value: number }[]>(() => {
  const e = a.value?.effects
  if (!e) return []
  const defs: [string, I18nKey][] = [
    ['within', 'anaWithin'],
    ['composition', 'anaComposition'],
    ['interaction', 'anaInteraction'],
  ]
  return defs
    .filter(([k]) => isNum(e[k]))
    .map(([k, label]) => ({ label, value: e[k] }))
})
const drill = computed(() => a.value?.drilldown ?? null)
const drillRows = computed<AnalysisContributionRow[]>(() => drill.value?.table ?? [])
const evidence = computed(() => a.value?.evidence ?? null)
const queries = computed<AnalysisQueryEvidence[]>(() => evidence.value?.queries ?? [])
const degraded = computed(() => evidence.value?.degraded ?? [])

/** 驱动器树 → 缩进行(先序);单叶树(没得拆)不渲染。 */
const treeRows = computed<{ depth: number; node: AnalysisTreeNode }[]>(() => {
  const root = a.value?.tree
  if (!root || !(root.children ?? []).length) return []
  const out: { depth: number; node: AnalysisTreeNode }[] = []
  const walk = (n: AnalysisTreeNode, d: number) => {
    out.push({ depth: d, node: n })
    for (const c of n.children ?? []) walk(c, d + 1)
  }
  walk(root, 0)
  return out
})

function treeNote(n: AnalysisTreeNode): string {
  if (!n.executed) return n.note || t('anaNoValue', ui.lang)
  const reason = n.residual?.reason || ''
  if (reason === 'identity') return t('anaIdentity', ui.lang)
  if (reason === 'gap') return `${t('anaResidual', ui.lang)} ${fv(n.residual?.value)}`
  if (reason === 'component_unavailable') return t('anaComponentMissing', ui.lang)
  if (reason === 'non_decomposable') return t('anaNonDecomposable', ui.lang)
  if (n.informational) return t('anaInformational', ui.lang)
  return n.note || ''
}

const purposeLabels: Record<string, string> = {
  overall: 'anaPurposeOverall',
  probe: 'anaPurposeProbe',
  drilldown: 'anaPurposeDrilldown',
  driver_tree: 'anaPurposeTree',
}

function purposeLabel(p?: string): string {
  const key = (p && purposeLabels[p]) || ''
  return key ? t(key as Parameters<typeof t>[0], ui.lang) : p || ''
}

const showEvidence = ref(false)

/* ── 瀑布图(ECharts;服务端出 spec,前端按主题建 option)────────── */
const chartEl = ref<HTMLDivElement>()
let chartInstance: echarts.ECharts | null = null

const waterfallSpec = computed(() => {
  const charts = a.value?.charts ?? []
  return charts.find((c) => c.type === 'waterfall') ?? null
})

function renderChart() {
  const spec = waterfallSpec.value
  if (!el0() || !spec) return
  if (!chartInstance) chartInstance = echarts.init(el0()!)
  const data = (spec.series?.[0]?.data ?? []).map((v) =>
    typeof v === 'number' ? v : Number(v) || 0,
  )
  chartInstance.setOption(
    buildWaterfallOption(spec.categories ?? [], data, {
      delta: t('anaDelta', ui.lang),
      base: t('anaBase', ui.lang),
      current: t('anaCurrent', ui.lang),
    }),
    true,
  )
}

function el0(): HTMLDivElement | undefined {
  return chartEl.value
}

onMounted(async () => {
  await nextTick()
  renderChart()
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chartInstance?.dispose()
  chartInstance = null
})

watch(waterfallSpec, async () => {
  await nextTick()
  renderChart()
})

function resize() {
  chartInstance?.resize()
}
</script>

<template>
  <section v-if="a" class="ana-card">
    <header class="ana-head">
      <span class="ana-title">{{ t('anaCardTitle', ui.lang) }}</span>
      <span v-if="a.metric" class="ana-chip">{{ a.metric }}</span>
      <span class="ana-chip subtle">{{ kindLabel }}</span>
      <span v-if="a.labels?.baseline_label" class="ana-chip subtle">
        {{ a.labels.baseline_label }}
      </span>
      <span v-if="a.partial" class="ana-chip warn" :title="t('anaPartialHint', ui.lang)">
        {{ t('anaPartial', ui.lang) }}
      </span>
    </header>

    <!-- 头条:总变化(带符号着色;比率指标标加权率变化) -->
    <div v-if="isNum(a.total_delta)" class="ana-headline">
      <span class="ana-headline-k">{{ t('anaTotalDelta', ui.lang) }}</span>
      <span class="ana-headline-v" :class="deltaClass(a.total_delta)">
        {{ fsign(a.total_delta) }}
      </span>
      <span v-if="a.labels?.primary_dimension" class="ana-headline-dim">
        {{ t('anaPrimaryDim', ui.lang) }}: {{ a.labels.primary_dimension }}
      </span>
    </div>

    <!-- 瀑布图 -->
    <div v-if="waterfallSpec" ref="chartEl" class="ana-chart" />

    <!-- 比率三效应 -->
    <div v-if="isRatio && effectItems.length" class="ana-effects">
      <span v-for="item in effectItems" :key="item.label" class="ana-effect">
        <span class="ana-effect-k">{{ t(item.label, ui.lang) }}</span>
        <span class="ana-effect-v" :class="deltaClass(item.value)">{{ fsign(item.value) }}</span>
      </span>
    </div>

    <!-- 维度贡献表 -->
    <table v-if="rows.length" class="ana-table">
      <thead>
        <tr v-if="isRatio">
          <th>{{ t('anaDim', ui.lang) }}</th>
          <th>{{ t('anaBaseRate', ui.lang) }}</th>
          <th>{{ t('anaCurrentRate', ui.lang) }}</th>
          <th>{{ t('anaBaseWeight', ui.lang) }}</th>
          <th>{{ t('anaCurrentWeight', ui.lang) }}</th>
          <th>{{ t('anaDelta', ui.lang) }}</th>
          <th>{{ t('anaContribution', ui.lang) }}</th>
        </tr>
        <tr v-else>
          <th>{{ t('anaDim', ui.lang) }}</th>
          <th>{{ t('anaBase', ui.lang) }}</th>
          <th>{{ t('anaCurrent', ui.lang) }}</th>
          <th>{{ t('anaDelta', ui.lang) }}</th>
          <th>{{ t('anaContribution', ui.lang) }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(r, i) in rows" :key="i">
          <td class="ana-dim">{{ r.dim ?? '' }}</td>
          <template v-if="isRatio">
            <td>{{ fv(r.base_rate) }}</td>
            <td>{{ fv(r.current_rate) }}</td>
            <td>{{ fpct(r.base_weight) }}</td>
            <td>{{ fpct(r.current_weight) }}</td>
            <td :class="deltaClass(r.delta)">{{ fsign(r.delta) }}</td>
            <td>{{ fv(r.contribution) }}</td>
          </template>
          <template v-else>
            <td>{{ fv(r.base) }}</td>
            <td>{{ fv(r.current) }}</td>
            <td :class="deltaClass(r.delta)">{{ fsign(r.delta) }}</td>
            <td>{{ fpct(r.contribution) }}</td>
          </template>
        </tr>
      </tbody>
    </table>

    <!-- 驱动器树 -->
    <div v-if="treeRows.length" class="ana-tree">
      <div class="ana-subtitle">{{ t('anaTreeTitle', ui.lang) }}</div>
      <table class="ana-table">
        <thead>
          <tr>
            <th>{{ t('anaComponent', ui.lang) }}</th>
            <th>{{ t('anaBase', ui.lang) }}</th>
            <th>{{ t('anaCurrent', ui.lang) }}</th>
            <th>{{ t('anaDelta', ui.lang) }}</th>
            <th>{{ t('anaNote', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in treeRows" :key="i" :class="{ 'tree-root': row.depth === 0 }">
            <td class="ana-dim" :style="{ paddingLeft: `${8 + row.depth * 16}px` }">
              {{ row.node.name ?? '' }}
            </td>
            <td>{{ fv(row.node.base) }}</td>
            <td>{{ fv(row.node.current) }}</td>
            <td :class="deltaClass(row.node.delta)">{{ fsign(row.node.delta) }}</td>
            <td class="ana-note">{{ treeNote(row.node) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 下钻(第二维) -->
    <div v-if="drillRows.length" class="ana-drill">
      <div class="ana-subtitle">
        {{ t('anaDrilldown', ui.lang) }}<template v-if="drill?.dimension"> · {{ drill.dimension }}</template>
      </div>
      <table class="ana-table">
        <thead>
          <tr>
            <th>{{ t('anaDim', ui.lang) }}</th>
            <th>{{ t('anaBase', ui.lang) }}</th>
            <th>{{ t('anaCurrent', ui.lang) }}</th>
            <th>{{ t('anaDelta', ui.lang) }}</th>
            <th>{{ t('anaContribution', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in drillRows" :key="i">
            <td class="ana-dim">{{ r.dim ?? '' }}</td>
            <td>{{ fv(r.base) }}</td>
            <td>{{ fv(r.current) }}</td>
            <td :class="deltaClass(r.delta)">{{ fsign(r.delta) }}</td>
            <td>{{ fpct(r.contribution) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 证据抽屉:每条查询贴 SQL + 样例行(已落盘,只读) -->
    <div v-if="queries.length || degraded.length" class="ana-evidence">
      <button class="ana-evidence-toggle" @click="showEvidence = !showEvidence">
        {{ showEvidence ? '▾' : '▸' }} {{ t('anaEvidence', ui.lang) }}
        <span class="ana-chip subtle">{{ t('anaQueries', ui.lang, queries.length) }}</span>
      </button>
      <div v-if="showEvidence" class="ana-evidence-body">
        <div v-if="degraded.length" class="ana-degraded">
          <div v-for="(d, i) in degraded" :key="i" class="ana-degraded-row">
            <span class="ana-chip warn">{{ d.stage }}</span>
            <span>{{ d.reason }}</span>
          </div>
        </div>
        <div v-for="q in queries" :key="q.id ?? 0" class="ana-query">
          <div class="ana-query-head">
            <span class="ana-chip subtle">{{ purposeLabel(q.purpose) }}</span>
            <span v-if="q.period" class="ana-chip subtle">{{ q.period }}</span>
            <span v-if="q.filter" class="ana-chip subtle">{{ q.filter }}</span>
            <span v-if="q.row_count != null" class="ana-chip subtle">
              {{ q.row_count }} {{ t('anaRows', ui.lang) }}
              <template v-if="q.truncated">· {{ t('anaTruncated', ui.lang) }}</template>
            </span>
          </div>
          <SqlBlock v-if="q.sql" :code="q.sql" />
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ana-card {
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  background: var(--surface-raised);
  padding: 14px 16px;
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ana-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ana-title {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
}
.ana-chip {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--surface-muted);
  color: var(--text-secondary);
  white-space: nowrap;
}
.ana-chip.subtle {
  background: transparent;
  border: 1px solid var(--border-subtle);
}
.ana-chip.warn {
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  color: var(--danger);
}
.ana-headline {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  color: var(--text-secondary);
}
.ana-headline-v {
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
}
.ana-headline-dim {
  margin-left: auto;
  font-size: 12px;
}
.up {
  color: var(--ok);
}
.down {
  color: var(--danger);
}
.ana-chart {
  width: 100%;
  height: 240px;
}
.ana-effects {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  font-size: 13px;
}
.ana-effect-k {
  color: var(--text-secondary);
  margin-right: 6px;
}
.ana-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.ana-table th {
  text-align: left;
  font-weight: 500;
  color: var(--text-tertiary);
  font-size: 12px;
  padding: 4px 8px;
  border-bottom: 1px solid var(--border-subtle);
}
.ana-table td {
  padding: 4px 8px;
  border-bottom: 1px solid var(--border-subtle);
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}
.tree-root td {
  font-weight: 600;
}
.ana-note {
  color: var(--text-tertiary);
  font-size: 12px;
}
.ana-subtitle {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}
.ana-evidence-toggle {
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
}
.ana-evidence-body {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ana-degraded-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}
.ana-query-head {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
</style>
