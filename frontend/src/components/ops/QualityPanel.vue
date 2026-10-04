<!--
  QualityPanel — 「质量」Tab(设计稿 §4.1)。

  六条硬规则在页面上的落点:
    1. 产物缺失 → 该卡片显示空态 + DegradedNotice 明说原因,判不了就是
       「判不了」——不渲染成通过;
    2. 未判定条目不进分子分母(端点的 gate 复用了门禁纯函数,页面不再算);
    3. not_concluded 是合法结论:徽章第三态,与 pass/regress 并列;
    4. 覆盖率在产物卡片上,覆盖率徽章紧挨判定徽章(样本缩水要看得见);
    5. gold SQL 只在失败详情抽屉里出现(端点是 admin-only);
    6. 起草教训 = 预填复制,没有任何写端点。

  失败清单的筛选/排序/翻页是**客户端**的:端点按 failures_limit 取前 N 条
  (默认 50),页面在其内过滤 —— 与文档 §3.2 的 URL 键 {q, verdict, path,
  page, sort, order} 一一对应,刷新/分享/后退都复原。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Search } from 'lucide-vue-next'
import type { OpsArtifact, OpsFailureItem, QualityOverview } from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import { useListQuery } from '../../composables/useListQuery'
import DataTable, {
  type DataTableColumn,
  type DataTableSort,
} from '../base/DataTable.vue'
import KpiTile from '../base/KpiTile.vue'
import StatePanel from '../base/StatePanel.vue'
import FailureDrawer from './FailureDrawer.vue'
import FeedbackStrip from './FeedbackStrip.vue'
import SourceChip from './SourceChip.vue'

const props = defineProps<{
  data: QualityOverview | null
  loading: boolean
  error: string
}>()

const emit = defineEmits<{ (e: 'retry'): void }>()

const ui = useUiStore()
const PAGE_SIZE = 20

type I18nKey = Parameters<typeof t>[0]

/* 失败清单的筛选/排序/翻页 —— 全部住在 URL 里。 */
const { values, isActive: isFiltered, reset: resetFilters } = useListQuery({
  q: '',
  verdict: '',
  path: '',
  page: '1',
  sort: 'qid',
  order: 'asc',
})

const items = computed<OpsFailureItem[]>(() => props.data?.failures?.items ?? [])
const verdictOptions = computed(() => Object.keys(props.data?.failures?.by_verdict ?? {}).sort())
const pathOptions = computed(() => Object.keys(props.data?.failures?.by_path ?? {}).sort())

const filtered = computed(() => {
  const q = values.q.trim().toLowerCase()
  return items.value.filter((it) => {
    if (values.verdict && it.verdict !== values.verdict) return false
    if (values.path && (it.path ?? 'UNKNOWN') !== values.path) return false
    if (q && !`${it.question} ${it.qid}`.toLowerCase().includes(q)) return false
    return true
  })
})

const sorted = computed(() => {
  const dir = values.order === 'asc' ? 1 : -1
  const key = values.sort
  const rows = [...filtered.value]
  rows.sort((a, b) => {
    if (key === 'retries') return (a.retries - b.retries) * dir
    const va = String((a as unknown as Record<string, unknown>)[key] ?? '')
    const vb = String((b as unknown as Record<string, unknown>)[key] ?? '')
    return va.localeCompare(vb) * dir
  })
  return rows
})

const page = computed(() => Math.max(1, Number.parseInt(values.page, 10) || 1))
const pageCount = computed(() => Math.max(1, Math.ceil(sorted.value.length / PAGE_SIZE)))
const paged = computed(() =>
  sorted.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)
const tableSort = computed<DataTableSort>(() => ({
  key: values.sort,
  dir: values.order === 'asc' ? 'asc' : 'desc',
}))

// 任一筛选变化 → 回到第一页(留着第 3 页会落在空列表上)。
watch(
  () => [values.q, values.verdict, values.path],
  () => {
    values.page = '1'
  },
)

const columns = computed<DataTableColumn[]>(() => [
  { key: 'qid', label: t('opsColQid', ui.lang), sortable: true, width: 90 },
  { key: 'question', label: t('opsColQuestion', ui.lang), sortable: true },
  { key: 'verdict', label: t('opsColVerdict', ui.lang), sortable: true, width: 130 },
  { key: 'path', label: t('opsColPath', ui.lang), sortable: true, width: 100 },
  { key: 'retries', label: t('opsColRetries', ui.lang), sortable: true, width: 70, align: 'right' },
  { key: 'actions', label: '', width: 84 },
])

