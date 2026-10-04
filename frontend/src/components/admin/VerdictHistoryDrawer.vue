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
  // N2:digest 是整份 decisions.yml 的字节 hash —— 编辑别的规则也会亮。
  // rev 两边都在时只信 rev("这条规则被改过");rev 缺席(B2 之前的行)才
  // 退回 digest,保持老行为。
  const revComparable = Boolean(d.prev_rule_rev) && Boolean(d.rule_rev)
  const ruleChanged = revComparable ? d.rule_rev_changed : d.rule_digest_changed
  if (ruleChanged) bits.push(t('decisionsDiffDigest', ui.lang))
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

function significanceOf(
  ev: Record<string, unknown>,
): Record<string, unknown> | null {
  const s = ev.significance
  return s && typeof s === 'object' ? (s as Record<string, unknown>) : null
}

function bandEntries(
  ev: Record<string, unknown>,
): { dim: string; entry: Record<string, unknown> }[] {
  const by = significanceOf(ev)?.by_dim
  if (!by || typeof by !== 'object') return []
  return Object.entries(by as Record<string, unknown>).map(([dim, entry]) => ({
    dim,
    entry: (entry && typeof entry === 'object' ? entry : {}) as Record<string, unknown>,
  }))
}

function bandDegradedOf(ev: Record<string, unknown>): Record<string, unknown>[] {
  const d = significanceOf(ev)?.degraded
  return Array.isArray(d) ? (d as Record<string, unknown>[]) : []
}

/** 带门列只在规则声明了 significance 时出现(未声明 → 与历史逐列一致)。 */
function cardsHaveBand(ev: Record<string, unknown>): boolean {
  return judgedRowsOf(ev).some((c) => 'gated' in c)
}

function budgetOf(
  ev: Record<string, unknown>,
): { used: number; limit: number } | null {
  const b = ev.budget as Record<string, unknown> | undefined
  if (!b || typeof b.used !== 'number' || typeof b.limit !== 'number') return null
  return { used: b.used, limit: b.limit }
}

/** 因果升级梯(B3):无 rung 则整节不渲染(R1:梯子必须显式出现,
 *  宁可不显示结论,也不显示一个看不出硬度的结论)。 */
function causalOf(v: VerdictDetail): Record<string, unknown> | null {
  const c = evidenceOf(v).causal
  if (!c || typeof c !== 'object') return null
  const m = c as Record<string, unknown>
  return typeof m.rung === 'string' && m.rung ? m : null
}

function causalRows(
  v: VerdictDetail,
  key: string,
): Record<string, unknown>[] {
  const rows = causalOf(v)?.[key]
  return Array.isArray(rows) ? (rows as Record<string, unknown>[]) : []
}

/** 净效应行:有效应量才出现(与后端消息的「净效应」后缀同判据)。 */
function causalEffectOf(
  v: VerdictDetail,
): { effect: number; design: string; crosses: boolean | null } | null {
  const c = causalOf(v)
  const src = c?.rung === 'L3' ? c.synthetic : c?.rung === 'L2' ? c.did : null
  const s = src && typeof src === 'object' ? (src as Record<string, unknown>) : null
  if (!s) return null
  const effect = typeof s.effect === 'number'
    ? s.effect
    : typeof s.att === 'number' ? s.att : null
  if (effect === null) return null
  return {
    effect,
    design: t(
      c!.rung === 'L3' ? 'decisionsCausalSynthetic' : 'decisionsCausalDid',
      ui.lang,
    ),
    crosses: typeof s.crosses_zero === 'boolean' ? s.crosses_zero : null,
  }
}

/** 假设条目的三态:✓ 已检验 / — 无法检验(写出来而非省略)/ ✗ 不成立。 */
function checkedMark(v: unknown): string {
  return v === true ? '✓' : v === false ? '✗' : '—'
}

