<!--
  VerdictHistoryDrawer — 一条决策规则的判定历史(只读审计线)。

  判定是不可编辑的事实记录,所以这里没有任何写操作;抽屉只回答三个问题:
  最近几次判了什么(状态/消息)、相邻两次之间变了什么(diff)、这次判定是
  在哪些数字上做的(证据:SQL + 判定行 + 分析摘要)。

  两条渲染纪律:
  · ``diff: null`` 是窗口边界(该条前面没有可比的一条),渲染成"窗口边界"
    而不是"没有变化" —— 把没得比读成平静正是审计视图最危险的错觉;
  · ``analysis.degraded`` 必须显示:桥没做成的那一步写在这里,静默的附录
    与"没什么可分析的"无从区分。
-->
<script setup lang="ts">
import { ref, watch } from 'vue'
import { ChevronDown, ChevronRight } from 'lucide-vue-next'
import DetailDrawer from '../base/DetailDrawer.vue'
import StatePanel from '../base/StatePanel.vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { fmtDateTime, fmtVal } from '../../utils/format'
import {
  fetchVerdict,
  fetchVerdicts,
  verdictStatusClass,
  type VerdictBrief,
  type VerdictDetail,
} from '../../api/decisions'

const props = defineProps<{
  modelValue: boolean
  datasource: string
  ruleId: string
  ruleName?: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const ui = useUiStore()
const verdicts = ref<VerdictBrief[]>([])
const loading = ref(false)
const error = ref('')
const expandedId = ref<number | null>(null)
const details = ref<Record<number, VerdictDetail>>({})
const detailError = ref('')

async function load() {
  if (!props.datasource || !props.ruleId) return
  loading.value = true
  error.value = ''
  expandedId.value = null
  details.value = {}
  try {
    const body = await fetchVerdicts(props.datasource, props.ruleId, { limit: 20 })
    verdicts.value = body.verdicts ?? []
  } catch (e) {
    verdicts.value = []
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function toggle(v: VerdictBrief) {
  detailError.value = ''
  if (expandedId.value === v.id) {
    expandedId.value = null
    return
  }
  expandedId.value = v.id
  if (details.value[v.id]) return
  try {
    details.value = { ...details.value, [v.id]: await fetchVerdict(v.id) }
  } catch (e) {
    detailError.value = e instanceof Error ? e.message : String(e)
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) void load()
  },
  { immediate: true },
)

/** 分组变化类型 → i18n 键(闭集,未知类型原样显示,不吞)。 */
const GROUP_LABELS: Record<string, string> = {
  fired: 'decisionsDiffGroupFired',
  cleared: 'decisionsDiffGroupCleared',
  jump: 'decisionsDiffGroupJump',
  appeared: 'decisionsDiffGroupAppeared',
  vanished: 'decisionsDiffGroupVanished',
}

/** diff 的一句话摘要(触发器/规则变更/分组变化)。 */
function diffSummary(d: VerdictDetail['diff']): string {
  if (!d) return t('decisionsDiffBoundary', ui.lang)
  const bits: string[] = []
  if (d.rule_digest_changed) bits.push(t('decisionsDiffDigest', ui.lang))
  if (d.trigger === 'fired') bits.push(t('decisionsDiffFired', ui.lang))
  if (d.trigger === 'cleared') bits.push(t('decisionsDiffCleared', ui.lang))
  for (const g of d.groups) {
    // 无维度规则(唯一分组的 dim 是空串)的 fired/cleared 与上面的规则级
    // 触发位说的是同一件事(规则级 triggered = 任一分组触发)——不再叠加,
    // 也避免留下一个悬空的「 · 」;幅度突变没有规则级对应位,照常单独写出。
    if (!g.dim && (g.change === 'fired' || g.change === 'cleared')) continue
    const key = GROUP_LABELS[g.change]
    const label = key ? t(key as never, ui.lang) : g.change
    bits.push(g.dim ? `${g.dim} · ${label}` : label)
  }
  return bits.length ? bits.join(' · ') : t('decisionsDiffNone', ui.lang)
}

function evidenceOf(v: VerdictDetail): Record<string, unknown> {
  return (v.verdict.evidence ?? {}) as Record<string, unknown>
}

function sqlOf(ev: Record<string, unknown>, key: string): string {
  const inner = (ev.evidence ?? {}) as Record<string, unknown>
  return String(inner[key] ?? '')
}

function rowsOf(ev: Record<string, unknown>): unknown[][] {
  const inner = (ev.evidence ?? {}) as Record<string, unknown>
  const rows = inner.rows
  return Array.isArray(rows) ? (rows as unknown[][]) : []
}

function columnsOf(ev: Record<string, unknown>): string[] {
  const inner = (ev.evidence ?? {}) as Record<string, unknown>
  const cols = inner.columns
  return Array.isArray(cols) ? (cols as string[]) : []
}

function judgedRowsOf(ev: Record<string, unknown>): Record<string, unknown>[] {
  const rows = ev.rows
  return Array.isArray(rows) ? (rows as Record<string, unknown>[]) : []
}

function analysisOf(ev: Record<string, unknown>): Record<string, unknown> | null {
  const a = ev.analysis
  return a && typeof a === 'object' ? (a as Record<string, unknown>) : null
}

function topComponents(a: Record<string, unknown>): Record<string, unknown>[] {
  const c = a.top_components
  return Array.isArray(c) ? (c as Record<string, unknown>[]) : []
}

function degradedOf(a: Record<string, unknown>): Record<string, unknown>[] {
  const d = a.degraded
  return Array.isArray(d) ? (d as Record<string, unknown>[]) : []
}

function pct(v: unknown): string {
  const n = typeof v === 'number' ? v : null
  return n === null ? '—' : `${(n * 100).toFixed(1)}%`
}
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    :title="t('decisionsHistory', ui.lang)"
    :close-label="t('close', ui.lang)"
    width="760px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="vh-head">
      <span class="cell-mono">{{ ruleId }}</span>
      <span v-if="ruleName" class="dim">{{ ruleName }}</span>
    </div>

    <StatePanel
      v-if="loading"
      mode="loading"
      :title="t('decisionsHistoryLoading', ui.lang)"
    />
    <StatePanel
      v-else-if="error"
      mode="error"
      :title="t('decisionsHistoryError', ui.lang)"
      :detail="error"
      :retry-text="t('retry', ui.lang)"
      @retry="load"
    />
    <StatePanel
      v-else-if="!verdicts.length"
      mode="empty"
      :title="t('decisionsHistoryEmpty', ui.lang)"
      :description="t('decisionsHistoryEmptyHint', ui.lang)"
    />

    <ol v-else class="vh-list">
      <li v-for="v in verdicts" :key="v.id" class="vh-item">
        <button
          type="button"
          class="vh-row"
          :aria-expanded="expandedId === v.id"
          @click="toggle(v)"
        >
          <component
            :is="expandedId === v.id ? ChevronDown : ChevronRight"
            :size="14"
            class="vh-caret"
          />
          <span class="cell-mono vh-time">{{ fmtDateTime(v.evaluated_at) }}</span>
          <span class="pill" :class="verdictStatusClass(v.status)">{{ v.status }}</span>
          <span class="vh-msg">{{ v.message || v.error || '—' }}</span>
        </button>

        <div class="vh-meta">
          <span class="vh-chip" :class="v.diff ? 'vh-chip-neutral' : 'vh-chip-boundary'">
            {{ diffSummary(v.diff) }}
          </span>
          <span v-if="v.error" class="vh-chip vh-chip-danger">{{ v.error }}</span>
          <span class="dim vh-count">
            {{ t('decisionsRowCount', ui.lang, v.row_count) }}
          </span>
        </div>

        <div v-if="expandedId === v.id" class="vh-detail">
          <p v-if="detailError" class="vh-detail-error">{{ detailError }}</p>
          <template v-if="details[v.id]">
            <div class="vh-sec">
              <div class="vh-sec-title">{{ t('decisionsEvidence', ui.lang) }}</div>
              <pre class="cell-mono vh-sql">{{ sqlOf(evidenceOf(details[v.id]), 'sql_current') }}</pre>
              <pre
                v-if="sqlOf(evidenceOf(details[v.id]), 'sql_baseline')"
                class="cell-mono vh-sql"
              >{{ sqlOf(evidenceOf(details[v.id]), 'sql_baseline') }}</pre>
            </div>

            <div v-if="judgedRowsOf(evidenceOf(details[v.id])).length" class="vh-sec">
              <div class="vh-sec-title">{{ t('decisionsJudgedRows', ui.lang) }}</div>
              <div class="vh-table-wrap">
                <table class="vh-table cell-mono">
                  <thead>
                    <tr>
                      <th v-for="(c, i) in columnsOf(evidenceOf(details[v.id]))" :key="i">
                        {{ c }}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(r, i) in rowsOf(evidenceOf(details[v.id]))"
                      :key="i"
                    >
                      <td v-for="(cell, j) in r" :key="j">{{ fmtVal(cell) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div class="vh-sec">
              <div class="vh-sec-title">{{ t('decisionsJudgedCards', ui.lang) }}</div>
              <div class="vh-table-wrap">
                <table class="vh-table cell-mono">
                  <thead>
                    <tr>
                      <th>{{ t('decisionsColDim', ui.lang) }}</th>
                      <th>{{ t('decisionsColCurrent', ui.lang) }}</th>
                      <th>{{ t('decisionsColBaseline', ui.lang) }}</th>
                      <th>Δ</th>
                      <th>Δ%</th>
                      <th>{{ t('decisionsColTriggered', ui.lang) }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(card, i) in judgedRowsOf(evidenceOf(details[v.id]))"
                      :key="i"
                      :class="{ 'vh-fired': card.triggered }"
                    >
                      <td>{{ fmtVal(card.dim) }}</td>
                      <td>{{ fmtVal(card.current) }}</td>
                      <td>{{ fmtVal(card.baseline) }}</td>
                      <td>{{ fmtVal(card.delta) }}</td>
                      <td>{{ pct(card.delta_pct) }}</td>
                      <td>{{ card.triggered ? t('decisionsTriggered', ui.lang) : '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div v-if="analysisOf(evidenceOf(details[v.id]))" class="vh-sec">
              <div class="vh-sec-title">{{ t('decisionsAnalysis', ui.lang) }}</div>
              <ul class="vh-comps">
                <li
                  v-for="(c, i) in topComponents(analysisOf(evidenceOf(details[v.id]))!)"
                  :key="i"
                >
                  <span class="cell-mono">{{ c.dim }}={{ c.value }}</span>
                  <span class="dim"> · {{ fmtVal(c.delta) }} · {{ pct(c.contribution) }}</span>
                </li>
              </ul>
              <p class="dim vh-residual">
                {{ t('decisionsResidual', ui.lang) }}:
                {{ fmtVal((analysisOf(evidenceOf(details[v.id]))!.residual as Record<string, unknown>)?.value) }}
                ({{ (analysisOf(evidenceOf(details[v.id]))!.residual as Record<string, unknown>)?.reason }})
              </p>
              <ul
                v-if="degradedOf(analysisOf(evidenceOf(details[v.id]))!).length"
                class="vh-degraded"
              >
                <li
                  v-for="(d, i) in degradedOf(analysisOf(evidenceOf(details[v.id]))!)"
                  :key="i"
                  class="cell-mono"
                >
                  {{ d.stage }}: {{ d.reason }}
                </li>
              </ul>
            </div>
          </template>
        </div>
      </li>
    </ol>
  </DetailDrawer>
</template>

<style scoped>
.vh-head {
  display: flex;
  gap: var(--sp-2);
  align-items: baseline;
  padding: 0 0 var(--sp-3);
}
.vh-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin: 0;
  padding: 0;
  list-style: none;
}
.vh-item {
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface-raised);
}
.vh-row {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  font: inherit;
  color: inherit;
  text-align: left;
  background: none;
  border: 0;
  cursor: pointer;
}
.vh-row:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: -2px;
}
.vh-caret {
  flex: none;
  color: var(--text-tertiary);
}
.vh-time {
  flex: none;
  font-size: 12px;
}
.vh-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vh-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  padding: 0 var(--sp-3) var(--sp-2) calc(var(--sp-3) + 22px);
}
.vh-chip {
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 4px;
}
.vh-chip-neutral {
  color: var(--text-secondary);
  background: var(--fill-subtle, rgba(128, 128, 128, 0.12));
}
.vh-chip-boundary {
  color: var(--text-tertiary);
  background: transparent;
  border: 1px dashed var(--border-subtle);
}
.vh-chip-danger {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.vh-count {
  font-size: 11px;
}
.vh-detail {
  padding: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
}
.vh-detail-error {
  margin: 0 0 var(--sp-2);
  font-size: 12px;
  color: var(--el-color-danger);
}
.vh-sec + .vh-sec {
  margin-top: var(--sp-3);
}
.vh-sec-title {
  margin-bottom: 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.vh-sql {
  margin: 0 0 4px;
  padding: var(--sp-2);
  font-size: 12px;
  overflow-x: auto;
  background: var(--fill-subtle, rgba(128, 128, 128, 0.08));
  border-radius: 4px;
  white-space: pre-wrap;
}
.vh-table-wrap {
  overflow-x: auto;
}
.vh-table {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;
}
.vh-table th,
.vh-table td {
  padding: 3px 8px;
  text-align: left;
  white-space: nowrap;
  border-bottom: 1px solid var(--border-subtle);
}
.vh-fired td {
  color: var(--el-color-warning);
}
.vh-comps,
.vh-degraded {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
}
.vh-degraded {
  color: var(--el-color-warning);
}
.vh-residual {
  margin: 4px 0 0;
  font-size: 12px;
}
</style>