const metricColumns = computed<DataTableColumn[]>(() => [
  { key: 'metric', label: t('opsColMetric', ui.lang) },
  { key: 'baseline', label: t('opsColBaseline', ui.lang), width: 100, align: 'right' },
  { key: 'current', label: t('opsColCurrent', ui.lang), width: 100, align: 'right' },
  { key: 'delta', label: t('opsColDelta', ui.lang), width: 90, align: 'right' },
  { key: 'tolerance', label: t('opsColTolerance', ui.lang), width: 90 },
  { key: 'ok', label: t('opsColVerdict', ui.lang), width: 90 },
])

/** 失败行的身份:qid 允许重复(裁决 ② 不做 qid 去重),索引是唯一稳定键。 */
function failureKey(row: unknown, index: number): string {
  return `${(row as OpsFailureItem).qid}#${index}`
}

/** 覆盖率徽章紧挨判定徽章的绑定源(模板里少一层可选链)。 */
const coverage = computed(() => props.data?.current?.coverage ?? null)

const gateTone = computed(() => {
  const v = props.data?.gate.verdict
  if (v === 'pass') return 'is-pass'
  if (v === 'regress') return 'is-regress'
  return 'is-nc'
})

const gateLabel = computed(() => {
  const v = props.data?.gate.verdict
  if (v === 'pass') return t('opsGatePass', ui.lang)
  if (v === 'regress') return t('opsGateRegress', ui.lang)
  return t('opsGateNotConcluded', ui.lang)
})

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function num(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined) return '—'
  return Number(v).toFixed(digits)
}

function coverageText(cov: { covered: number; baseline_qids: number } | null): string {
  if (!cov) return '—'
  return `${cov.covered}/${cov.baseline_qids}`
}

function artifactSub(a: OpsArtifact | null): string {
  if (!a) return t('opsEmptyFirstRun', ui.lang)
  const parts = [
    t('opsQualitySamples', ui.lang, a.n),
    a.n_judged === null ? '' : t('opsQualityJudged', ui.lang, a.n_judged),
    a.kind,
  ].filter(Boolean)
  return parts.join(' · ')
}

/* ── 判定质量全局面(B8)─────────────────────────────────────────
 * 整块拿不到(store 未装配/枚举失败 → decisions = null)就不渲染 ——
 * 缺席容忍;跨源只给总量,per-rule 明细在「决策」页。 */

interface DecisionRow {
  datasource: string
  buckets: number
  ok: number
  alert: number
  error: number
  triggered_rate: number | null
  decided: number
  effective: number
  effective_rate: number | null
  /** 不足原因已译人话(与决策页同一套键);空串 = 没不足。 */
  insufficient: string
}

const decisions = computed(() => props.data?.decisions ?? null)

/** 每源一行(扁平化 —— DataTable 的键是平的;比率不足的成因原样带上)。 */
const decisionRows = computed<DecisionRow[]>(() =>
  (decisions.value?.reports ?? []).map((r) => ({
    datasource: r.datasource,
    buckets: r.summary.buckets,
    ok: r.summary.ok,
    alert: r.summary.alert,
    error: r.summary.error,
    triggered_rate: r.summary.triggered_rate,
    decided: r.summary.decided,
    effective: r.summary.effects.effective,
    effective_rate: r.summary.effective_rate,
    insufficient: r.summary.insufficient.map(insuffLabel).join(' / '),
  })),
)

const decisionColumns = computed<DataTableColumn[]>(() => [
  { key: 'datasource', label: t('opsDecisionsSource', ui.lang), mono: true },
  { key: 'buckets', label: t('opsDecisionsBuckets', ui.lang), width: 110, numeric: true },
  { key: 'ok', label: t('opsDecisionsVerdicts', ui.lang), width: 170 },
  { key: 'triggered_rate', label: t('opsDecisionsTriggeredRate', ui.lang), width: 110, numeric: true },
  { key: 'effective_rate', label: t('opsDecisionsEffectiveRate', ui.lang) },
])

/** 不足原因 → 展示词(复用决策页的键:同一概念一份措辞)。 */
const INSUFF_KEY: Record<string, I18nKey> = {
  few_verdicts: 'decisionsQualityFewVerdicts',
  few_effects: 'decisionsQualityFewEffects',
  no_effects: 'decisionsQualityNoEffects',
}

function insuffLabel(code: string): string {
  const key = INSUFF_KEY[code]
  return key ? t(key, ui.lang) : code
}