function num2(v: unknown): string {
  const n = typeof v === 'number' ? v : null
  return n === null ? '—' : n.toFixed(2)
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
                      <th v-if="cardsHaveBand(evidenceOf(details[v.id]))">
                        {{ t('decisionsColBand', ui.lang) }}
                      </th>
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
                      <td v-if="cardsHaveBand(evidenceOf(details[v.id]))">
                        <template v-if="card.gated === true">
                          {{ t('decisionsBandOutside', ui.lang) }}
                          <span class="dim">{{ num2(card.confidence) }}</span>
                        </template>
                        <span v-else class="dim">—</span>
                      </td>
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

            <!-- 噪声带(B2):声明了 significance 的规则才有这一节。note 是
                 后端的限定语(位置分数非概率)—— 它必须出现,否则这一节
                 会被读成概率/因果。degraded 同样必须显示:带没算出来和
                 "没有异常"是两回事。 -->
            <div v-if="significanceOf(evidenceOf(details[v.id]))" class="vh-sec">
              <div class="vh-sec-title">{{ t('decisionsSignificance', ui.lang) }}</div>
              <p
                v-if="significanceOf(evidenceOf(details[v.id]))!.note"
                class="dim vh-residual"
              >
                {{ significanceOf(evidenceOf(details[v.id]))!.note }}
              </p>
              <div
                v-if="bandEntries(evidenceOf(details[v.id])).length"
                class="vh-table-wrap"
              >
                <table class="vh-table cell-mono">
                  <thead>
                    <tr>
                      <th>{{ t('decisionsColDim', ui.lang) }}</th>
                      <th>z</th>
                      <th>k</th>
                      <th>{{ t('decisionsColPosition', ui.lang) }}</th>
                      <th>{{ t('decisionsColTriggered', ui.lang) }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(b, i) in bandEntries(evidenceOf(details[v.id]))"
                      :key="i"
                      :class="{ 'vh-fired': b.entry.gated }"
                    >
                      <td>{{ b.dim || '—' }}</td>
                      <td>{{ num2(b.entry.z) }}</td>
                      <td>{{ num2(b.entry.k) }}</td>
                      <td>{{ num2(b.entry.confidence) }}</td>
                      <td>
                        {{ b.entry.gated ? t('decisionsBandOutside', ui.lang)
                          : (b.entry.reason || t('decisionsBandUnavailable', ui.lang)) }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <ul
                v-if="bandDegradedOf(evidenceOf(details[v.id])).length"
                class="vh-degraded"
              >
                <li
                  v-for="(d, i) in bandDegradedOf(evidenceOf(details[v.id]))"
                  :key="i"
                  class="cell-mono"
                >
                  {{ t('decisionsBandDegraded', ui.lang) }} · {{ d.reason }}
                </li>
              </ul>
            </div>

            <!-- 查询账目:这次判定花了几条、花在哪。让路(yield)的原因在
                 degraded 里,这里只给总量。显著性/因果任一在场即有账本
                 —— 所以它站在两节之外,不被其中任一节的缺席藏掉。 -->
            <p
              v-if="budgetOf(evidenceOf(details[v.id]))"
              class="dim vh-residual cell-mono"
            >
              {{ t('decisionsBudget', ui.lang) }}
              {{ budgetOf(evidenceOf(details[v.id]))!.used }}/{{ budgetOf(evidenceOf(details[v.id]))!.limit }}
            </p>

            <!-- 因果升级梯(B3):声明了 causal 的规则才有这一节,无 rung
                 则整节不渲染(R1:梯子显式出现,否则不显示结论)。unmet
                 带实测/阈值、assumptions 三态、degraded 全部必须可见 ——
                 「结论有多硬、差在哪」正是这一节存在的意义。 -->
            <div v-if="causalOf(details[v.id])" class="vh-sec">
              <div class="vh-sec-title">{{ t('decisionsCausal', ui.lang) }}</div>
              <p class="dim vh-residual">
                {{ causalOf(details[v.id])!.note }}
              </p>
              <p class="vh-residual">
                <span class="cell-mono">
                  {{ t('decisionsCausalRung', ui.lang) }}
                  {{ causalOf(details[v.id])!.rung }}
                </span>
                <template v-if="causalEffectOf(details[v.id])">
                  · {{ t('decisionsCausalNet', ui.lang) }}
                  <span class="cell-mono">
                    {{ fmtVal(causalEffectOf(details[v.id])!.effect) }}
                  </span>
                  ({{ causalEffectOf(details[v.id])!.design }}<template
                    v-if="causalEffectOf(details[v.id])!.crosses !== null"
                  >,{{ causalEffectOf(details[v.id])!.crosses
                    ? t('decisionsCausalCrosses', ui.lang)
                    : t('decisionsCausalNotCrosses', ui.lang) }}</template>)
                </template>
              </p>
              <ul
                v-if="causalRows(details[v.id], 'unmet').length"
                class="vh-degraded"
              >
                <li
                  v-for="(u, i) in causalRows(details[v.id], 'unmet')"
                  :key="i"
                  class="cell-mono"
                >
                  {{ t('decisionsCausalUnmet', ui.lang) }} · {{ u.condition }}: {{ u.reason }}
                  <template v-if="u.measured != null && u.threshold != null">
                    ({{ t('decisionsCausalMeasured', ui.lang) }} {{ num2(u.measured) }}
                    / {{ t('decisionsCausalThreshold', ui.lang) }} {{ num2(u.threshold) }})
                  </template>
                </li>
              </ul>
              <template v-if="causalRows(details[v.id], 'assumptions').length">
                <div class="vh-sec-title">
                  {{ t('decisionsCausalAssumptions', ui.lang) }}
                </div>
                <ul class="vh-comps">
                  <li
                    v-for="(a, i) in causalRows(details[v.id], 'assumptions')"
                    :key="i"
                  >
                    <span class="cell-mono">{{ checkedMark(a.checked) }}</span>
                    <span class="dim">
                      · {{ a.text }}<template v-if="a.detail">（{{ a.detail }}）</template>
                    </span>
                  </li>
                </ul>
              </template>
              <ul
                v-if="causalRows(details[v.id], 'degraded').length"
                class="vh-degraded"
              >
                <li
                  v-for="(d, i) in causalRows(details[v.id], 'degraded')"
                  :key="i"
                  class="cell-mono"
                >
                  {{ t('decisionsCausalDegraded', ui.lang) }} · {{ d.reason }}
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
  font-size: var(--fs-2xs);
}
.vh-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-2xs);
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
  font-size: var(--fs-2xs);
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
  font-size: var(--fs-2xs);
}
.vh-detail {
  padding: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
}
.vh-detail-error {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--el-color-danger);
}
.vh-sec + .vh-sec {
  margin-top: var(--sp-3);
}
.vh-sec-title {
  margin-bottom: 4px;
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.vh-sql {
  margin: 0 0 4px;
  padding: var(--sp-2);
  font-size: var(--fs-2xs);
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
  font-size: var(--fs-2xs);
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
  font-size: var(--fs-2xs);
}
.vh-degraded {
  color: var(--el-color-warning);
}
.vh-residual {
  margin: 4px 0 0;
  font-size: var(--fs-2xs);
}
</style>