/** 两张产物卡片:{当前, 基线} —— 逐字段渲染同一形状。 */
const artifacts = computed(() => [
  { key: 'current', title: t('opsQualityCurrent', ui.lang), art: props.data?.current ?? null },
  { key: 'baseline', title: t('opsQualityBaseline', ui.lang), art: props.data?.baseline ?? null },
])

/* ── 失败详情抽屉 ── */
const drawerOpen = ref(false)
const activeFailure = ref<OpsFailureItem | null>(null)

function openFailure(row: unknown) {
  activeFailure.value = row as OpsFailureItem
  drawerOpen.value = true
}
</script>

<template>
  <div class="quality-panel">
    <StatePanel
      v-if="error"
      mode="error"
      :title="t('opsErrorTitle', ui.lang)"
      :detail="error"
      :retry-text="t('opsRetry', ui.lang)"
      @retry="emit('retry')"
    />

    <StatePanel
      v-else-if="loading && !data"
      mode="loading"
      :title="t('opsLoading', ui.lang)"
    />

    <StatePanel
      v-else-if="data && !data.available && !data.current && !data.baseline"
      mode="empty"
      :title="t('opsEmptyFirstRun', ui.lang)"
      :description="t('opsEmptyFirstRunDesc', ui.lang)"
    />

    <template v-else-if="data">
      <!-- 产物卡片:当前 / 基线 -->
      <div class="artifact-row">
        <section v-for="a in artifacts" :key="a.key" class="ops-card">
          <header class="card-head">
            <h3 class="card-title">{{ a.title }}</h3>
            <SourceChip kind="file" :label="a.art?.path || ''" />
          </header>
          <template v-if="a.art">
            <div class="artifact-meta">
              <span>{{ artifactSub(a.art) }}</span>
              <span v-if="a.art.batch_at">
                {{ t('opsQualityBatch', ui.lang) }}: {{ fmtDateTime(a.art.batch_at) }}
              </span>
              <span v-if="a.art.mtime">
                {{ t('opsQualityMtime', ui.lang) }}: {{ fmtDateTime(a.art.mtime) }}
              </span>
            </div>
            <div class="artifact-kpis">
              <KpiTile :label="'EX'" :value="pct(a.art.metrics?.ex)" />
              <KpiTile
                :label="t('opsColMetric', ui.lang) + ' · compile_hit'"
                :value="pct(a.art.metrics?.compile_hit)"
              />
            </div>
          </template>
          <p v-else class="ops-note">{{ t('opsEmptyFirstRun', ui.lang) }}</p>
        </section>
      </div>

      <!-- 判定 + 覆盖率徽章并排(样本缩水要看得见) -->
      <section class="ops-card gate-card">
        <header class="card-head">
          <h3 class="card-title">{{ t('opsQualityGate', ui.lang) }}</h3>
          <span class="gate-badge" :class="gateTone">{{ gateLabel }}</span>
          <span
            v-if="coverage"
            class="cov-chip"
            :title="t('opsQualityDupQidsTitle', ui.lang)"
          >
            {{ t('opsQualityCoverage', ui.lang) }}
            {{ coverageText(coverage) }}
            ({{ pct(coverage.ratio) }})
            <span
              v-if="coverage.duplicate_qids.length"
              class="cov-dup"
            >
              {{ t('opsQualityDupQids', ui.lang, coverage.duplicate_qids.length) }}
            </span>
          </span>
          <span class="gate-minn">{{ t('opsQualityMinN', ui.lang, data.gate.min_n) }}</span>
        </header>

        <p v-if="data.gate.reason" class="gate-reason">
          <strong>{{ t('opsQualityGateReason', ui.lang) }}:</strong> {{ data.gate.reason }}
        </p>

        <DataTable
          v-if="data.gate.metrics.length"
          class="metrics-table"
          :columns="metricColumns"
          :rows="data.gate.metrics"
          row-key="metric"
        >
          <template #cell-baseline="{ value }">{{ num(value as number | null) }}</template>
          <template #cell-current="{ value }">{{ num(value as number | null) }}</template>
          <template #cell-delta="{ value }">
            <span :class="Number(value) < 0 ? 'delta-down' : 'delta-up'">{{ num(value as number) }}</span>
          </template>
          <template #cell-ok="{ value }">
            <span class="ok-badge" :class="value ? 'is-ok' : 'is-bad'">
              {{ value ? '✓' : '✗' }}
            </span>
          </template>
        </DataTable>

        <div v-if="data.gate.unpaired.length" class="gate-notes">
          <span class="gate-notes-label">{{ t('opsQualityUnpaired', ui.lang) }}:</span>
          <code v-for="m in data.gate.unpaired" :key="m" class="gate-note-chip">{{ m }}</code>
        </div>
        <div v-if="data.gate.denominator_notes.length" class="gate-notes">
          <span class="gate-notes-label">{{ t('opsQualityDenominator', ui.lang) }}:</span>
          <span v-for="(n, i) in data.gate.denominator_notes" :key="i" class="gate-note-chip">{{
            n
          }}</span>
        </div>
      </section>

      <!-- 判定质量全局面(拿不到不整节渲染;跨源只给总量,明细在决策页) -->
      <section v-if="decisions" class="ops-card decisions-card">
        <header class="card-head">
          <h3 class="card-title">{{ t('opsDecisionsTitle', ui.lang) }}</h3>
          <span class="fail-count">
            {{ t('opsDecisionsSources', ui.lang, decisions.datasources) }}
          </span>
        </header>
        <p class="ops-note decisions-desc">{{ t('opsDecisionsDesc', ui.lang) }}</p>
        <div class="artifact-kpis">
          <KpiTile
            :label="t('opsDecisionsVerdicts', ui.lang)"
            :value="decisions.summary.total"
          />
          <KpiTile
            :label="t('opsDecisionsTriggeredRate', ui.lang)"
            :value="pct(decisions.summary.triggered_rate)"
          />
          <KpiTile
            :label="t('opsDecisionsMeasured', ui.lang)"
            :value="decisions.summary.effects.measured"
          />
          <KpiTile
            :label="t('opsDecisionsEffectiveRate', ui.lang)"
            :value="pct(decisions.summary.effective_rate)"
          />
        </div>
        <div v-if="decisions.summary.insufficient.length" class="gate-notes">
          <span class="gate-notes-label">{{ t('decisionsQualityRateGated', ui.lang) }}:</span>
          <span v-for="c in decisions.summary.insufficient" :key="c" class="gate-note-chip">
            {{ insuffLabel(c) }}
          </span>
        </div>
        <p v-if="!decisionRows.length" class="ops-note">{{ t('opsDecisionsEmpty', ui.lang) }}</p>
        <DataTable
          v-else
          class="decisions-table"
          :columns="decisionColumns"
          :rows="decisionRows"
          row-key="datasource"
        >
          <template #cell-ok="{ row }">
            <span class="decisions-counts">
              {{ (row as DecisionRow).ok }} /
              <span class="delta-down">{{ (row as DecisionRow).alert }}</span> /
              <span class="delta-down decisions-count-error">{{
                (row as DecisionRow).error
              }}</span>
            </span>
          </template>
          <template #cell-triggered_rate="{ value }">
            {{ pct(value as number | null) }}
          </template>
          <template #cell-effective_rate="{ row }">
            <span v-if="(row as DecisionRow).effective_rate != null" class="decisions-counts">
              {{ pct((row as DecisionRow).effective_rate) }}
            </span>
            <!-- 分母不够就不给比率;说清为什么(与决策页同一纪律) -->
            <span v-else class="decisions-gated">
              {{ t('decisionsQualityRateGated', ui.lang)
              }}<template v-if="(row as DecisionRow).insufficient">
                · {{ (row as DecisionRow).insufficient }}</template>
            </span>
          </template>
        </DataTable>
      </section>

      <!-- 失败清单 -->
      <section class="ops-card">
        <header class="card-head">
          <h3 class="card-title">{{ t('opsQualityFailures', ui.lang) }}</h3>
          <span v-if="data.failures" class="fail-count">
            {{ t('opsQualityFailuresTotal', ui.lang, data.failures.total) }}
          </span>
          <span v-if="data.failures?.truncated" class="fail-trunc">
            {{ t('opsQualityFailuresTruncated', ui.lang, data.failures.items.length) }}
          </span>
        </header>

        <div class="list-toolbar">
          <el-input
            v-model="values.q"
            class="toolbar-search"
            :prefix-icon="Search"
            :placeholder="t('opsQualitySearchPlaceholder', ui.lang)"
            :aria-label="t('opsQualitySearchAria', ui.lang)"
            clearable
          />
          <el-select v-model="values.verdict" class="filter-select">
            <el-option :label="t('opsFilterVerdict', ui.lang)" value="" />
            <el-option v-for="v in verdictOptions" :key="v" :label="v" :value="v" />
          </el-select>
          <el-select v-model="values.path" class="filter-select">
            <el-option :label="t('opsFilterPath', ui.lang)" value="" />
            <el-option v-for="p in pathOptions" :key="p" :label="p" :value="p" />
          </el-select>
          <button v-if="isFiltered" type="button" class="clear-btn" @click="resetFilters">
            {{ t('opsClearFilters', ui.lang) }}
          </button>
        </div>

        <DataTable
          class="failures-table"
          :columns="columns"
          :rows="paged"
          :row-key="failureKey"
          :sort="tableSort"
          row-clickable
          @update:sort="
            (s) => {
              values.sort = s.key
              values.order = s.dir
            }
          "
          @row-click="openFailure"
        >
          <template #cell-verdict="{ value }">
            <span class="verdict-chip" :class="`is-${String(value).toLowerCase()}`">{{
              value
            }}</span>
          </template>
          <template #cell-actions="{ row }">
            <button type="button" class="row-btn" @click.stop="openFailure(row)">
              {{ t('opsViewDetail', ui.lang) }}
            </button>
          </template>
          <template #empty>
            <StatePanel
              mode="empty"
              :title="items.length ? t('opsEmptyFiltered', ui.lang) : t('opsEmptyNoFailures', ui.lang)"
              :description="items.length ? '' : t('opsEmptyNoFailuresDesc', ui.lang)"
            />
          </template>
        </DataTable>

        <div v-if="sorted.length > PAGE_SIZE" class="pager">
          <button
            type="button"
            class="pager-btn"
            :disabled="page <= 1"
            @click="values.page = String(page - 1)"
          >
            ‹
          </button>
          <span class="pager-info">{{ page }} / {{ pageCount }}</span>
          <button
            type="button"
            class="pager-btn"
            :disabled="page >= pageCount"
            @click="values.page = String(page + 1)"
          >
            ›
          </button>
        </div>
      </section>

      <FeedbackStrip :feedback="data.feedback" />
    </template>

    <FailureDrawer v-model="drawerOpen" :item="activeFailure" />
  </div>
</template>

<style scoped>
.artifact-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: var(--sp-3);
}
.artifact-row .ops-card {
  margin-bottom: var(--sp-3);
}
.ops-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  padding: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}
.card-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.artifact-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.artifact-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--sp-2);
}
.gate-badge,
.cov-chip,
.gate-minn,
.fail-count,
.fail-trunc {
  font-size: var(--fs-2xs);
  padding: 1px 8px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-subtle);
}
.gate-badge.is-pass {
  color: var(--success, #15803d);
  border-color: var(--success-border, currentColor);
  font-weight: 600;
}
.gate-badge.is-regress {
  color: var(--danger, #b91c1c);
  border-color: var(--danger-border, currentColor);
  font-weight: 600;
}
.gate-badge.is-nc {
  color: var(--warn, #b45309);
  font-weight: 600;
}
.cov-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
}
.cov-dup {
  color: var(--warn, #b45309);
  font-weight: 600;
}
.gate-minn {
  margin-left: auto;
  color: var(--text-tertiary);
}
.gate-reason {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.gate-notes {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.gate-notes-label {
  font-weight: 600;
}
.gate-note-chip {
  padding: 1px 6px;
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  border: 1px solid var(--border-subtle);
}
.delta-down {
  color: var(--danger, #b91c1c);
}
.delta-up {
  color: var(--success, #15803d);
}
.ok-badge.is-ok {
  color: var(--success, #15803d);
}
.ok-badge.is-bad {
  color: var(--danger, #b91c1c);
  font-weight: 600;
}
.list-toolbar {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}
.toolbar-search {
  max-width: 280px;
}
.filter-select {
  width: 150px;
}
.clear-btn {
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  border-radius: var(--r-sm);
  padding: 4px 10px;
  font-size: var(--fs-2xs);
  cursor: pointer;
  color: var(--text-secondary);
}
.verdict-chip {
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-secondary);
}
.verdict-chip.is-mismatch,
.verdict-chip.is-execution_error,
.verdict-chip.is-generation_error {
  color: var(--danger, #b91c1c);
}
.row-btn {
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  border-radius: var(--r-sm);
  padding: 2px 8px;
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.pager-btn {
  border: 1px solid var(--border-default);
  background: var(--surface-raised);
  border-radius: var(--r-sm);
  width: 24px;
  height: 22px;
  cursor: pointer;
}
.pager-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.ops-note {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.decisions-desc {
  margin: 0 0 var(--sp-2);
}
.decisions-table {
  margin-top: var(--sp-2);
}
.decisions-counts {
  font-variant-numeric: tabular-nums;
}
.decisions-count-error {
  font-weight: 600;
}
.decisions-gated {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
</style>
