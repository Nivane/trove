<!--
  SemanticView — the semantic workbench (P2 console rebuild).

  The page's contract, in one place:

  - state lives in the URL (?ds=&tab=&q=&sort=&order=&page=&prob=&draft=;
    ordering is two keys per §4.3 U2, never sort=key:dir), so a filtered
    view is shareable and ?draft=<id> is a deep link;
  - the five KPI numbers are entry points (pending → queue, problems → the
    panel filtered to errors), not decoration;
  - problems are structured rows ([定位] jumps to the asset), and the drift
    section distinguishes "not checked" from "clean" — skipped must never
    render as no-drift;
  - the detail drawer gathers definition / expression (copyable, editable) /
    anchoring / value dictionary / references / history, with the danger
    action at the bottom;
  - editing an expression validates and dry-runs against the *same*
    compiler the chat path uses before a draft is created;
  - drafts are shown as a server-computed diff, validated before applying;
  - bulk approve/reject goes through the batch endpoint and renders
    per-item failures with a retry;
  - conflicted drafts (A2) carry a server annotation: they render as such,
    confirm is pre-disabled (the server refuses them too), and "create from
    this draft" — in the drawer, or via the refusal card's bp_* deep link —
    opens the matching dialog prefilled with the draft's own content.

  All copy comes from i18n (`sem*` keys); server strings (lint messages,
  diff rows) are transcribed verbatim — they are data, not UI copy.
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronDown, Copy, Plus, RefreshCw, Search, TriangleAlert } from 'lucide-vue-next'
import type {
  DatasourceInfo,
  SemanticBatchResult,
  SemanticChangeDetail,
  SemanticChangeRecord,
  SemanticDatasetInfo,
  SemanticDetail,
  SemanticDraft,
  SemanticDriftItem,
  SemanticFieldInfo,
  SemanticHistoryEntry,
  SemanticIssueItem,
  SemanticMetricInfo,
  SemanticModelInfo,
  SemanticPreviewResult,
  SemanticValidateResult,
} from '../../api/types'
import { apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, toastError } from '../../utils/notify'
import { copyText, fmtDateTime, trunc } from '../../utils/format'
import { changeStatusTone, impactLines, verdictKey } from '../../utils/semantic-changes'
import { useListQuery } from '../../composables/useListQuery'
import PageHeader from '../../components/base/PageHeader.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import DataTable, {
  type DataTableColumn,
  type DataTableSort,
} from '../../components/base/DataTable.vue'
import DetailDrawer from '../../components/base/DetailDrawer.vue'
import ConfirmDialog from '../../components/base/ConfirmDialog.vue'

/** 动态键（纯函数返回的 i18n 键名）无法被字面量类型收窄，按仓库既有写法回铸。 */
type I18nKey = Parameters<typeof t>[0]

type Kind = 'metric' | 'field' | 'dataset'
type FieldRow = SemanticFieldInfo & { dataset: string }
interface AuditEntry {
  ts?: string
  username?: string
  action?: string
  status?: number
  details?: Record<string, unknown>
}

const ui = useUiStore()
const router = useRouter()
const route = useRoute()

const PAGE_SIZE = 25

const { values } = useListQuery({
  ds: '',
  tab: 'metrics',
  q: '',
  sort: '',
  // U2: the direction is its own key — the same shape the users endpoint
  // takes (sort=key&order=asc|desc), never the packed sort=key:dir.
  order: '',
  page: '',
  prob: '',
  draft: '',
  // 变更单评审的深链(?change=<id>)
  change: '',
})

/* ── load ─────────────────────────────────────────────────────── */

const datasources = ref<DatasourceInfo[]>([])
const detail = ref<SemanticDetail | null>(null)
const loading = ref(false)
const loadError = ref('')
const driftChecking = ref(false)
const checkError = ref('')

const connected = computed(() => datasources.value.filter((d) => d.status === 'connected'))
const enabled = computed(() => !!detail.value?.enabled)
const model = computed<SemanticModelInfo | null>(() => detail.value?.model ?? null)
const metrics = computed<SemanticMetricInfo[]>(() => model.value?.metrics ?? [])
const datasets = computed<SemanticDatasetInfo[]>(() => model.value?.datasets ?? [])
const fields = computed<FieldRow[]>(() =>
  datasets.value.flatMap((d) => (d.fields ?? []).map((f) => ({ ...f, dataset: d.name }))),
)
const pending = computed<SemanticDraft[]>(() => detail.value?.drafts.pending ?? [])
const applied = computed<SemanticDraft[]>(() => detail.value?.drafts.applied ?? [])
const rejected = computed<SemanticDraft[]>(() => detail.value?.drafts.rejected ?? [])
const issueItems = computed<SemanticIssueItem[]>(() => detail.value?.issue_items ?? [])
const drift = computed(() => detail.value?.drift ?? null)
const driftItems = computed<SemanticDriftItem[]>(() => drift.value?.items ?? [])
const driftSkipped = computed(() => drift.value?.status === 'skipped')
const driftClean = computed(() => drift.value?.status === 'ok' && driftItems.value.length === 0)
const datasetNames = computed(() => datasets.value.map((d) => d.name))

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) return String((e as { message: unknown }).message)
  return String(e ?? '')
}

async function loadDatasources() {
  try {
    const body = await apiGet<{ datasources: DatasourceInfo[] }>('/v1/admin/datasources')
    datasources.value = body.datasources ?? []
    if (!values.ds && connected.value.length) {
      const dflt = connected.value.find((d) => d.default)
      values.ds = dflt ? dflt.name : connected.value[0].name
    }
  } catch (e) {
    loadError.value = errMsg(e)
  }
}

async function loadDetail() {
  const ds = values.ds
  if (!ds) return
  loading.value = true
  loadError.value = ''
  try {
    const body = await apiGet<{ semantic: SemanticDetail }>(
      `/v1/admin/semantic/${encodeURIComponent(ds)}`,
    )
    if (values.ds !== ds) return // raced with a datasource switch
    detail.value = body.semantic
    // The context strip shows the last commit, so history is not drawer-lazy.
    void ensureTrail()
  } catch (e) {
    if (values.ds !== ds) return
    detail.value = null
    loadError.value = errMsg(e)
  } finally {
    if (values.ds === ds) loading.value = false
  }
}

function resetViewState() {
  assetKey.value = null
  closeEditor()
  draftCheck.value = null
  draftCheckError.value = ''
  selected.value = []
  batchResults.value = null
  history.value = []
  audit.value = []
  historyLoaded.value = false
  trailError.value = false
  checkError.value = ''
  changesLoaded.value = false
  changeDetail.value = null
  changeRejectReason.value = ''
  changeRejectError.value = ''
}

onMounted(async () => {
  if (values.ds) await loadDetail()
  await loadDatasources()
})

watch(
  () => values.ds,
  async () => {
    resetViewState()
    await loadDetail()
  },
)

/* ── drift re-check ───────────────────────────────────────────── */

async function redetectDrift() {
  const ds = values.ds
  if (!ds) return
  driftChecking.value = true
  checkError.value = ''
  try {
    await apiPost(`/v1/admin/drift/check?datasource=${encodeURIComponent(ds)}`)
  } catch (e) {
    // 503 = the check did not complete. The state stays "not checked" —
    // never washed into "clean"; the panel below shows why.
    checkError.value = errMsg(e)
  } finally {
    driftChecking.value = false
    await loadDetail()
  }
}

/* ── KPIs / tabs / URL-backed filters ─────────────────────────── */

const tabDefs = computed(() => [
  { key: 'metrics', label: t('semMetrics', ui.lang), count: metrics.value.length },
  { key: 'datasets', label: t('semDatasets', ui.lang), count: datasets.value.length },
  { key: 'fields', label: t('semFields', ui.lang), count: fields.value.length },
  { key: 'pending', label: t('semPending', ui.lang), count: pending.value.length },
  { key: 'changes', label: t('semTabChanges', ui.lang), count: openChanges.value.length },
])

const problemCount = computed(() => issueItems.value.length + driftItems.value.length)
const problemsActive = computed(() => values.prob !== '')

const problemSub = computed(() => {
  if (!problemCount.value) return ''
  return `${problemCount.value} = ${t('semProblemSubLint', ui.lang, issueItems.value.length)} + ${t('semProblemSubDrift', ui.lang, driftItems.value.length)}`
})

function switchDs(name: string) {
  if (values.ds !== name) values.ds = name
}

function switchTab(key: string) {
  values.tab = key
  values.page = ''
}

function applyKpi(key: string) {
  if (key === 'problems') {
    values.prob = values.prob === 'error' ? '' : 'error'
    return
  }
  values.tab = key
  values.q = ''
  values.page = ''
  values.prob = ''
}

watch(
  () => values.q,
  () => {
    values.page = ''
  },
)
watch(
  () => values.tab,
  () => {
    values.page = ''
  },
)
watch(pending, () => {
  const live = new Set(pending.value.map((d) => d.id))
  selected.value = selected.value.filter((id) => live.has(id))
})

/* ── filters / sorting / paging ───────────────────────────────── */

function matchesQ(q: string, ...parts: (string | undefined | null)[]): boolean {
  if (!q) return true
  const needle = q.trim().toLowerCase()
  if (!needle) return true
  return parts.some((p) => (p ?? '').toLowerCase().includes(needle))
}

const q = computed(() => values.q)

function sortWith<T>(rows: T[], getter: (row: T) => string | number, dir: 'asc' | 'desc'): T[] {
  const out = [...rows].sort((a, b) => {
    const va = getter(a)
    const vb = getter(b)
    if (typeof va === 'number' && typeof vb === 'number') return va - vb
    return String(va).localeCompare(String(vb), undefined, { numeric: true, sensitivity: 'base' })
  })
  return dir === 'desc' ? out.reverse() : out
}

const sortState = computed<DataTableSort | null>(() => {
  if (!values.sort) return null
  // a hand-edited ?order= that is neither asc nor desc falls back to asc,
  // matching the backend's own default
  return { key: values.sort, dir: values.order === 'desc' ? 'desc' : 'asc' }
})

function onSort(s: DataTableSort) {
  // Always written: the header toggle reads its next direction from the URL,
  // and "state in the URL" means the chosen ordering is shareable too.
  values.sort = s.key
  values.order = s.dir
  values.page = ''
}

function sortOf(key: string, fallback: 'asc' | 'desc' = 'asc'): { key: string; dir: 'asc' | 'desc' } {
  const s = sortState.value
  return s ?? { key: 'name', dir: fallback }
}

const filteredMetrics = computed(() =>
  metrics.value.filter((m) =>
    matchesQ(q.value, m.name, m.expression, m.definition, (m.synonyms ?? []).join(' '), (m.datasets ?? []).join(' ')),
  ),
)
const filteredFields = computed(() =>
  fields.value.filter((f) =>
    matchesQ(q.value, f.name, `${f.dataset}.${f.name}`, f.expression, f.datatype, f.semantic_role, (f.synonyms ?? []).join(' ')),
  ),
)
const filteredDatasets = computed(() =>
  datasets.value.filter((d) => matchesQ(q.value, d.name, d.source, d.description, (d.synonyms ?? []).join(' '))),
)
const filteredPending = computed(() =>
  pending.value.filter((d) => matchesQ(q.value, d.name, d.kind, d.action, d.note)),
)
const filteredChanges = computed(() =>
  changes.value.filter((c) =>
    matchesQ(q.value, c.id, c.note, c.question, c.origin, c.status, changeSummary(c)),
  ),
)

const METRIC_COLUMNS = computed<DataTableColumn[]>(() => [
  { key: 'name', label: t('semName', ui.lang), sortable: true },
  { key: 'expression', label: t('semExpression', ui.lang), sortable: true },
  { key: 'synonyms', label: t('semSynonyms', ui.lang) },
  { key: 'datasets', label: t('semTables', ui.lang) },
  { key: 'definition', label: t('semDefinition', ui.lang) },
  { key: 'changed', label: t('semChanged', ui.lang), sortable: true, defaultDir: 'desc' as const },
])
const FIELD_COLUMNS = computed<DataTableColumn[]>(() => [
  { key: 'name', label: t('semName', ui.lang), sortable: true },
  { key: 'dataset', label: t('semDataset', ui.lang) },
  { key: 'expression', label: t('semExpression', ui.lang), sortable: true },
  { key: 'datatype', label: t('semDatatype', ui.lang), sortable: true },
  { key: 'role', label: t('semRole', ui.lang), sortable: true },
  { key: 'changed', label: t('semChanged', ui.lang), sortable: true, defaultDir: 'desc' as const },
])
const DATASET_COLUMNS = computed<DataTableColumn[]>(() => [
  { key: 'name', label: t('semName', ui.lang), sortable: true },
  { key: 'source', label: t('semSource', ui.lang), sortable: true },
  { key: 'primary_key', label: t('semPrimaryKey', ui.lang) },
  { key: 'fieldsCount', label: t('semFieldsCount', ui.lang), sortable: true, align: 'right' as const },
  { key: 'description', label: t('semDefinition', ui.lang) },
  { key: 'changed', label: t('semChanged', ui.lang), sortable: true, defaultDir: 'desc' as const },
])
const PENDING_COLUMNS = computed<DataTableColumn[]>(() => [
  { key: 'kind', label: t('semKindLabel', ui.lang) },
  { key: 'action', label: t('semUpsert', ui.lang) },
  { key: 'name', label: t('semName', ui.lang), sortable: true },
  { key: 'note', label: t('semNote', ui.lang) },
  { key: 'created_at', label: t('semCreatedAt', ui.lang), sortable: true, defaultDir: 'desc' as const },
])
const CHANGE_COLUMNS = computed<DataTableColumn[]>(() => [
  { key: 'status', label: t('status', ui.lang), sortable: true },
  { key: 'origin', label: t('semChangeOrigin', ui.lang), sortable: true },
  { key: 'summary', label: t('semChangeSummary', ui.lang) },
  { key: 'subjects', label: t('semChangeSubjects', ui.lang), align: 'right' as const },
  { key: 'created_at', label: t('semCreatedAt', ui.lang), sortable: true, defaultDir: 'desc' as const },
])

const sortedMetrics = computed(() => {
  const s = sortOf('name')
  const get = {
    name: (m: SemanticMetricInfo) => m.name,
    expression: (m: SemanticMetricInfo) => m.expression ?? '',
    changed: (m: SemanticMetricInfo) => changedSortKey(m.name),
  }[s.key] ?? ((m: SemanticMetricInfo) => m.name)
  return sortWith(filteredMetrics.value, get, s.dir)
})
const sortedFields = computed(() => {
  const s = sortOf('name')
  const get = {
    name: (f: FieldRow) => f.name,
    expression: (f: FieldRow) => f.expression ?? '',
    datatype: (f: FieldRow) => f.datatype ?? '',
    role: (f: FieldRow) => roleLabel(f.semantic_role ?? ''),
    changed: (f: FieldRow) => changedSortKey(`${f.dataset}.${f.name}`),
  }[s.key] ?? ((f: FieldRow) => f.name)
  return sortWith(filteredFields.value, get, s.dir)
})
const sortedDatasets = computed(() => {
  const s = sortOf('name')
  const get = {
    name: (d: SemanticDatasetInfo) => d.name,
    source: (d: SemanticDatasetInfo) => d.source ?? '',
    fieldsCount: (d: SemanticDatasetInfo) => (d.fields ?? []).length,
    changed: (d: SemanticDatasetInfo) => changedSortKey(d.name),
  }[s.key] ?? ((d: SemanticDatasetInfo) => d.name)
  return sortWith(filteredDatasets.value, get, s.dir)
})
const sortedPending = computed(() => {
  const s = sortOf('name')
  const get = {
    name: (d: SemanticDraft) => d.name,
    created_at: (d: SemanticDraft) => d.created_at ?? '',
  }[s.key] ?? ((d: SemanticDraft) => d.name)
  return sortWith(filteredPending.value, get, s.dir)
})
const sortedChanges = computed(() => {
  const s = sortOf('created_at', 'desc')
  const get = {
    status: (c: SemanticChangeRecord) => changeStatusLabel(c.status),
    origin: (c: SemanticChangeRecord) => c.origin ?? '',
    created_at: (c: SemanticChangeRecord) => c.created_at ?? '',
  }[s.key] ?? ((c: SemanticChangeRecord) => c.created_at ?? '')
  return sortWith(filteredChanges.value, get, s.dir)
})

function pageOf<T>(rows: T[]): T[] {
  const total = rows.length
  if (total <= PAGE_SIZE) return rows
  const page = Math.min(pageNum.value, Math.max(1, Math.ceil(total / PAGE_SIZE)))
  const start = (page - 1) * PAGE_SIZE
  return rows.slice(start, start + PAGE_SIZE)
}
const pageNum = computed(() => {
  const n = Number.parseInt(values.page || '1', 10)
  return Number.isFinite(n) && n >= 1 ? n : 1
})
const visibleMetrics = computed(() => pageOf(sortedMetrics.value))
const visibleFields = computed(() => pageOf(sortedFields.value))
const visibleDatasets = computed(() => pageOf(sortedDatasets.value))
const visibleChanges = computed(() => pageOf(sortedChanges.value))

const currentTotal = computed(() => {
  if (values.tab === 'datasets') return sortedDatasets.value.length
  if (values.tab === 'fields') return sortedFields.value.length
  if (values.tab === 'pending') return sortedPending.value.length
  if (values.tab === 'changes') return sortedChanges.value.length
  return sortedMetrics.value.length
})
const filtersActive = computed(() => !!q.value.trim())

function onPage(p: number) {
  values.page = p <= 1 ? '' : String(p)
}
function clearFilters() {
  values.q = ''
  values.page = ''
  values.prob = ''
}

function roleLabel(role: string): string {
  const map: Record<string, 'semRoleIdentifier' | 'semRoleMeasure' | 'semRoleDimension' | 'semRoleEnum' | 'semRoleTime'> = {
    identifier: 'semRoleIdentifier',
    measure: 'semRoleMeasure',
    dimension: 'semRoleDimension',
    enum: 'semRoleEnum',
    time: 'semRoleTime',
  }
  const key = map[role]
  return key ? t(key, ui.lang) : role || '—'
}
function kindLabel(kind: string): string {
  const map: Record<string, 'semMetric' | 'semField' | 'semDataset'> = {
    metric: 'semMetric',
    field: 'semField',
    dataset: 'semDataset',
  }
  const key = map[kind]
  return key ? t(key, ui.lang) : kind
}
function actionLabel(action: string): string {
  if (action === 'delete') return t('semDelete', ui.lang)
  if (action === 'upsert') return t('semUpsert', ui.lang)
  return action || '—'
}
function severityLabel(sev: string): string {
  if (sev === 'error' || sev === 'critical') {
    return sev === 'critical' ? t('semSevCritical', ui.lang) : t('semSevError', ui.lang)
  }
  if (sev === 'info') return t('semSevInfo', ui.lang)
  return t('semSevWarning', ui.lang)
}
function severityPill(sev: string): string {
  if (sev === 'error') return 'pill-danger'
  if (sev === 'critical') return 'pill-danger'
  if (sev === 'warning') return 'pill-warn'
  return 'pill-neutral'
}
function relTime(iso?: string | null): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diff = Date.now() - then
  if (diff < 60_000) return t('semRelJustNow', ui.lang)
  const mins = Math.floor(diff / 60_000)
  if (mins < 60) return t('semRelMinutes', ui.lang, mins)
  const hours = Math.floor(mins / 60)
  if (hours < 24) return t('semRelHours', ui.lang, hours)
  const days = Math.floor(hours / 24)
  if (days <= 30) return t('semRelDays', ui.lang, days)
  return fmtDateTime(iso)
}

const lastCommit = computed<SemanticHistoryEntry | null>(() => history.value[0] ?? null)

/* ── problems panel ───────────────────────────────────────────── */

const visibleIssues = computed(() =>
  values.prob === 'error'
    ? issueItems.value.filter((it) => it.severity === 'error')
    : issueItems.value,
)
const visibleDrift = computed(() =>
  values.prob === 'error'
    ? driftItems.value.filter((it) => it.severity === 'critical' || it.severity === 'error')
    : driftItems.value,
)

function locatable(it: SemanticIssueItem): boolean {
  const k = it.target.kind
  if (k === 'metric') return !!metricByName(it.target.name)
  if (k === 'dataset') return !!datasetByName(it.target.name)
  if (k === 'field') return !!fieldByName(it.target.name)
  return false
}

function locate(it: SemanticIssueItem) {
  const k = it.target.kind
  if (k === 'metric') {
    switchTab('metrics')
    values.q = it.target.name
  } else if (k === 'dataset') {
    switchTab('datasets')
    values.q = it.target.name
  } else if (k === 'field') {
    const f = fieldByName(it.target.name)
    switchTab('fields')
    values.q = f ? f.name : it.target.name
  }
}

function metricByName(name: string): SemanticMetricInfo | undefined {
  return metrics.value.find((m) => m.name === name)
}
function datasetByName(name: string): SemanticDatasetInfo | undefined {
  return datasets.value.find((d) => d.name === name)
}
function fieldByName(target: string): FieldRow | undefined {
  return fields.value.find((f) => `${f.dataset}.${f.name}` === target)
}

const expandedImpact = ref<Set<string>>(new Set())
function driftKey(item: SemanticDriftItem, i: number): string {
  return item.drift_id != null ? `d${item.drift_id}` : `${item.level}:${item.subject}:${i}`
}
function toggleImpact(item: SemanticDriftItem, i: number) {
  const key = driftKey(item, i)
  const next = new Set(expandedImpact.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedImpact.value = next
}
const IMPACT_GROUPS = ['metrics', 'examples', 'rules', 'lessons'] as const
function impactOf(item: SemanticDriftItem, group: (typeof IMPACT_GROUPS)[number]): string[] {
  return item.impact?.[group] ?? []
}
function impactEmpty(item: SemanticDriftItem): boolean {
  return IMPACT_GROUPS.every((g) => impactOf(item, g).length === 0)
}
function impactLabel(group: (typeof IMPACT_GROUPS)[number]): string {
  if (group === 'metrics') return t('semMetrics', ui.lang)
  if (group === 'examples') return t('kbExamples', ui.lang)
  if (group === 'rules') return t('kbRules', ui.lang)
  return t('kbKpiLessons', ui.lang)
}

function driftDetailText(item: SemanticDriftItem): string {
  const d = item.detail || {}
  const problems = typeof d.problems === 'string' ? d.problems : ''
  const note = typeof d.note === 'string' ? d.note : ''
  if (problems || note) return [problems, note].filter(Boolean).join(' — ')
  const extra = [d.dataset, d.field, d.key, d.relationship]
    .filter((v): v is string => typeof v === 'string' && !!v)
  return extra.join(' · ')
}

/** The drift subject resolves to an asset the drawer can open (fix action). */
function driftAsset(item: SemanticDriftItem): { kind: Kind; name: string } | null {
  const s = item.subject
  if (fieldByName(s)) return { kind: 'field', name: s }
  if (datasetByName(s)) return { kind: 'dataset', name: s }
  if (metricByName(s)) return { kind: 'metric', name: s }
  return null
}
function fixDrift(item: SemanticDriftItem) {
  const target = driftAsset(item)
  if (target) openAsset(target.kind, target.name)
}

/* ── selection / batch ────────────────────────────────────────── */

const selected = ref<string[]>([])
const bulkBusy = ref(false)
const bulkAction = ref<'confirm' | 'reject'>('confirm')
const batchResults = ref<SemanticBatchResult | null>(null)

const batchFailures = computed(() => (batchResults.value?.results ?? []).filter((r) => !r.ok))
const batchApplied = computed(() => batchResults.value?.applied ?? 0)

function draftNameOf(id: string): string {
  const all = [...pending.value, ...applied.value, ...rejected.value]
  return all.find((d) => d.id === id)?.name ?? id
}

async function runBatch(action: 'confirm' | 'reject', ids: string[]) {
  if (!ids.length || !values.ds) return
  bulkBusy.value = true
  try {
    const body = await apiPost<SemanticBatchResult>(
      `/v1/admin/semantic/${encodeURIComponent(values.ds)}/drafts/batch`,
      { ids, action },
    )
    // Merge per-item results: a retry replaces only its own row.
    const previous = batchResults.value?.results ?? []
    const fresh = new Map(body.results.map((r) => [r.id, r]))
    const merged = [
      ...previous.filter((r) => !fresh.has(r.id)).map((r) => (r.ok ? r : r)),
      ...body.results,
    ]
    const applied = merged.filter((r) => r.ok).length
    const failed = merged.filter((r) => !r.ok).length
    batchResults.value = { results: merged, applied, failed }
    if (failed === 0) {
      notifySuccess(
        action === 'confirm'
          ? t('semBulkApplied', ui.lang, applied)
          : t('semBulkRejected', ui.lang, applied),
      )
      selected.value = []
    } else {
      selected.value = merged.filter((r) => !r.ok).map((r) => r.id)
    }
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    bulkBusy.value = false
  }
}

function retryBatchItem(id: string) {
  void runBatch(bulkAction.value, [id])
}

/* ── confirm dialog (batch / reject-one / delete) ─────────────── */

type ConfirmCtx =
  | { mode: 'batch'; action: 'confirm' | 'reject' }
  | { mode: 'single'; draft: SemanticDraft }
  | { mode: 'change'; change: SemanticChangeDetail }
  | { mode: 'delete'; kind: Kind; name: string }

const confirmCtx = ref<ConfirmCtx | null>(null)
const confirmBusy = ref(false)
const confirmOpen = computed({
  get: () => !!confirmCtx.value,
  set: (v: boolean) => {
    if (!v) confirmCtx.value = null
  },
})
const confirmTitle = computed(() => {
  const ctx = confirmCtx.value
  if (!ctx) return ''
  if (ctx.mode === 'batch') {
    const n = selected.value.length
    return ctx.action === 'confirm'
      ? t('semConfirmApplyTitle', ui.lang, n)
      : t('semConfirmRejectTitle', ui.lang, n)
  }
  if (ctx.mode === 'single') return t('semConfirmRejectTitle', ui.lang, 1)
  if (ctx.mode === 'change') return `${t('semChangeReject', ui.lang)} · ${ctx.change.id}`
  return t('semDeleteAssetTitle', ui.lang, ctx.name)
})
const confirmTextLabel = computed(() => {
  const ctx = confirmCtx.value
  if (!ctx) return ''
  if (ctx.mode === 'batch') {
    return ctx.action === 'confirm' ? t('semBulkApply', ui.lang) : t('semBulkReject', ui.lang)
  }
  if (ctx.mode === 'single') return t('semRejectDraft', ui.lang)
  if (ctx.mode === 'change') return t('semChangeReject', ui.lang)
  return t('semDelete', ui.lang)
})
const confirmDanger = computed(() => {
  const ctx = confirmCtx.value
  if (!ctx) return false
  if (ctx.mode === 'batch') return ctx.action === 'reject'
  return true
})
const confirmImpactNames = computed(() => {
  const ctx = confirmCtx.value
  if (!ctx) return []
  if (ctx.mode === 'batch') return selected.value.map(draftNameOf)
  if (ctx.mode === 'single') return [ctx.draft.name]
  if (ctx.mode === 'change') {
    const names = (ctx.change.subjects ?? []).map((s) => `${s.kind}:${s.name}`)
    return names.length ? names : [ctx.change.id]
  }
  return [ctx.name]
})

async function runConfirm() {
  const ctx = confirmCtx.value
  if (!ctx) return
  confirmBusy.value = true
  try {
    if (ctx.mode === 'batch') {
      bulkAction.value = ctx.action
      await runBatch(ctx.action, [...selected.value])
      confirmCtx.value = null
    } else if (ctx.mode === 'single') {
      await apiPost(
        `/v1/admin/semantic/${encodeURIComponent(values.ds)}/drafts/${encodeURIComponent(ctx.draft.id)}/reject`,
      )
      notifySuccess(t('semDraftRejected', ui.lang))
      confirmCtx.value = null
      values.draft = ''
      await loadDetail()
    } else if (ctx.mode === 'change') {
      const reason = changeRejectReason.value.trim()
      if (!reason) {
        // 服务端也拒空理由 —— 前端先把这一步挡住,不发必被 400 的请求
        changeRejectError.value = t('semChangeRejectReason', ui.lang)
        return
      }
      changeAction.value = 'reject'
      try {
        await apiPost(
          `/v1/admin/semantic/${encodeURIComponent(values.ds)}/changes/${encodeURIComponent(ctx.change.id)}/reject`,
          { reason },
        )
        notifySuccess(t('semChangeRejectedOk', ui.lang))
        confirmCtx.value = null
        values.change = ''
        await Promise.all([loadChanges(), loadDetail()])
      } finally {
        changeAction.value = null
      }
    } else {
      const ok = await createDraft(ctx.kind, 'delete', ctx.name, {}, '')
      if (ok) confirmCtx.value = null
    }
  } catch (e) {
    toastError(e)
  } finally {
    confirmBusy.value = false
  }
}

/* ── draft creation / drawer ──────────────────────────────────── */

function draftsUrl(): string {
  return `/v1/admin/semantic/${encodeURIComponent(values.ds)}`
}

async function createDraft(
  kind: Kind,
  action: 'upsert' | 'delete',
  name: string,
  payload: Record<string, unknown>,
  note = '',
): Promise<boolean> {
  try {
    await apiPost(`${draftsUrl()}/drafts`, { kind, action, name, payload, note })
    notifySuccess(t('semDraftCreated', ui.lang))
    await loadDetail()
    return true
  } catch (e) {
    toastError(e)
    return false
  }
}

async function validatePayload(
  kind: Kind,
  action: 'upsert' | 'delete',
  name: string,
  payload: Record<string, unknown>,
): Promise<SemanticValidateResult> {
  return apiPost<SemanticValidateResult>(`${draftsUrl()}/validate`, { kind, action, name, payload })
}

const draftSel = computed<SemanticDraft | null>(() => {
  if (!values.draft) return null
  const all = [...pending.value, ...applied.value, ...rejected.value]
  return all.find((d) => d.id === values.draft) ?? null
})
const draftOpen = computed({
  get: () => !!draftSel.value,
  set: (v: boolean) => {
    if (!v) values.draft = ''
  },
})

const draftCheck = ref<SemanticValidateResult | null>(null)
const draftCheckError = ref('')
const draftChecking = ref(false)

watch(draftSel, async (d) => {
  draftCheck.value = null
  draftCheckError.value = ''
  if (!d) return
  await runDraftCheck(d)
})

async function runDraftCheck(d: SemanticDraft) {
  draftChecking.value = true
  draftCheckError.value = ''
  try {
    draftCheck.value = await validatePayload(
      (d.kind as Kind) || 'metric',
      (d.action as 'upsert' | 'delete') || 'upsert',
      d.name,
      d.payload ?? {},
    )
  } catch (e) {
    draftCheckError.value = errMsg(e)
  } finally {
    draftChecking.value = false
  }
}

const draftApplyable = computed(() => !!draftCheck.value?.ok && !draftChecking.value)

async function applyDraft() {
  const d = draftSel.value
  if (!d || !draftApplyable.value) return
  try {
    await apiPost(
      `/v1/admin/semantic/${encodeURIComponent(values.ds)}/drafts/${encodeURIComponent(d.id)}/confirm`,
    )
    notifySuccess(t('semDraftConfirmed', ui.lang))
    values.draft = ''
    await loadDetail()
  } catch (e) {
    toastError(e)
  }
}

function openDraftDrawer(d: SemanticDraft) {
  values.draft = d.id
}

/* ── changes（变更评审）─────────────────────────────────────── */

const changes = ref<SemanticChangeRecord[]>([])
const changesLoaded = ref(false)
const changeDetail = ref<SemanticChangeDetail | null>(null)
const changeAction = ref<'merge' | 'verify' | 'reject' | null>(null)
const changesLoading = ref(false)
const changeRejectReason = ref('')
const changeRejectError = ref('')

async function loadChanges() {
  if (!values.ds) return
  changesLoading.value = true
  try {
    changes.value = (await apiGet<{ changes: SemanticChangeRecord[] }>(
      `/v1/admin/semantic/${encodeURIComponent(values.ds)}/changes`)).changes ?? []
    changesLoaded.value = true
  } catch (e) {
    toastError(e)
  } finally {
    changesLoading.value = false
  }
}

const openChanges = computed(() => changes.value.filter((c) => c.status === 'open'))

watch(
  () => [values.ds, values.tab],
  () => {
    if (!values.ds) return
    // 徽标显示未合并数 —— 没加载过的列表渲染成 0 与「没有未合并变更」同形,
    // 所以列表跟着数据源走(进入 tab 再取一次保鲜)。
    if (values.tab === 'changes' || !changesLoaded.value) void loadChanges()
  },
  { immediate: true },
)

// 详情跟随 (变更 id, 数据源) 双键:composable 的 immediate watcher 先把 URL 里的
// `change` 灌进 values(在本次 setup 之前),immediate 缺省会让深链首绘永远不发详情请求;
// ds 切换时 resetViewState 清了 changeDetail 但 id 没变,不带 ds 键同样不重取。
watch(
  [() => values.change, () => values.ds],
  async ([id]) => {
    changeDetail.value = null
    if (!id) return
    const ds = values.ds
    if (!ds) return
    try {
      changeDetail.value = (await apiGet<{ change: SemanticChangeDetail }>(
        `/v1/admin/semantic/${encodeURIComponent(ds)}/changes/${encodeURIComponent(id)}`,
      )).change
    } catch (e) {
      toastError(e)
    }
  },
  { immediate: true },
)

const changeOpen = computed({
  get: () => !!values.change,
  set: (v: boolean) => {
    if (!v) values.change = ''
  },
})

function openChangeRow(row: unknown) {
  values.change = (row as SemanticChangeRecord).id
}

function changeStatusLabel(status: string): string {
  const map: Record<
    string,
    'semChangeOpen' | 'semChangeMerged' | 'semChangeRejected' | 'semChangeStale' | 'semChangeApproved'
  > = {
    open: 'semChangeOpen',
    merged: 'semChangeMerged',
    rejected: 'semChangeRejected',
    stale: 'semChangeStale',
    approved: 'semChangeApproved',
  }
  const key = map[status]
  // 认不出的状态是数据不是文案,原样带出(色调已按 danger/muted 表态)
  return key ? t(key, ui.lang) : status || '—'
}

/** tone → pill 类名（'muted' 没有对应的 pill-muted,落中性色）。 */
function tonePill(tone: 'ok' | 'warn' | 'danger' | 'muted'): string {
  return tone === 'muted' ? 'pill-neutral' : `pill-${tone}`
}

const changeSummary = (c: SemanticChangeRecord): string => c.note || c.question || c.id

const changeImpact = computed(() =>
  changeDetail.value?.impact
    ? impactLines(changeDetail.value.impact, (k) => t(k as I18nKey, ui.lang))
    : [],
)

/** 实体级 diff(按 kind 一行:增/删/改名字)。 */
const changeDiffEntities = computed<
  Array<{ kind: string; added: string[]; removed: string[]; modified: string[] }>
>(() =>
  Object.entries(changeDetail.value?.diff?.entities ?? {}).map(([kind, e]) => ({
    kind,
    added: e.added ?? [],
    removed: e.removed ?? [],
    modified: e.modified ?? [],
  })),
)

const changeDiffDetails = computed(() => changeDetail.value?.diff?.details ?? [])

/** verdict 的色调:improves 绿 / regresses 红 / neutral 中性 / 其余琥珀 ——
 *  「判不了」与「无可回放产物」都不许渲染成通过。 */
function verifyPill(verdict: string): string {
  if (verdict === 'improves') return 'pill-ok'
  if (verdict === 'regresses') return 'pill-danger'
  if (verdict === 'neutral') return 'pill-neutral'
  return 'pill-warn'
}

/** 已知原因码译人话;认不出的码原样带出(不吞也不编)。 */
function changeWarningLabel(code: string): string {
  return code === 'open_drift' ? t('semChangeWarnDrift', ui.lang) : code
}
const DEGRADED_KEYS: Record<string, I18nKey> = {
  drift_unavailable: 'semChangeDegradedDrift',
  snapshot_missing: 'semChangeDegradedSnapshot',
  impact_unavailable: 'semChangeDegradedImpact',
}
function changeDegradedLabel(code: string): string {
  const key = DEGRADED_KEYS[code]
  return key ? t(key, ui.lang) : code
}

function subjectLabel(i: number): string {
  const s = changeDetail.value?.subjects?.[i]
  return s ? `${s.kind}:${s.name}` : ''
}

function payloadJson(p: Record<string, unknown>): string {
  try {
    return JSON.stringify(p, null, 2)
  } catch {
    return String(p)
  }
}

async function mergeChange(rec: SemanticChangeDetail) {
  changeAction.value = 'merge'
  try {
    await apiPost(`/v1/admin/semantic/${encodeURIComponent(values.ds)}/changes/${encodeURIComponent(rec.id)}/merge`)
    notifySuccess(t('semChangeMergedOk', ui.lang))
    values.change = ''
    await Promise.all([loadChanges(), loadDetail()])
  } catch (e) {
    // 409 stale_change 的 message 逐字由 http 层带出(detail 是 {code,message} 对象)
    toastError(e)
  } finally {
    changeAction.value = null
  }
}

async function runChangeVerify(rec: SemanticChangeDetail) {
  changeAction.value = 'verify'
  try {
    const out = await apiPost<{ verification: SemanticChangeDetail['verification'] }>(
      `/v1/admin/semantic/${encodeURIComponent(values.ds)}/changes/${encodeURIComponent(rec.id)}/verify`)
    if (changeDetail.value) changeDetail.value.verification = out.verification
  } catch (e) {
    toastError(e)
  } finally {
    changeAction.value = null
  }
}

/** 驳回要先有理由(服务端也拒空理由)——理由输入挂在确认对话框里。 */
function askRejectChange(rec: SemanticChangeDetail) {
  changeRejectReason.value = ''
  changeRejectError.value = ''
  confirmCtx.value = { mode: 'change', change: rec }
}

/* ── asset drawer ─────────────────────────────────────────────── */

const assetKey = ref<{ kind: Kind; name: string } | null>(null)
const assetOpen = computed({
  get: () => !!assetKey.value,
  set: (v: boolean) => {
    if (!v) {
      assetKey.value = null
      closeEditor()
    }
  },
})

type AssetSel =
  | { kind: 'metric'; metric: SemanticMetricInfo }
  | { kind: 'dataset'; dataset: SemanticDatasetInfo }
  | { kind: 'field'; field: SemanticFieldInfo; dataset: string }

const assetSel = computed<AssetSel | null>(() => {
  const k = assetKey.value
  if (!k) return null
  if (k.kind === 'metric') {
    const m = metricByName(k.name)
    return m ? { kind: 'metric', metric: m } : null
  }
  if (k.kind === 'dataset') {
    const d = datasetByName(k.name)
    return d ? { kind: 'dataset', dataset: d } : null
  }
  const f = fieldByName(k.name)
  if (!f) return null
  return { kind: 'field', field: f, dataset: f.dataset }
})

const assetName = computed(() => {
  const a = assetSel.value
  if (!a) return ''
  if (a.kind === 'metric') return a.metric.name
  if (a.kind === 'dataset') return a.dataset.name
  return `${a.dataset}.${a.field.name}`
})
const assetKindLabel = computed(() => {
  const a = assetSel.value
  return a ? kindLabel(a.kind) : ''
})
const assetExpression = computed(() => {
  const a = assetSel.value
  if (!a) return ''
  if (a.kind === 'metric') return a.metric.expression ?? ''
  if (a.kind === 'field') return a.field.expression ?? ''
  return ''
})
const assetDescription = computed(() => {
  const a = assetSel.value
  if (!a) return ''
  if (a.kind === 'metric') return a.metric.definition ?? ''
  if (a.kind === 'field') return a.field.description ?? ''
  return a.dataset.description ?? ''
})
const assetAnchors = computed<string[]>(() => {
  const a = assetSel.value
  if (!a) return []
  if (a.kind === 'metric') return a.metric.datasets ?? []
  if (a.kind === 'field') return [a.dataset]
  return [a.dataset.source ?? a.dataset.name]
})
const enumDisplay = computed<[string, string][]>(() => {
  const a = assetSel.value
  if (!a || a.kind !== 'field') return []
  return Object.entries(a.field.enum_display ?? {})
})
const valueAliases = computed<[string, string[]][]>(() => {
  const a = assetSel.value
  if (!a || a.kind !== 'field') return []
  return Object.entries(a.field.value_aliases ?? {})
})

function openAsset(kind: Kind, name: string) {
  assetKey.value = { kind, name }
  closeEditor()
  void ensureTrail()
}
function openMetricRow(row: unknown) {
  const m = row as SemanticMetricInfo
  openAsset('metric', m.name)
}
function openFieldRow(row: unknown) {
  const f = row as FieldRow
  openAsset('field', `${f.dataset}.${f.name}`)
}
function openDatasetRow(row: unknown) {
  const d = row as SemanticDatasetInfo
  openAsset('dataset', d.name)
}

/* ── references ("who uses this") ─────────────────────────────── */

interface RefItem {
  kind: Kind
  name: string
  /** False when the name resolves to no asset (example questions, rule and
   *  lesson texts) — rendered as plain text, not a dead-end link. */
  openable: boolean
}

/** Resolve a name mentioned by the model/drift records to a drawer-openable asset. */
function refOf(name: string): RefItem {
  if (metricByName(name)) return { kind: 'metric', name, openable: true }
  if (datasetByName(name)) return { kind: 'dataset', name, openable: true }
  if (fieldByName(name)) return { kind: 'field', name, openable: true }
  return { kind: 'metric', name, openable: false }
}

const usedBy = computed<{ label: string; items: RefItem[] }[]>(() => {
  const a = assetSel.value
  if (!a) return []
  const name = assetName.value
  // accumulate per label so two evidence sources can't emit duplicate groups
  const groups = new Map<string, RefItem[]>()
  const add = (label: string, items: RefItem[]) => {
    const seen = groups.get(label) ?? []
    for (const it of items) if (!seen.some((s) => s.name === it.name)) seen.push(it)
    groups.set(label, seen)
  }
  // Drift blast-radius snapshots are the one place references are
  // materialized server-side: for the drifted asset itself, all four groups
  // (metrics / examples / rules / lessons) are evidence of "who uses it".
  for (const it of driftItems.value) {
    if (it.subject !== name) continue
    for (const g of IMPACT_GROUPS) {
      const items = impactOf(it, g)
      if (items.length) add(impactLabel(g), items.map(refOf))
    }
  }
  if (a.kind === 'metric') {
    // findings whose blast radius names this metric; the drifted asset is the referrer
    const hits = driftItems.value
      .filter((it) => impactOf(it, 'metrics').includes(name))
      .map((it) => refOf(it.subject))
    if (hits.length) add(t('semDriftSection', ui.lang), hits)
  } else if (a.kind === 'dataset') {
    const anchored = metrics.value
      .filter((m) => (m.datasets ?? []).includes(a.dataset.name))
      .map((m) => ({ kind: 'metric' as const, name: m.name, openable: true }))
    if (anchored.length) add(t('semMetrics', ui.lang), anchored)
  } else {
    // fields have no anchoring index server-side; text match in expressions is declared as such
    const needle = `${a.dataset}.${a.field.name}`
    const refs = metrics.value
      .filter((m) => `${m.expression ?? ''} ${m.filter ?? ''}`.includes(needle))
      .map((m) => ({ kind: 'metric' as const, name: m.name, openable: true }))
    if (refs.length) add(t('semUsedByExpression', ui.lang), refs)
  }
  return [...groups.entries()].map(([label, items]) => ({ label, items }))
})

function openRef(item: RefItem) {
  if (!item.openable) return
  openAsset(item.kind, item.name)
}

/* ── last-changed (audit-derived) ─────────────────────────────── */

/**
 * Latest recorded change per asset, from the workbench audit trail —
 * the only per-asset timestamp the server has. Basis is stated in the UI:
 * edits made straight to semantics.yml (git commits) are not counted, and
 * "—" means "no record", not "never changed".
 */
const changedAt = computed<Map<string, string>>(() => {
  const m = new Map<string, string>()
  for (const a of audit.value) {
    const name = String(a.details?.name ?? '')
    if (!name || !a.ts) continue
    const prev = m.get(name)
    if (!prev || a.ts > prev) m.set(name, a.ts)
  }
  return m
})

function changedCell(name: string): { text: string; title: string } {
  const at = changedAt.value.get(name)
  if (at) return { text: relTime(at), title: fmtDateTime(at) }
  return { text: '—', title: t('semChangedBasis', ui.lang) }
}
function metricChanged(row: unknown): { text: string; title: string } {
  return changedCell((row as SemanticMetricInfo).name)
}
function fieldChanged(row: unknown): { text: string; title: string } {
  const f = row as FieldRow
  return changedCell(`${f.dataset}.${f.name}`)
}
function datasetChanged(row: unknown): { text: string; title: string } {
  return changedCell((row as SemanticDatasetInfo).name)
}
function changedSortKey(name: string): string {
  return changedAt.value.get(name) ?? ''
}

/* ── expression editor (validate → dry run → create draft) ────── */

const editorOpen = ref(false)
const editorExpr = ref('')
const editorBusy = ref<'' | 'validate' | 'preview' | 'create'>('')
const editorCheck = ref<SemanticValidateResult | null>(null)
const editorError = ref('')
const preview = ref<SemanticPreviewResult | null>(null)
const previewMetric = ref('')

const editTarget = computed<{ kind: 'metric' | 'field'; name: string; base: string } | null>(() => {
  const a = assetSel.value
  if (!a) return null
  if (a.kind === 'metric') {
    return { kind: 'metric', name: a.metric.name, base: a.metric.expression ?? '' }
  }
  if (a.kind === 'field') {
    return { kind: 'field', name: `${a.dataset}.${a.field.name}`, base: a.field.expression ?? '' }
  }
  return null
})

function closeEditor() {
  editorOpen.value = false
  editorCheck.value = null
  editorError.value = ''
  preview.value = null
  editorBusy.value = ''
}

function startEdit() {
  const e = editTarget.value
  if (!e) return
  editorExpr.value = e.base
  editorCheck.value = null
  editorError.value = ''
  preview.value = null
  if (previewNeedsMetric.value && !previewMetric.value) {
    previewMetric.value = metrics.value[0]?.name ?? ''
  }
  editorOpen.value = true
}

const previewNeedsMetric = computed(() => editTarget.value?.kind === 'field')
const previewMetricOptions = computed(() => metrics.value.map((m) => m.name))

function editorPayload(): Record<string, unknown> {
  const a = assetSel.value
  const expr = editorExpr.value.trim()
  if (!a) return {}
  if (a.kind === 'metric') {
    return {
      expression: expr,
      synonyms: a.metric.synonyms ?? [],
      datasets: a.metric.datasets ?? [],
      definition: a.metric.definition ?? '',
    }
  }
  if (a.kind === 'field') {
    return {
      expression: expr,
      datatype: a.field.datatype ?? '',
      semantic_role: a.field.semantic_role ?? '',
      synonyms: a.field.synonyms ?? [],
      description: a.field.description ?? '',
      is_time: !!a.field.is_time,
    }
  }
  return {}
}

async function runValidate() {
  const e = editTarget.value
  if (!e) return
  editorBusy.value = 'validate'
  editorError.value = ''
  preview.value = null
  try {
    editorCheck.value = await validatePayload(e.kind, 'upsert', e.name, editorPayload())
  } catch (err) {
    editorCheck.value = null
    editorError.value = errMsg(err)
  } finally {
    editorBusy.value = ''
  }
}

async function runPreview() {
  const e = editTarget.value
  if (!e) return
  const metricName = e.kind === 'metric' ? e.name : previewMetric.value
  if (!metricName) return
  editorBusy.value = 'preview'
  editorError.value = ''
  try {
    preview.value = await apiPost<SemanticPreviewResult>(`${draftsUrl()}/preview`, {
      kind: e.kind,
      action: 'upsert',
      name: e.name,
      payload: editorPayload(),
      query: { metrics: [metricName] },
    })
  } catch (err) {
    preview.value = null
    editorError.value = errMsg(err)
  } finally {
    editorBusy.value = ''
  }
}

async function createFromEditor() {
  const e = editTarget.value
  if (!e) return
  editorBusy.value = 'create'
  editorError.value = ''
  try {
    const ok = await createDraft(e.kind, 'upsert', e.name, editorPayload(), '')
    if (ok) {
      closeEditor()
    }
  } finally {
    editorBusy.value = ''
  }
}

/* ── history + audit trail ────────────────────────────────────── */

const history = ref<SemanticHistoryEntry[]>([])
const audit = ref<AuditEntry[]>([])
const historyLoaded = ref(false)
const trailError = ref(false)

async function ensureTrail() {
  if (historyLoaded.value) return
  historyLoaded.value = true
  trailError.value = false
  const ds = values.ds
  try {
    const body = await apiGet<{ history: SemanticHistoryEntry[] }>(
      `/v1/admin/semantic/${encodeURIComponent(ds)}/history?limit=20`,
    )
    if (values.ds === ds) history.value = body.history ?? []
  } catch {
    if (values.ds === ds) trailError.value = true
  }
  try {
    const body = await apiGet<{ audit: AuditEntry[] }>('/v1/admin/audit?limit=200')
    if (values.ds === ds) {
      audit.value = (body.audit ?? []).filter((a) => {
        if (!(a.action ?? '').startsWith('semantic.draft.')) return false
        const src = a.details?.datasource
        return src === undefined || String(src) === ds
      })
    }
  } catch {
    /* audit is best-effort; the git history below still renders */
  }
}

function retryTrail() {
  historyLoaded.value = false
  void ensureTrail()
}

const assetAudit = computed(() => {
  const name = assetName.value
  if (!name) return []
  return audit.value.filter((a) => String(a.details?.name ?? '') === name).slice(0, 5)
})

/* ── copy ─────────────────────────────────────────────────────── */

async function copyValue(text: string) {
  if (!text) return
  const ok = await copyText(text)
  if (ok) notifySuccess(t('copied', ui.lang))
}

/* ── new-asset dialogs ────────────────────────────────────────── */

const menuOpen = ref(false)
function onDocumentClick(e: MouseEvent) {
  if (menuOpen.value && !(e.target as HTMLElement)?.closest?.('.sem-more')) menuOpen.value = false
}
function onDocumentKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') menuOpen.value = false
}
onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onDocumentKeydown)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onDocumentKeydown)
})

function splitCsv(v: string): string[] {
  return v
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** Local (pre-flight) issue rows reuse the server shape so one renderer fits. */
function localIssue(code: 'name' | 'expression', message: string): SemanticIssueItem {
  return { severity: 'error', code, target: { kind: '', name: '' }, message, hint: '' }
}

/** code → dialog field: the mapping the server contract promises (stable codes). */
function issueFieldOf(code: string): string {
  if (code === 'name' || code === 'expression') return code
  if (['dup_metric', 'dup_field', 'dataset_undeclared', 'field_target_invalid', 'kind_unknown', 'dataset_unknown'].includes(code)) return 'name'
  if (['expr_parse', 'expr_missing', 'filter_parse', 'filter_ref', 'enum_baked', 'agg_time_dimension', 'mask_invalid', 'mask_hash_without_salt', 'non_additive'].includes(code)) return 'expression'
  if (code === 'datatype_invalid') return 'datatype'
  if (code === 'bad_synonym') return 'synonyms'
  if (code === 'unique_keys') return 'primary_key'
  return ''
}
function fieldErrorOf(issues: SemanticIssueItem[], field: string): string {
  return issues
    .filter((it) => issueFieldOf(it.code) === field)
    .map((it) => it.message)
    .join('；')
}
function generalIssues(issues: SemanticIssueItem[]): SemanticIssueItem[] {
  return issues.filter((it) => !issueFieldOf(it.code))
}

const metricOpen = ref(false)
const metricIssues = ref<SemanticIssueItem[]>([])
const metricBusy = ref(false)
const metricForm = reactive({
  name: '',
  expression: '',
  synonyms: '',
  datasets: [] as string[],
  definition: '',
  note: '',
})
function openMetricDialog() {
  menuOpen.value = false
  metricIssues.value = []
  blueprintNote.value = '' // a plain open is never a blueprint prefill
  Object.assign(metricForm, { name: '', expression: '', synonyms: '', datasets: [], definition: '', note: '' })
  metricOpen.value = true
}
async function saveMetric() {
  const f = metricForm
  metricIssues.value = []
  if (!f.name.trim()) metricIssues.value = [localIssue('name', t('semNameRequired', ui.lang))]
  else if (!f.expression.trim()) metricIssues.value = [localIssue('expression', t('semExpressionRequired', ui.lang))]
  if (metricIssues.value.length) return
  const payload = {
    expression: f.expression.trim(),
    synonyms: splitCsv(f.synonyms),
    datasets: [...f.datasets],
    definition: f.definition.trim(),
  }
  metricBusy.value = true
  try {
    const check = await validatePayload('metric', 'upsert', f.name.trim(), payload)
    if (!check.ok) {
      metricIssues.value = [...check.errors, ...check.warnings]
      return
    }
    if (await createDraft('metric', 'upsert', f.name.trim(), payload, f.note.trim())) {
      metricOpen.value = false
    }
  } catch (e) {
    toastError(e)
  } finally {
    metricBusy.value = false
  }
}

const fieldOpen = ref(false)
const fieldIssues = ref<SemanticIssueItem[]>([])
const fieldBusy = ref(false)
const fieldForm = reactive({
  dataset: '',
  name: '',
  expression: '',
  datatype: '',
  semantic_role: '',
  synonyms: '',
  description: '',
  is_time: false,
  note: '',
})
const ROLE_OPTIONS = ['identifier', 'measure', 'dimension', 'enum', 'time'] as const
function openFieldDialog() {
  menuOpen.value = false
  fieldIssues.value = []
  blueprintNote.value = '' // a plain open is never a blueprint prefill
  Object.assign(fieldForm, {
    dataset: datasetNames.value[0] ?? '',
    name: '',
    expression: '',
    datatype: '',
    semantic_role: '',
    synonyms: '',
    description: '',
    is_time: false,
    note: '',
  })
  fieldOpen.value = true
}
async function saveField() {
  const f = fieldForm
  fieldIssues.value = []
  if (!f.dataset || !f.name.trim()) fieldIssues.value = [localIssue('name', t('semNameRequired', ui.lang))]
  else if (!f.expression.trim()) fieldIssues.value = [localIssue('expression', t('semExpressionRequired', ui.lang))]
  if (fieldIssues.value.length) return
  const target = `${f.dataset}.${f.name.trim()}`
  const payload = {
    expression: f.expression.trim(),
    datatype: f.datatype.trim(),
    semantic_role: f.semantic_role,
    synonyms: splitCsv(f.synonyms),
    description: f.description.trim(),
    is_time: f.is_time,
  }
  fieldBusy.value = true
  try {
    const check = await validatePayload('field', 'upsert', target, payload)
    if (!check.ok) {
      fieldIssues.value = [...check.errors, ...check.warnings]
      return
    }
    if (await createDraft('field', 'upsert', target, payload, f.note.trim())) {
      fieldOpen.value = false
    }
  } catch (e) {
    toastError(e)
  } finally {
    fieldBusy.value = false
  }
}

/* ── blueprint prefill (A2) ───────────────────────────────────────
   A conflicted draft cannot be confirmed, so the refusal card offers
   "create from this draft": the backend carries the draft's content in
   the action's `payload`, NextActions relays it mechanically as bp_*
   query params, and this page owns the meaning of every key. The params
   live exactly one navigation — the dialog opens prefilled and the keys
   are dropped from the URL immediately, so a refresh or a back-button
   never replays a half-consumed state. */

interface Blueprint {
  kind: string
  name: string
  expression: string
  datasets: string[]
  synonyms: string[]
  definition: string
  datatype: string
  /** Conflict diagnostic (server text, transcribed verbatim — data, not copy). */
  note: string
  /** confirm_draft deep link: open the draft drawer instead of a dialog. */
  draftId: string
}

const blueprintNote = ref('')

function bpParam(key: string): string {
  const raw = route.query[`bp_${key}`]
  return Array.isArray(raw) ? String(raw[raw.length - 1] ?? '') : raw == null ? '' : String(raw)
}

/** Lists travel comma-joined (NextActions joins arrays the same way). */
function bpList(key: string): string[] {
  return bpParam(key).split(',').filter(Boolean)
}

function clearBlueprintParams() {
  const next = { ...route.query }
  let dirty = false
  for (const key of Object.keys(next)) {
    if (key.startsWith('bp_')) {
      delete next[key]
      dirty = true
    }
  }
  // Best effort, like useListQuery's own writes: a dropped navigation
  // must never break the page.
  if (dirty) {
    void Promise.resolve(router.replace({ query: next, hash: route.hash })).catch(() => {})
  }
}

function applyBlueprint(bp: Blueprint) {
  if (bp.draftId) {
    values.draft = bp.draftId
    return
  }
  if (bp.kind === 'metric') {
    openMetricDialog()
    metricForm.name = bp.name
    metricForm.expression = bp.expression
    metricForm.datasets = [...bp.datasets]
    metricForm.synonyms = bp.synonyms.join(', ')
    metricForm.definition = bp.definition
  } else if (bp.kind === 'field') {
    openFieldDialog()
    const dot = bp.name.lastIndexOf('.')
    if (dot > 0) {
      fieldForm.dataset = bp.name.slice(0, dot)
      fieldForm.name = bp.name.slice(dot + 1)
    } else {
      // The name is not dataset.field (one of the conflict causes): never
      // guess a dataset — leave the select empty so the admin picks one.
      fieldForm.dataset = ''
      fieldForm.name = bp.name
    }
    fieldForm.expression = bp.expression
    fieldForm.datatype = bp.datatype
    fieldForm.synonyms = bp.synonyms.join(', ')
    fieldForm.description = bp.definition
  } else {
    return // unknown kind: nothing to prefill (params still get cleared)
  }
  blueprintNote.value = bp.note
}

/** The URL half: refuse → next_actions → bp_* (kind/name/expression/
 *  datasets/conflict_message; the richer draft payload fields — synonyms,
 *  definition, datatype — only exist on the in-console drawer path). */
function consumeBlueprint() {
  const kind = bpParam('draft_kind')
  const draftId = bpParam('draft_id')
  if (!kind && !draftId) return
  applyBlueprint({
    kind,
    name: bpParam('draft_name'),
    expression: bpParam('expression'),
    datasets: bpList('datasets'),
    synonyms: bpList('synonyms'),
    definition: bpParam('definition'),
    datatype: bpParam('datatype'),
    note: bpParam('conflict_message'),
    draftId,
  })
  clearBlueprintParams()
}

/** The in-console half: the draft drawer's own "create from this draft". */
function blueprintFromDraft() {
  const d = draftSel.value
  if (!d) return
  const p = (d.payload ?? {}) as Record<string, unknown>
  const str = (v: unknown) => (v == null ? '' : String(v))
  const list = (v: unknown) => (Array.isArray(v) ? v.map(String) : [])
  const bp: Blueprint = {
    kind: str(d.kind),
    name: d.name,
    expression: str(p.expression),
    datasets: list(p.datasets),
    synonyms: list(p.synonyms),
    definition: str(p.definition ?? p.description),
    datatype: str(p.datatype),
    note: str(d.conflict?.message),
    draftId: '',
  }
  applyBlueprint(bp)
  values.draft = '' // close the drawer; the prefilled dialog is the next step
}

// Consume when the page has a loaded model (dataset names, drawer rows)
// and bp_* keys are present. Idempotent: consuming clears the keys.
watch(
  () => [detail.value, route.query.bp_draft_kind, route.query.bp_draft_id] as const,
  () => {
    if (detail.value) consumeBlueprint()
  },
  { immediate: true },
)

const datasetOpen = ref(false)
const datasetIssues = ref<SemanticIssueItem[]>([])
const datasetBusy = ref(false)
const datasetForm = reactive({
  name: '',
  source: '',
  primary_key: '',
  synonyms: '',
  description: '',
  note: '',
})
function openDatasetDialog() {
  menuOpen.value = false
  datasetIssues.value = []
  Object.assign(datasetForm, { name: '', source: '', primary_key: '', synonyms: '', description: '', note: '' })
  datasetOpen.value = true
}
async function saveDataset() {
  const f = datasetForm
  datasetIssues.value = []
  if (!f.name.trim()) {
    datasetIssues.value = [localIssue('name', t('semNameRequired', ui.lang))]
    return
  }
  const payload = {
    source: f.source.trim() || f.name.trim(),
    primary_key: splitCsv(f.primary_key),
    synonyms: splitCsv(f.synonyms),
    description: f.description.trim(),
  }
  datasetBusy.value = true
  try {
    const check = await validatePayload('dataset', 'upsert', f.name.trim(), payload)
    if (!check.ok) {
      datasetIssues.value = [...check.errors, ...check.warnings]
      return
    }
    if (await createDraft('dataset', 'upsert', f.name.trim(), payload, f.note.trim())) {
      datasetOpen.value = false
    }
  } catch (e) {
    toastError(e)
  } finally {
    datasetBusy.value = false
  }
}

function askBatch(action: 'confirm' | 'reject') {
  if (!selected.value.length) return
  confirmCtx.value = { mode: 'batch', action }
}
function askRejectDraft() {
  const d = draftSel.value
  if (d) confirmCtx.value = { mode: 'single', draft: d }
}
function clearSelection() {
  selected.value = []
}
function onSelectedUpdate(keys: (string | number)[]) {
  selected.value = keys.map(String)
}
function onPendingRow(row: unknown) {
  openDraftDrawer(row as SemanticDraft)
}
function fieldRowKey(row: unknown): string {
  const f = row as FieldRow
  return `${f.dataset}.${f.name}`
}
function metricRowKey(row: unknown): string {
  return (row as SemanticMetricInfo).name
}
function datasetRowKey(row: unknown): string {
  return (row as SemanticDatasetInfo).name
}

function askDeleteAsset() {
  const a = assetSel.value
  if (!a) return
  confirmCtx.value = { mode: 'delete', kind: a.kind, name: assetName.value }
}

function goKbInit() {
  void router.push({ name: 'admin-kb', query: { ds: values.ds } })
}

const crumbs = computed(() => [
  { label: t('semCrumbManage', ui.lang), to: { path: '/admin' } },
  { label: t('semCrumbSection', ui.lang) },
  { label: t('semModelTitle', ui.lang) },
])
const pageTitle = computed(() =>
  values.ds ? `${t('semModelTitle', ui.lang)} · ${values.ds}` : t('semModelTitle', ui.lang),
)
</script>

<template>
  <div class="admin-view sem-page">
    <PageHeader :title="pageTitle" :description="t('semanticPageDesc', ui.lang)" :breadcrumbs="crumbs">
      <template #actions>
        <div class="sem-ds-switch" role="group" :aria-label="t('semSelectDs', ui.lang)">
          <button
            v-for="d in connected"
            :key="d.name"
            type="button"
            class="sem-ds-seg"
            :class="{ 'is-active': d.name === values.ds }"
            :aria-pressed="d.name === values.ds ? 'true' : 'false'"
            @click="switchDs(d.name)"
          >
            {{ d.default ? `${d.name} · default` : d.name }}
          </button>
        </div>
        <el-button :loading="driftChecking" :disabled="!values.ds" @click="redetectDrift">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('semRedetect', ui.lang) }}
        </el-button>
        <div class="sem-more">
          <button
            type="button"
            class="sem-new-btn"
            :aria-expanded="menuOpen ? 'true' : 'false'"
            aria-haspopup="menu"
            :disabled="!enabled"
            @click="menuOpen = !menuOpen"
          >
            <Plus :size="15" />
            {{ t('semNewAsset', ui.lang) }}
            <ChevronDown :size="14" />
          </button>
          <div v-if="menuOpen" class="sem-menu" role="menu">
            <button type="button" role="menuitem" class="sem-menu-item" @click="openMetricDialog">
              {{ t('semAddMetric', ui.lang) }}
            </button>
            <button type="button" role="menuitem" class="sem-menu-item" @click="openFieldDialog">
              {{ t('semAddField', ui.lang) }}
            </button>
            <button type="button" role="menuitem" class="sem-menu-item" @click="openDatasetDialog">
              {{ t('semAddDataset', ui.lang) }}
            </button>
          </div>
        </div>
      </template>
    </PageHeader>

    <!-- load failed: the page is cleared, never the previous source's data -->
    <StatePanel
      v-if="loadError && !detail"
      mode="error"
      :title="t('semErrorTitle', ui.lang)"
      :description="t('semErrorDesc', ui.lang)"
      :detail="loadError"
      :retry-text="t('retry', ui.lang)"
      @retry="loadDetail"
    />

    <!-- first paint -->
    <StatePanel
      v-else-if="loading && !detail"
      mode="loading"
      :title="t('semLoading', ui.lang)"
    />

    <!-- no semantic model yet: first-run empty state carries the CTA -->
    <StatePanel
      v-else-if="detail && !enabled"
      mode="empty"
      :title="t('semNotEnabledTitle', ui.lang)"
      :description="t('semNotEnabled', ui.lang)"
    >
      <template #action>
        <el-button type="primary" @click="goKbInit">
          {{ t('semGoKbInit', ui.lang) }}
        </el-button>
      </template>
    </StatePanel>

    <template v-else-if="detail">
      <!-- context strip: counts, last commit, drift checked time (relative + absolute on hover) -->
      <div class="sem-context">
        <span>{{ metrics.length }} {{ t('semMetrics', ui.lang) }}</span>
        <span class="sem-sep">·</span>
        <span>{{ datasets.length }} {{ t('semDatasets', ui.lang) }}</span>
        <span class="sem-sep">·</span>
        <span>{{ fields.length }} {{ t('semFields', ui.lang) }}</span>
        <span class="sem-sep">·</span>
        <span>{{ pending.length }} {{ t('semPending', ui.lang) }}</span>
        <template v-if="lastCommit">
          <span class="sem-sep">·</span>
          <span :title="fmtDateTime(lastCommit.date)">
            {{ t('semLastCommit', ui.lang) }} {{ relTime(lastCommit.date) }}
          </span>
        </template>
        <span class="sem-sep">·</span>
        <span :title="drift?.checked_at ? fmtDateTime(drift.checked_at) : ''">
          {{ t('semDriftCheckedAt', ui.lang) }}
          {{ driftSkipped ? t('semDriftNotChecked', ui.lang) : relTime(drift?.checked_at) }}
        </span>
      </div>

      <!-- KPI row — every number is an entry point -->
      <div class="sem-kpi-row">
        <KpiTile
          :label="t('semMetrics', ui.lang)"
          :value="metrics.length"
          :active="values.tab === 'metrics'"
          @click="applyKpi('metrics')"
        />
        <KpiTile
          :label="t('semDatasets', ui.lang)"
          :value="datasets.length"
          :active="values.tab === 'datasets'"
          @click="applyKpi('datasets')"
        />
        <KpiTile
          :label="t('semFields', ui.lang)"
          :value="fields.length"
          :active="values.tab === 'fields'"
          @click="applyKpi('fields')"
        />
        <KpiTile
          :label="t('semPending', ui.lang)"
          :value="pending.length"
          :active="values.tab === 'pending'"
          @click="applyKpi('pending')"
        />
        <KpiTile
          :label="t('semProblems', ui.lang)"
          :value="problemCount"
          :sub="problemSub"
          :active="problemsActive"
          @click="applyKpi('problems')"
        />
      </div>

      <!-- problems panel: structured lint rows + drift entries with blast radius -->
      <section class="admin-card sem-problems" :aria-label="t('semProblemsTitle', ui.lang)">
        <div class="card-header">
          <h3 class="card-title">{{ t('semProblemsTitle', ui.lang) }}</h3>
          <div class="card-actions">
            <span v-if="problemsActive" class="pill pill-accent">{{ t('semProblems', ui.lang) }}</span>
            <button v-if="filtersActive || problemsActive" type="button" class="link-btn" @click="clearFilters">
              {{ t('semClearFilters', ui.lang) }}
            </button>
          </div>
        </div>

        <!-- the 5th state: a skipped check must not look like "clean" -->
        <div v-if="driftSkipped" class="sem-drift-skipped" role="status">
          <TriangleAlert :size="16" aria-hidden="true" />
          <div class="sds-text">
            <div class="sds-title">{{ t('semDriftNotChecked', ui.lang) }}</div>
            <p class="sds-desc">{{ t('semDriftNotCheckedDesc', ui.lang) }}</p>
            <p class="sds-reason">
              {{ t('semDriftReason', ui.lang) }}:
              <code>{{ drift?.skip_reason || t('semDriftReasonUnknown', ui.lang) }}</code>
            </p>
          </div>
        </div>
        <p v-if="checkError" class="sem-check-error" role="alert">
          <strong>{{ t('semCheckFailed', ui.lang) }}</strong> — {{ t('semCheckFailedDesc', ui.lang) }}
          <code>{{ checkError }}</code>
        </p>

        <div class="sem-problem-group">
          <h4 class="sem-group-title">
            {{ t('semIssues', ui.lang) }} ({{ visibleIssues.length }})
          </h4>
          <ul v-if="visibleIssues.length" class="sem-issue-list">
            <li v-for="(it, i) in visibleIssues" :key="`${it.code}-${i}`" class="sem-issue-row">
              <span class="pill" :class="severityPill(it.severity)">{{ severityLabel(it.severity) }}</span>
              <div class="sem-issue-body">
                <div class="sem-issue-msg">
                  <code v-if="it.target.name" class="sem-issue-target">{{ it.target.kind }} {{ it.target.name }}</code>
                  <span>{{ it.message }}</span>
                </div>
                <p v-if="it.hint" class="sem-issue-hint">{{ t('semHint', ui.lang) }}: {{ it.hint }}</p>
              </div>
              <button v-if="locatable(it)" type="button" class="mini-btn" @click="locate(it)">
                {{ t('semLocate', ui.lang) }}
              </button>
            </li>
          </ul>
          <p v-else class="empty-note">{{ t('semNoIssues', ui.lang) }}</p>
        </div>

        <div class="sem-problem-group">
          <h4 class="sem-group-title">
            {{ t('semDriftSection', ui.lang) }} ({{ visibleDrift.length }})
          </h4>
          <ul v-if="visibleDrift.length" class="sem-drift-list">
            <li v-for="(item, i) in visibleDrift" :key="driftKey(item, i)" class="sem-drift-row">
              <div class="sdr-head">
                <span class="pill pill-neutral">{{ item.level }}</span>
                <span class="pill" :class="severityPill(item.severity)">{{ severityLabel(item.severity) }}</span>
                <code class="sdr-subject">{{ item.subject }}</code>
                <span v-if="item.first_seen_at" class="sdr-life" :title="fmtDateTime(item.first_seen_at)">
                  {{ t('semDriftFirstSeen', ui.lang) }} {{ fmtDateTime(item.first_seen_at) }}
                </span>
                <span v-if="item.seen_count != null" class="sdr-life">
                  {{ t('semDriftSeenCount', ui.lang, item.seen_count) }}
                </span>
                <span class="sdr-spacer" />
                <button
                  type="button"
                  class="mini-btn"
                  :aria-expanded="expandedImpact.has(driftKey(item, i)) ? 'true' : 'false'"
                  @click="toggleImpact(item, i)"
                >
                  {{ t('semDriftImpact', ui.lang) }}
                </button>
                <button
                  v-if="driftAsset(item)"
                  type="button"
                  class="mini-btn primary"
                  @click="fixDrift(item)"
                >
                  {{ t('semDriftFix', ui.lang) }}
                </button>
              </div>
              <p v-if="driftDetailText(item)" class="sdr-detail">{{ driftDetailText(item) }}</p>
              <div v-if="expandedImpact.has(driftKey(item, i))" class="sdr-impact">
                <div
                  v-for="g in IMPACT_GROUPS"
                  v-show="impactOf(item, g).length"
                  :key="g"
                  class="sdr-impact-group"
                >
                  <span class="sdr-impact-label">{{ impactLabel(g) }} ({{ impactOf(item, g).length }})</span>
                  <span class="sdr-impact-items">{{ impactOf(item, g).join(' · ') }}</span>
                </div>
                <p v-if="impactEmpty(item)" class="empty-note">{{ t('semDriftNoImpact', ui.lang) }}</p>
              </div>
            </li>
          </ul>
          <p v-else-if="driftSkipped" class="empty-note">{{ t('semDriftNotChecked', ui.lang) }}</p>
          <p v-else-if="driftItems.length" class="empty-note">{{ t('semFilteredEmpty', ui.lang) }}</p>
          <p v-else-if="driftClean" class="empty-note">{{ t('semDriftClean', ui.lang) }}</p>
          <!-- an unrecognized status is not evidence of cleanliness -->
          <p v-else class="empty-note">{{ t('semDriftNotChecked', ui.lang) }}</p>
        </div>

        <p v-if="!issueItems.length && driftClean" class="empty-note sem-no-problems">
          {{ t('semNoProblems', ui.lang) }}
        </p>
      </section>

      <!-- tabs -->
      <div class="sem-tabs" role="tablist" :aria-label="t('semModelTitle', ui.lang)">
        <button
          v-for="tabDef in tabDefs"
          :key="tabDef.key"
          type="button"
          class="sem-tab"
          role="tab"
          :class="{ 'is-active': values.tab === tabDef.key }"
          :aria-selected="values.tab === tabDef.key ? 'true' : 'false'"
          @click="switchTab(tabDef.key)"
        >
          {{ tabDef.label }}
          <span class="tab-badge">{{ tabDef.count }}</span>
        </button>
      </div>

      <section class="sem-pane">
        <div class="list-toolbar">
          <el-input
            v-model="values.q"
            class="sem-search"
            clearable
            :placeholder="t('semSearchPlaceholder', ui.lang)"
            :aria-label="t('semSearchPlaceholder', ui.lang)"
          >
            <template #prefix>
              <Search :size="14" />
            </template>
          </el-input>
          <span class="spacer" />
          <span class="view-count">{{ t('semItemsCount', ui.lang, currentTotal) }}</span>
        </div>

        <!-- filters cleared: the way out of a filtered-empty list -->
        <StatePanel
          v-if="!currentTotal && filtersActive"
          mode="empty"
          :title="t('semFilteredEmpty', ui.lang)"
        >
          <template #action>
            <el-button size="small" @click="clearFilters">{{ t('semClearFilters', ui.lang) }}</el-button>
          </template>
        </StatePanel>

        <template v-else>
          <!-- ── metrics ── -->
          <DataTable
            v-if="values.tab === 'metrics'"
            :columns="METRIC_COLUMNS"
            :rows="visibleMetrics"
            :row-key="metricRowKey"
            row-clickable
            :sort="sortState"
            :loading="loading"
            :select-all-label="t('semSelectAll', ui.lang)"
            :empty-text="t('semNoMetrics', ui.lang)"
            @row-click="openMetricRow"
            @update:sort="onSort"
          >
            <template #cell-name="{ row }">
              <span class="asset-name">{{ (row as SemanticMetricInfo).name }}</span>
            </template>
            <template #cell-expression="{ row }">
              <span v-if="(row as SemanticMetricInfo).expression" class="cell-expr">
                <code class="cell-mono">{{ trunc((row as SemanticMetricInfo).expression ?? '', 42) }}</code>
                <button
                  type="button"
                  class="mini-btn icon"
                  :aria-label="t('copy', ui.lang)"
                  :title="t('copy', ui.lang)"
                  @click.stop="copyValue((row as SemanticMetricInfo).expression ?? '')"
                >
                  <Copy :size="12" />
                </button>
              </span>
              <span v-else class="cell-muted">—</span>
            </template>
            <template #cell-synonyms="{ row }">
              <span class="cell-muted">{{ ((row as SemanticMetricInfo).synonyms ?? []).join(' · ') || '—' }}</span>
            </template>
            <template #cell-datasets="{ row }">
              <span class="cell-muted">{{ ((row as SemanticMetricInfo).datasets ?? []).join(' · ') || '—' }}</span>
            </template>
            <template #cell-definition="{ row }">
              <span class="cell-muted">{{ trunc((row as SemanticMetricInfo).definition ?? '', 40) || '—' }}</span>
            </template>
            <template #cell-changed="{ row }">
              <span class="cell-muted" :title="metricChanged(row).title">{{ metricChanged(row).text }}</span>
            </template>
          </DataTable>

          <!-- ── datasets ── -->
          <DataTable
            v-else-if="values.tab === 'datasets'"
            :columns="DATASET_COLUMNS"
            :rows="visibleDatasets"
            :row-key="datasetRowKey"
            row-clickable
            :sort="sortState"
            :loading="loading"
            :empty-text="t('semNoDatasets', ui.lang)"
            @row-click="openDatasetRow"
            @update:sort="onSort"
          >
            <template #cell-name="{ row }">
              <span class="asset-name">{{ (row as SemanticDatasetInfo).name }}</span>
            </template>
            <template #cell-source="{ row }">
              <code class="cell-mono">{{ (row as SemanticDatasetInfo).source || '—' }}</code>
            </template>
            <template #cell-primary_key="{ row }">
              <span class="cell-muted">{{ ((row as SemanticDatasetInfo).primary_key ?? []).join(' · ') || '—' }}</span>
            </template>
            <template #cell-fieldsCount="{ row }">
              <span class="cell-mono">{{ ((row as SemanticDatasetInfo).fields ?? []).length }}</span>
            </template>
            <template #cell-description="{ row }">
              <span class="cell-muted">{{ trunc((row as SemanticDatasetInfo).description ?? '', 40) || '—' }}</span>
            </template>
            <template #cell-changed="{ row }">
              <span class="cell-muted" :title="datasetChanged(row).title">{{ datasetChanged(row).text }}</span>
            </template>
          </DataTable>

          <!-- ── fields ── -->
          <DataTable
            v-else-if="values.tab === 'fields'"
            :columns="FIELD_COLUMNS"
            :rows="visibleFields"
            :row-key="fieldRowKey"
            row-clickable
            :sort="sortState"
            :loading="loading"
            :empty-text="t('semNoFields', ui.lang)"
            @row-click="openFieldRow"
            @update:sort="onSort"
          >
            <template #cell-name="{ row }">
              <span class="asset-name">{{ (row as FieldRow).name }}</span>
            </template>
            <template #cell-dataset="{ row }">
              <span class="cell-muted">{{ (row as FieldRow).dataset }}</span>
            </template>
            <template #cell-expression="{ row }">
              <span v-if="(row as FieldRow).expression" class="cell-expr">
                <code class="cell-mono">{{ trunc((row as FieldRow).expression ?? '', 42) }}</code>
                <button
                  type="button"
                  class="mini-btn icon"
                  :aria-label="t('copy', ui.lang)"
                  :title="t('copy', ui.lang)"
                  @click.stop="copyValue((row as FieldRow).expression ?? '')"
                >
                  <Copy :size="12" />
                </button>
              </span>
              <span v-else class="cell-muted">—</span>
            </template>
            <template #cell-datatype="{ row }">
              <span class="cell-mono">{{ (row as FieldRow).datatype || '—' }}</span>
            </template>
            <template #cell-role="{ row }">
              <span class="cell-muted">{{ roleLabel((row as FieldRow).semantic_role ?? '') }}</span>
            </template>
            <template #cell-changed="{ row }">
              <span class="cell-muted" :title="fieldChanged(row).title">{{ fieldChanged(row).text }}</span>
            </template>
          </DataTable>

          <!-- ── changes（变更评审）── -->
          <DataTable
            v-else-if="values.tab === 'changes'"
            :columns="CHANGE_COLUMNS"
            :rows="visibleChanges"
            row-key="id"
            row-clickable
            :sort="sortState"
            :loading="loading || changesLoading"
            :empty-text="t('emptyData', ui.lang)"
            @row-click="openChangeRow"
            @update:sort="onSort"
          >
            <template #cell-status="{ row }">
              <span
                class="pill"
                :class="tonePill(changeStatusTone((row as SemanticChangeRecord).status))"
              >
                {{ changeStatusLabel((row as SemanticChangeRecord).status) }}
              </span>
            </template>
            <template #cell-origin="{ row }">
              <span class="cell-muted">{{ (row as SemanticChangeRecord).origin || '—' }}</span>
            </template>
            <template #cell-summary="{ row }">
              <span class="cell-muted">{{ trunc(changeSummary(row as SemanticChangeRecord), 48) || '—' }}</span>
            </template>
            <template #cell-subjects="{ row }">
              <span class="cell-mono">{{ ((row as SemanticChangeRecord).subjects ?? []).length }}</span>
            </template>
            <template #cell-created_at="{ row }">
              <span class="cell-muted" :title="(row as SemanticChangeRecord).created_at || ''">
                {{ fmtDateTime((row as SemanticChangeRecord).created_at) || '—' }}
              </span>
            </template>
          </DataTable>

          <!-- ── pending drafts ── -->
          <template v-else>
            <div v-if="selected.length" class="bulk-bar" role="status">
              <span class="bulk-count">{{ t('semSelected', ui.lang, selected.length) }}</span>
              <el-button
                size="small"
                type="primary"
                :loading="bulkBusy"
                @click="askBatch('confirm')"
              >
                {{ t('semBulkApply', ui.lang) }}
              </el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :disabled="bulkBusy"
                @click="askBatch('reject')"
              >
                {{ t('semBulkReject', ui.lang) }}
              </el-button>
              <span class="spacer" />
              <el-button size="small" text :disabled="bulkBusy" @click="clearSelection">
                {{ t('semClearSelection', ui.lang) }}
              </el-button>
            </div>

            <div v-if="batchFailures.length" class="partial-panel" role="alert">
              <div class="partial-title">
                <TriangleAlert :size="14" aria-hidden="true" />
                {{ t('semBulkPartial', ui.lang) }}
              </div>
              <div v-for="f in batchFailures" :key="f.id" class="partial-row">
                <span class="partial-name">{{ draftNameOf(f.id) }}</span>
                <span class="partial-error">{{ f.error }}</span>
                <el-button size="small" :loading="bulkBusy" @click="retryBatchItem(f.id)">
                  {{ t('semBulkRetry', ui.lang) }}
                </el-button>
              </div>
            </div>
            <p v-else-if="batchResults && batchResults.failed === 0 && batchApplied > 0" class="sem-bulk-ok" role="status">
              {{ t('semBulkApplied', ui.lang, batchApplied) }}
            </p>

            <DataTable
              :columns="PENDING_COLUMNS"
              :rows="sortedPending"
              row-key="id"
              selectable
              row-clickable
              :selected="selected"
              :sort="sortState"
              :loading="loading"
              :select-all-label="t('semSelectAll', ui.lang)"
              :select-row-label="t('semSelectRow', ui.lang)"
              :empty-text="t('semNoPending', ui.lang)"
              @update:selected="onSelectedUpdate"
              @row-click="onPendingRow"
              @update:sort="onSort"
            >
              <template #cell-kind="{ row }">
                <span class="pill pill-neutral">{{ kindLabel((row as SemanticDraft).kind) }}</span>
              </template>
              <template #cell-action="{ row }">
                <span class="cell-muted">{{ actionLabel((row as SemanticDraft).action) }}</span>
              </template>
              <template #cell-name="{ row }">
                <span class="asset-name">{{ (row as SemanticDraft).name }}</span>
                <span v-if="(row as SemanticDraft).conflict" class="pill pill-danger">
                  {{ t('semDraftConflict', ui.lang) }}
                </span>
              </template>
              <template #cell-note="{ row }">
                <span class="cell-muted">{{ trunc((row as SemanticDraft).note ?? '', 32) || '—' }}</span>
              </template>
              <template #cell-created_at="{ row }">
                <span class="cell-muted" :title="(row as SemanticDraft).created_at || ''">
                  {{ fmtDateTime((row as SemanticDraft).created_at) || '—' }}
                </span>
              </template>
            </DataTable>

            <!-- applied / rejected trail -->
            <div v-if="applied.length || rejected.length" class="history-block">
              <div class="history-title">{{ t('semHistory', ui.lang) }}</div>
              <ul class="sem-history-list">
                <li
                  v-for="d in [...applied, ...rejected].slice(0, 10)"
                  :key="d.id"
                  class="sem-history-row"
                >
                  <span class="pill" :class="d.status === 'applied' ? 'pill-ok' : 'pill-neutral'">
                    {{ d.status === 'applied' ? t('semApplied', ui.lang) : t('semRejected', ui.lang) }}
                  </span>
                  <span class="cell-muted">{{ kindLabel(d.kind) }}</span>
                  <span>{{ d.name }}</span>
                  <span class="cell-muted">{{ fmtDateTime(d.created_at) || '—' }}</span>
                </li>
              </ul>
            </div>
          </template>

          <div v-if="currentTotal > PAGE_SIZE" class="pagination-bar">
            <el-pagination
              :current-page="pageNum"
              :page-size="PAGE_SIZE"
              :total="currentTotal"
              layout="prev, pager, next"
              @current-change="onPage"
            />
          </div>
        </template>
      </section>
    </template>

    <!-- ── asset drawer ── -->
    <DetailDrawer
      v-model="assetOpen"
      :title="assetName"
      width="600px"
      :close-label="t('cancel', ui.lang)"
    >
      <div v-if="assetSel" class="sem-drawer">
        <div class="sem-drawer-meta">
          <span class="pill pill-accent">{{ assetKindLabel }}</span>
          <code class="cell-mono">{{ assetName }}</code>
        </div>

        <section v-if="assetDescription" class="sem-block">
          <h4>{{ t('semDefinition', ui.lang) }}</h4>
          <p class="sem-block-text">{{ assetDescription }}</p>
        </section>

        <section v-if="assetExpression || editorOpen" class="sem-block">
          <h4>
            {{ t('semExpression', ui.lang) }}
            <button v-if="editTarget && !editorOpen" type="button" class="link-btn" @click="startEdit">
              {{ t('semEdit', ui.lang) }}
            </button>
          </h4>
          <div class="sem-expr">
            <code class="sem-expr-code">{{ assetExpression || '—' }}</code>
            <button
              type="button"
              class="mini-btn icon"
              :aria-label="t('copy', ui.lang)"
              :title="t('copy', ui.lang)"
              @click="copyValue(assetExpression)"
            >
              <Copy :size="12" />
            </button>
          </div>

          <!-- in-drawer editor: validate → dry run → create draft -->
          <div v-if="editorOpen" class="sem-editor">
            <el-input
              v-model="editorExpr"
              type="textarea"
              :rows="3"
              class="mono-input"
              :aria-label="t('semExpression', ui.lang)"
            />
            <p class="form-hint">{{ t('semExpressionHint', ui.lang) }}</p>

            <div v-if="previewNeedsMetric" class="sem-preview-pick">
              <el-select
                v-model="previewMetric"
                :placeholder="t('semPreviewMetric', ui.lang)"
                :aria-label="t('semPreviewMetric', ui.lang)"
                size="small"
              >
                <el-option v-for="m in previewMetricOptions" :key="m" :value="m" :label="m" />
              </el-select>
              <span v-if="!previewMetricOptions.length" class="empty-note">
                {{ t('semPreviewNoMetrics', ui.lang) }}
              </span>
            </div>

            <div class="sem-editor-actions">
              <el-button size="small" :loading="editorBusy === 'validate'" @click="runValidate">
                {{ t('semValidate', ui.lang) }}
              </el-button>
              <el-button
                size="small"
                :loading="editorBusy === 'preview'"
                :disabled="previewNeedsMetric ? !previewMetric : false"
                @click="runPreview"
              >
                {{ t('semPreview', ui.lang) }}
              </el-button>
              <el-button
                size="small"
                type="primary"
                :loading="editorBusy === 'create'"
                :disabled="!editorCheck?.ok"
                @click="createFromEditor"
              >
                {{ t('semSaveDraft', ui.lang) }}
              </el-button>
              <span class="spacer" />
              <el-button size="small" text @click="closeEditor">{{ t('cancel', ui.lang) }}</el-button>
            </div>

            <div v-if="editorCheck" class="sem-check-result" role="status">
              <span class="pill" :class="editorCheck.ok ? 'pill-ok' : 'pill-danger'">
                {{ editorCheck.ok ? t('semValidatePassed', ui.lang) : t('semValidateFailed', ui.lang) }}
              </span>
              <ul v-if="editorCheck.errors.length || editorCheck.warnings.length" class="sem-check-list">
                <li v-for="(it, i) in [...editorCheck.errors, ...editorCheck.warnings]" :key="`${it.code}-${i}`">
                  <span class="pill" :class="severityPill(it.severity)">{{ severityLabel(it.severity) }}</span>
                  <span>{{ it.message }}</span>
                  <span v-if="it.hint" class="cell-muted">{{ it.hint }}</span>
                </li>
              </ul>
              <p
                v-if="editorCheck.normalized.expression && editorCheck.normalized.expression !== editorExpr.trim()"
                class="sem-normalized"
              >
                {{ t('semNormalized', ui.lang) }}: <code>{{ editorCheck.normalized.expression }}</code>
              </p>
            </div>

            <div v-if="preview" class="sem-preview">
              <div class="sem-preview-meta">
                <span class="pill pill-neutral">{{ t('semPreviewRows', ui.lang, preview.row_count) }}</span>
                <span v-if="preview.masking_applied" class="pill pill-warn">{{ t('semMasked', ui.lang) }}</span>
              </div>
              <pre class="sql-snippet"><code>{{ preview.sql }}</code></pre>
              <table v-if="preview.rows.length" class="sem-preview-table">
                <thead>
                  <tr>
                    <th v-for="c in preview.columns" :key="c" scope="col">{{ c }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in preview.rows.slice(0, 10)" :key="i">
                    <td v-for="(v, j) in r" :key="j">{{ v === null || v === undefined ? '—' : v }}</td>
                  </tr>
                </tbody>
              </table>
              <ul v-if="preview.warnings?.length" class="sem-check-list">
                <li v-for="(w, i) in preview.warnings" :key="`w-${i}`">
                  <span class="pill pill-warn">{{ t('semSevWarning', ui.lang) }}</span>
                  <span>{{ w.message }}</span>
                </li>
              </ul>
            </div>

            <p v-if="editorError" class="form-error" role="alert">
              {{ t('semPreviewFailed', ui.lang) }}: {{ editorError }}
            </p>
          </div>
        </section>

        <section class="sem-block">
          <h4>{{ t('semAnchoredDatasets', ui.lang) }}</h4>
          <p v-if="assetAnchors.length" class="sem-chips">
            <code v-for="a in assetAnchors" :key="a" class="sem-chip">{{ a }}</code>
          </p>
          <p v-else class="empty-note">—</p>
        </section>

        <section v-if="enumDisplay.length || valueAliases.length" class="sem-block">
          <h4>{{ t('semValueDict', ui.lang) }}</h4>
          <div v-if="enumDisplay.length" class="sem-kv">
            <div class="sem-kv-title">{{ t('semEnumDisplay', ui.lang) }}</div>
            <div v-for="[k, v] in enumDisplay" :key="k" class="sem-kv-row">
              <code>{{ k }}</code>
              <span>{{ v }}</span>
            </div>
          </div>
          <div v-if="valueAliases.length" class="sem-kv">
            <div class="sem-kv-title">{{ t('semValueAliases', ui.lang) }}</div>
            <div v-for="[k, v] in valueAliases" :key="k" class="sem-kv-row">
              <code>{{ k }}</code>
              <span>{{ v.join(' · ') }}</span>
            </div>
          </div>
        </section>

        <section class="sem-block">
          <h4>{{ t('semUsedBy', ui.lang) }}</h4>
          <div v-if="usedBy.length" class="sem-usedby">
            <div v-for="g in usedBy" :key="g.label" class="sem-usedby-group">
              <span class="sem-usedby-label">{{ g.label }}</span>
              <template v-for="item in g.items" :key="item.name">
                <button
                  v-if="item.openable"
                  type="button"
                  class="sem-ref"
                  @click="openRef(item)"
                >
                  {{ item.name }}
                </button>
                <span v-else class="sem-ref is-static">{{ item.name }}</span>
              </template>
            </div>
          </div>
          <p v-else class="empty-note">{{ t('semUsedByNone', ui.lang) }}</p>
        </section>

        <section class="sem-block">
          <h4>{{ t('semHistory', ui.lang) }}</h4>
          <p v-if="trailError" class="empty-note">
            {{ t('semErrorTitle', ui.lang) }}
            <button type="button" class="link-btn" @click="retryTrail">{{ t('retry', ui.lang) }}</button>
          </p>
          <ul v-if="assetAudit.length" class="sem-trail">
            <li v-for="(a, i) in assetAudit" :key="`a-${i}`" class="sem-trail-row">
              <span class="pill pill-neutral">{{ a.action }}</span>
              <span>{{ a.username || '—' }}</span>
              <span class="cell-muted" :title="a.ts || ''">{{ fmtDateTime(a.ts) || '—' }}</span>
            </li>
          </ul>
          <ul v-if="history.length" class="sem-trail">
            <li v-for="h in history.slice(0, 5)" :key="h.sha" class="sem-trail-row">
              <code class="cell-mono">{{ h.sha.slice(0, 7) }}</code>
              <span class="sem-trail-subject">{{ h.subject }}</span>
              <span class="cell-muted">{{ h.author }}</span>
              <span class="cell-muted" :title="h.date">{{ fmtDateTime(h.date) }}</span>
            </li>
          </ul>
          <p v-if="!history.length && !assetAudit.length && !trailError" class="empty-note">
            {{ t('semHistoryEmpty', ui.lang) }}
          </p>
        </section>
      </div>

      <template #footer>
        <el-button size="small" type="danger" plain @click="askDeleteAsset">
          {{ t('semDelete', ui.lang) }}
        </el-button>
      </template>
    </DetailDrawer>

    <!-- ── draft drawer: diff + validate-before-apply ── -->
    <DetailDrawer
      v-model="draftOpen"
      :title="`${t('semDraftTitle', ui.lang)} · ${draftSel?.name ?? ''}`"
      width="560px"
      :close-label="t('cancel', ui.lang)"
    >
      <div v-if="draftSel" class="sem-drawer">
        <div class="sem-drawer-meta">
          <span class="pill pill-accent">{{ kindLabel(draftSel.kind) }}</span>
          <span class="pill pill-neutral">{{ actionLabel(draftSel.action) }}</span>
          <span class="cell-muted">{{ fmtDateTime(draftSel.created_at) || '—' }}</span>
        </div>
        <p v-if="draftSel.note" class="sem-block-text">{{ draftSel.note }}</p>

        <div v-if="draftSel.conflict" class="sem-bad-draft" role="alert">
          <strong>{{ t('semDraftConflict', ui.lang) }}</strong>
          <p>{{ draftSel.conflict.message || t('semDraftConflictHint', ui.lang) }}</p>
        </div>

        <div class="sem-check-result" role="status">
          <span v-if="draftChecking" class="cell-muted">{{ t('semLoading', ui.lang) }}</span>
          <span v-else-if="draftCheckError" class="pill pill-danger">{{ t('semValidateFailed', ui.lang) }}</span>
          <span v-else-if="draftCheck" class="pill" :class="draftCheck.ok ? 'pill-ok' : 'pill-danger'">
            {{ draftCheck.ok ? t('semValidatePassed', ui.lang) : t('semValidateFailed', ui.lang) }}
          </span>
          <ul v-if="draftCheck && (draftCheck.errors.length || draftCheck.warnings.length)" class="sem-check-list">
            <li v-for="(it, i) in [...draftCheck.errors, ...draftCheck.warnings]" :key="`${it.code}-${i}`">
              <span class="pill" :class="severityPill(it.severity)">{{ severityLabel(it.severity) }}</span>
              <span>{{ it.message }}</span>
              <span v-if="it.hint" class="cell-muted">{{ it.hint }}</span>
            </li>
          </ul>
          <p v-if="draftCheckError" class="form-error">{{ draftCheckError }}</p>
          <button type="button" class="link-btn" @click="runDraftCheck(draftSel)">
            {{ t('semValidate', ui.lang) }}
          </button>
        </div>

        <div v-if="draftSel.diff?.error" class="sem-bad-draft" role="alert">
          <strong>{{ t('semBadDraft', ui.lang) }}</strong>
          <p class="form-error">{{ draftSel.diff.error }}</p>
        </div>
        <div v-else-if="draftSel.diff" class="sem-diff">
          <div class="sem-diff-head">
            <span>{{ t('semDiffBefore', ui.lang) }}</span>
            <span>{{ t('semDiffAfter', ui.lang) }}</span>
          </div>
          <div
            v-for="(row, i) in draftSel.diff.fields"
            :key="`${row.f}-${i}`"
            class="sem-diff-row"
            :class="{ 'is-changed': row.changed }"
          >
            <span class="sem-diff-f">{{ row.f }}</span>
            <span class="sem-diff-cell">{{ row.before || '—' }}</span>
            <span class="sem-diff-cell">{{ row.after || '—' }}</span>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button
          size="small"
          :disabled="draftChecking || !draftCheck?.ok || !!draftSel?.conflict"
          @click="applyDraft"
        >
          {{ t('semConfirmDraft', ui.lang) }}
        </el-button>
        <el-button
          v-if="draftSel?.conflict"
          size="small"
          type="primary"
          plain
          @click="blueprintFromDraft"
        >
          {{ t('semDraftFromBlueprint', ui.lang) }}
        </el-button>
        <el-button
          size="small"
          type="danger"
          plain
          @click="askRejectDraft"
        >
          {{ t('semRejectDraft', ui.lang) }}
        </el-button>
      </template>
    </DetailDrawer>

    <!-- ── change drawer: 评审(快照 diff / 影响面 / 编译回放 / 合并·驳回) ── -->
    <DetailDrawer
      v-model="changeOpen"
      :title="`${t('semTabChanges', ui.lang)} · ${changeDetail?.id ?? values.change}`"
      width="620px"
      :close-label="t('cancel', ui.lang)"
    >
      <div v-if="changeDetail" class="sem-drawer">
        <div class="sem-drawer-meta">
          <span class="pill" :class="tonePill(changeStatusTone(changeDetail.status))">
            {{ changeStatusLabel(changeDetail.status) }}
          </span>
          <span class="pill pill-neutral">{{ changeDetail.origin }}</span>
          <span v-if="changeDetail.auto" class="pill pill-accent">{{ t('semChangeAuto', ui.lang) }}</span>
          <span class="cell-muted">{{ fmtDateTime(changeDetail.created_at) || '—' }}</span>
        </div>
        <p v-if="changeDetail.question" class="sem-block-text">{{ changeDetail.question }}</p>
        <p v-if="changeDetail.note" class="sem-block-text">{{ changeDetail.note }}</p>
        <p v-if="changeDetail.author" class="cell-muted">{{ changeDetail.author }}</p>

        <!-- 漂移警示:开单不阻断,但必须说出来 -->
        <div v-for="(w, i) in changeDetail.warnings ?? []" :key="`${w.code}-${i}`" class="sem-bad-draft" role="alert">
          <strong>{{ changeWarningLabel(w.code) }}</strong>
          <p v-if="w.subjects?.length" class="cell-muted">{{ (w.subjects ?? []).join(' · ') }}</p>
        </div>
        <!-- 降级如实:「读不到」不等于「没有」 -->
        <p v-for="(d, i) in changeDetail.degraded ?? []" :key="`${d}-${i}`" class="cell-muted" role="status">
          {{ changeDegradedLabel(d) }}
        </p>

        <!-- payloads:这次要改的内容(冻结在开单时) -->
        <div v-for="(p, i) in changeDetail.payloads ?? []" :key="i" class="sem-payload">
          <div class="sem-payload-head">
            <span class="pill pill-accent">{{ subjectLabel(i) || '#' + (i + 1) }}</span>
          </div>
          <pre>{{ payloadJson(p) }}</pre>
        </div>

        <!-- 快照 diff:实体级摘要 + 逐字段的改前/改后(与草稿抽屉同一渲染) -->
        <div v-if="changeDiffEntities.length" class="sem-diff">
          <div v-for="e in changeDiffEntities" :key="e.kind" class="sem-diff-row">
            <span class="sem-diff-f">{{ e.kind }}</span>
            <span class="sem-diff-cell">
              <span v-for="n in e.added" :key="`+${n}`" class="cell-mono">+{{ n }} </span>
              <span v-for="n in e.removed" :key="`-${n}`" class="cell-mono">−{{ n }} </span>
              <span v-for="n in e.modified" :key="`~${n}`" class="cell-mono">~{{ n }} </span>
            </span>
          </div>
        </div>

        <div v-if="changeDiffDetails.length" class="sem-diff">
          <div class="sem-diff-head">
            <span>{{ t('semDiffBefore', ui.lang) }}</span>
            <span>{{ t('semDiffAfter', ui.lang) }}</span>
          </div>
          <div
            v-for="(row, i) in changeDiffDetails.flatMap((d) => d.fields ?? [])"
            :key="`${row.f}-${i}`"
            class="sem-diff-row"
            :class="{ 'is-changed': row.changed }"
          >
            <span class="sem-diff-f">{{ row.f }}</span>
            <span class="sem-diff-cell">{{ row.before || '—' }}</span>
            <span class="sem-diff-cell">{{ row.after || '—' }}</span>
          </div>
        </div>
        <div v-for="(d, i) in changeDiffDetails.filter((x) => !!x.error)" :key="`err-${i}`" class="sem-bad-draft" role="alert">
          <strong>{{ t('semBadDraft', ui.lang) }}</strong>
          <p class="form-error">{{ d.error }}</p>
        </div>

        <!-- 影响面(确定性解析,带依据强度) -->
        <section v-if="changeImpact.length" class="sem-block">
          <h4>{{ t('semImpactTitle', ui.lang) }}</h4>
          <ul class="sem-check-list">
            <li v-for="(line, i) in changeImpact" :key="`${line}-${i}`">{{ line }}</li>
          </ul>
        </section>

        <!-- 合入前验证(编译回放)—— 没有结论就整节不渲染 -->
        <section v-if="changeDetail.verification" class="sem-block">
          <h4>{{ t('semVerifyTitle', ui.lang) }}</h4>
          <p>
            <span class="pill" :class="verifyPill(changeDetail.verification.verdict)">
              {{ t(verdictKey(changeDetail.verification.verdict) as I18nKey, ui.lang) }}
            </span>
          </p>
          <ul v-if="changeDetail.verification.now_broken?.length" class="sem-check-list">
            <li v-for="(b, i) in changeDetail.verification.now_broken" :key="`brk-${b}-${i}`">
              <span class="pill pill-danger">{{ t('semVerifyNowBroken', ui.lang) }}</span>
              <span class="cell-mono">{{ b }}</span>
            </li>
          </ul>
          <ul v-if="changeDetail.verification.was_broken_now_compiles?.length" class="sem-check-list">
            <li v-for="(b, i) in changeDetail.verification.was_broken_now_compiles" :key="`fix-${b}-${i}`">
              <span class="pill pill-ok">{{ t('semVerifyFixed', ui.lang) }}</span>
              <span class="cell-mono">{{ b }}</span>
            </li>
          </ul>
          <!-- 回放不了的原因原样带出(服务端串是数据不是文案) -->
          <p v-if="changeDetail.verification.not_applicable_reason" class="cell-muted">
            {{ changeDetail.verification.not_applicable_reason }}
          </p>
        </section>

        <!-- 驳回理由(已驳回的记录保留理由,可回看) -->
        <section v-if="changeDetail.reject_reason" class="sem-block">
          <h4>{{ t('semChangeReject', ui.lang) }}</h4>
          <p class="sem-block-text">{{ changeDetail.reject_reason }}</p>
        </section>
      </div>
      <p v-else class="cell-muted">{{ t('semLoading', ui.lang) }}</p>

      <template #footer>
        <el-button
          size="small"
          type="primary"
          :loading="changeAction === 'merge'"
          :disabled="!!changeAction || changeDetail?.status !== 'open'"
          @click="changeDetail && mergeChange(changeDetail)"
        >
          {{ t('semChangeMerge', ui.lang) }}
        </el-button>
        <el-button
          size="small"
          :loading="changeAction === 'verify'"
          :disabled="!!changeAction || !changeDetail"
          @click="changeDetail && runChangeVerify(changeDetail)"
        >
          {{ t('semChangeVerify', ui.lang) }}
        </el-button>
        <el-button
          size="small"
          type="danger"
          plain
          :loading="changeAction === 'reject'"
          :disabled="!!changeAction || changeDetail?.status !== 'open'"
          @click="changeDetail && askRejectChange(changeDetail)"
        >
          {{ t('semChangeReject', ui.lang) }}
        </el-button>
      </template>
    </DetailDrawer>

    <!-- ── confirms: batch apply/reject, reject-one, delete asset ── -->
    <ConfirmDialog
      v-model="confirmOpen"
      :title="confirmTitle"
      :confirm-text="confirmTextLabel"
      :cancel-text="t('cancel', ui.lang)"
      :danger="confirmDanger"
      :loading="confirmBusy"
      @confirm="runConfirm"
    >
      <!-- 变更驳回:理由必填(服务端也拒空理由),前端先挡一道 -->
      <el-input
        v-if="confirmCtx?.mode === 'change'"
        v-model="changeRejectReason"
        size="small"
        :placeholder="t('semChangeRejectReason', ui.lang)"
        :aria-label="t('semChangeRejectReason', ui.lang)"
      />
      <p v-if="confirmCtx?.mode === 'change' && changeRejectError" class="form-error">
        {{ changeRejectError }}
      </p>
      <template #impact>
        <ul class="sem-confirm-list">
          <li v-for="n in confirmImpactNames.slice(0, 8)" :key="n">{{ n }}</li>
          <li v-if="confirmImpactNames.length > 8" class="cell-muted">
            +{{ confirmImpactNames.length - 8 }}
          </li>
        </ul>
      </template>
    </ConfirmDialog>

    <!-- ── new metric ── -->
    <el-dialog
      v-model="metricOpen"
      :title="t('semAddMetric', ui.lang)"
      width="560"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveMetric">
        <p v-if="blueprintNote" class="sem-blueprint-note" role="status">
          {{ t('semBlueprintPrefill', ui.lang, blueprintNote) }}
        </p>
        <el-form-item :label="t('semName', ui.lang)" :error="fieldErrorOf(metricIssues, 'name')">
          <el-input v-model="metricForm.name" />
        </el-form-item>
        <el-form-item :label="t('semExpression', ui.lang)" :error="fieldErrorOf(metricIssues, 'expression')">
          <el-input v-model="metricForm.expression" type="textarea" :rows="3" class="mono-input" />
          <p class="form-hint">{{ t('semExpressionHint', ui.lang) }}</p>
        </el-form-item>
        <el-form-item :label="t('semSynonyms', ui.lang)" :error="fieldErrorOf(metricIssues, 'synonyms')">
          <el-input v-model="metricForm.synonyms" />
        </el-form-item>
        <el-form-item :label="t('semTables', ui.lang)" :error="fieldErrorOf(metricIssues, 'datasets')">
          <el-select
            v-model="metricForm.datasets"
            multiple
            filterable
            allow-create
            default-first-option
            class="sem-dialog-select"
          >
            <el-option v-for="d in datasetNames" :key="d" :value="d" :label="d" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('semDefinition', ui.lang)">
          <el-input v-model="metricForm.definition" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item :label="t('semNote', ui.lang)">
          <el-input v-model="metricForm.note" :placeholder="t('semNoteHint', ui.lang)" />
        </el-form-item>
        <ul v-if="generalIssues(metricIssues).length" class="sem-check-list">
          <li v-for="(it, i) in generalIssues(metricIssues)" :key="i">
            <span class="pill" :class="severityPill(it.severity)">{{ severityLabel(it.severity) }}</span>
            <span>{{ it.message }}</span>
            <span v-if="it.hint" class="cell-muted">{{ it.hint }}</span>
          </li>
        </ul>
      </el-form>
      <template #footer>
        <el-button @click="metricOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="metricBusy" @click="saveMetric">
          {{ t('semSaveDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── new field ── -->
    <el-dialog
      v-model="fieldOpen"
      :title="t('semAddField', ui.lang)"
      width="560"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveField">
        <p v-if="blueprintNote" class="sem-blueprint-note" role="status">
          {{ t('semBlueprintPrefill', ui.lang, blueprintNote) }}
        </p>
        <el-form-item :label="t('semDatasetForField', ui.lang)" :error="fieldErrorOf(fieldIssues, 'name')">
          <el-select v-model="fieldForm.dataset" filterable class="sem-dialog-select">
            <el-option v-for="d in datasetNames" :key="d" :value="d" :label="d" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('semName', ui.lang)">
          <el-input v-model="fieldForm.name" :placeholder="t('semFieldTargetHint', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('semExpression', ui.lang)" :error="fieldErrorOf(fieldIssues, 'expression')">
          <el-input v-model="fieldForm.expression" type="textarea" :rows="3" class="mono-input" />
        </el-form-item>
        <el-form-item :label="t('semDatatype', ui.lang)" :error="fieldErrorOf(fieldIssues, 'datatype')">
          <el-input v-model="fieldForm.datatype" />
        </el-form-item>
        <el-form-item :label="t('semRole', ui.lang)">
          <el-select v-model="fieldForm.semantic_role" class="sem-dialog-select">
            <el-option v-for="r in ROLE_OPTIONS" :key="r" :value="r" :label="roleLabel(r)" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="fieldForm.is_time">{{ t('semIsTime', ui.lang) }}</el-checkbox>
        </el-form-item>
        <el-form-item :label="t('semSynonyms', ui.lang)" :error="fieldErrorOf(fieldIssues, 'synonyms')">
          <el-input v-model="fieldForm.synonyms" />
        </el-form-item>
        <el-form-item :label="t('semDefinition', ui.lang)">
          <el-input v-model="fieldForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item :label="t('semNote', ui.lang)">
          <el-input v-model="fieldForm.note" :placeholder="t('semNoteHint', ui.lang)" />
        </el-form-item>
        <ul v-if="generalIssues(fieldIssues).length" class="sem-check-list">
          <li v-for="(it, i) in generalIssues(fieldIssues)" :key="i">
            <span class="pill" :class="severityPill(it.severity)">{{ severityLabel(it.severity) }}</span>
            <span>{{ it.message }}</span>
            <span v-if="it.hint" class="cell-muted">{{ it.hint }}</span>
          </li>
        </ul>
      </el-form>
      <template #footer>
        <el-button @click="fieldOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="fieldBusy" @click="saveField">
          {{ t('semSaveDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── new dataset ── -->
    <el-dialog
      v-model="datasetOpen"
      :title="t('semAddDataset', ui.lang)"
      width="520"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveDataset">
        <el-form-item :label="t('semName', ui.lang)" :error="fieldErrorOf(datasetIssues, 'name')">
          <el-input v-model="datasetForm.name" />
        </el-form-item>
        <el-form-item :label="t('semSource', ui.lang)">
          <el-input v-model="datasetForm.source" class="mono-input" />
        </el-form-item>
        <el-form-item :label="t('semPrimaryKey', ui.lang)" :error="fieldErrorOf(datasetIssues, 'primary_key')">
          <el-input v-model="datasetForm.primary_key" />
        </el-form-item>
        <el-form-item :label="t('semSynonyms', ui.lang)" :error="fieldErrorOf(datasetIssues, 'synonyms')">
          <el-input v-model="datasetForm.synonyms" />
        </el-form-item>
        <el-form-item :label="t('semDefinition', ui.lang)">
          <el-input v-model="datasetForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item :label="t('semNote', ui.lang)">
          <el-input v-model="datasetForm.note" :placeholder="t('semNoteHint', ui.lang)" />
        </el-form-item>
        <ul v-if="generalIssues(datasetIssues).length" class="sem-check-list">
          <li v-for="(it, i) in generalIssues(datasetIssues)" :key="i">
            <span class="pill" :class="severityPill(it.severity)">{{ severityLabel(it.severity) }}</span>
            <span>{{ it.message }}</span>
            <span v-if="it.hint" class="cell-muted">{{ it.hint }}</span>
          </li>
        </ul>
      </el-form>
      <template #footer>
        <el-button @click="datasetOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="datasetBusy" @click="saveDataset">
          {{ t('semSaveDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sem-page {
  padding-bottom: var(--sp-8);
}

/* ── header controls ─────────────────────────────────────────── */
.sem-ds-switch {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
}
.sem-ds-seg {
  height: 26px;
  padding: 0 var(--sp-3);
  border-radius: var(--r-sm);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  font-weight: 500;
}
.sem-ds-seg:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.sem-ds-seg.is-active {
  background: var(--surface-raised);
  color: var(--accent-active);
  box-shadow: var(--shadow-sm);
}
.sem-ds-seg:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

.sem-more {
  position: relative;
}
.sem-new-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: 32px;
  padding: 0 var(--sp-3);
  border: 1px solid var(--accent);
  border-radius: var(--r-sm);
  background: var(--accent);
  color: var(--on-accent);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.sem-new-btn:not(:disabled):hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.sem-new-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.sem-new-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.sem-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 40;
  min-width: 160px;
  padding: var(--sp-1);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  box-shadow: var(--shadow-lg);
}
.sem-menu-item {
  display: block;
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  color: var(--text-primary);
  font-size: var(--fs-xs);
  text-align: left;
}
.sem-menu-item:hover {
  background: var(--surface-hover);
}
.sem-menu-item:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: -2px;
}

/* ── context strip ───────────────────────────────────────────── */
.sem-context {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.sem-sep {
  color: var(--text-tertiary);
}

/* ── KPI row ─────────────────────────────────────────────────── */
.sem-kpi-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--sp-2);
  margin-bottom: var(--sp-4);
}
@media (max-width: 900px) {
  .sem-kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* ── problems panel ──────────────────────────────────────────── */
.sem-problems {
  margin-bottom: var(--sp-4);
}
.sem-drift-skipped {
  display: flex;
  gap: var(--sp-3);
  margin: var(--sp-4) var(--sp-5) 0;
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-default);
  border-left: 3px solid var(--warn, var(--accent));
  border-radius: var(--r-md);
  background: var(--surface-muted);
  color: var(--text-secondary);
}
.sds-title {
  font-weight: 600;
  color: var(--text-primary);
}
.sds-desc,
.sds-reason {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-2xs);
}
.sem-check-error {
  margin: var(--sp-3) var(--sp-5) 0;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--danger-bg);
  color: var(--danger-text);
  font-size: var(--fs-2xs);
}
.sem-check-error code {
  font-family: var(--font-mono);
  margin-left: var(--sp-2);
}
.sem-problem-group {
  padding: var(--sp-3) var(--sp-5) 0;
}
.sem-group-title {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}
.sem-issue-list,
.sem-drift-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sem-issue-row {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  font-size: var(--fs-xs);
}
.sem-issue-body {
  flex: 1;
  min-width: 0;
}
.sem-issue-msg {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.sem-issue-target {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--accent-active);
}
.sem-issue-hint {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.sem-drift-row {
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  font-size: var(--fs-xs);
}
.sdr-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}
.sdr-subject {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
}
.sdr-life {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.sdr-spacer {
  flex: 1;
}
.sdr-detail {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.sdr-impact {
  margin-top: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.sdr-impact-group {
  display: flex;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
}
.sdr-impact-label {
  flex: none;
  min-width: 5em;
  color: var(--text-tertiary);
}
.sdr-impact-items {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.sem-no-problems {
  padding-bottom: var(--sp-4);
}

/* ── tabs / toolbar / table ──────────────────────────────────── */
.sem-tabs {
  display: flex;
  gap: var(--sp-1);
  margin-bottom: var(--sp-3);
}
.sem-tab {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  height: 30px;
  padding: 0 var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.sem-tab:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.sem-tab.is-active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-active);
}
.sem-tab:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.sem-pane {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.list-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.list-toolbar .spacer {
  flex: 1;
}
.sem-search {
  max-width: 320px;
}

.asset-name {
  font-weight: 500;
  color: var(--text-primary);
}
.cell-expr {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}
.cell-expr .cell-mono {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── bulk / partial ──────────────────────────────────────────── */
.bulk-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--accent);
  border-radius: var(--r-md);
  background: var(--accent-soft);
}
.bulk-bar .spacer {
  flex: 1;
}
.bulk-count {
  font-size: var(--fs-xs);
  font-weight: 500;
  color: var(--accent-active);
}
.partial-panel {
  padding: var(--sp-3);
  border: 1px solid var(--danger);
  border-radius: var(--r-md);
  background: var(--danger-bg);
}
.partial-title {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-bottom: var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--danger-text);
}
.partial-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
  font-size: var(--fs-2xs);
}
.partial-name {
  font-weight: 500;
  color: var(--text-primary);
}
.partial-error {
  flex: 1;
  min-width: 0;
  color: var(--danger-text);
  overflow-wrap: anywhere;
}
.sem-bulk-ok {
  margin: 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.sem-history-list {
  list-style: none;
  margin: 0;
  padding: 0 var(--sp-5) var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.sem-history-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
}

/* ── drawer ──────────────────────────────────────────────────── */
.sem-drawer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.sem-drawer-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}
.sem-block h4 {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}
.sem-block-text {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  line-height: var(--lh-relaxed);
  overflow-wrap: anywhere;
}
.sem-expr {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}
.sem-expr-code {
  flex: 1;
  min-width: 0;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.sem-editor {
  margin-top: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sem-editor-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}
.sem-editor-actions .spacer {
  flex: 1;
}
.sem-preview-pick {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.sem-check-result {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
}
.sem-check-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.sem-check-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.sem-normalized {
  margin: 0;
}
.sem-normalized code {
  font-family: var(--font-mono);
}
.sem-preview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sem-preview-meta {
  display: flex;
  gap: var(--sp-2);
}
.sem-preview-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-2xs);
}
.sem-preview-table th,
.sem-preview-table td {
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border-subtle);
  text-align: left;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.sem-preview-table th {
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-weight: 500;
}
.sem-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  margin: 0;
}
.sem-chip {
  padding: 1px var(--sp-2);
  border-radius: var(--r-full);
  background: var(--accent-soft);
  color: var(--accent-active);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
}
.sem-kv {
  margin-bottom: var(--sp-2);
}
.sem-kv-title {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  margin-bottom: var(--sp-1);
}
.sem-kv-row {
  display: flex;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
}
.sem-kv-row code {
  font-family: var(--font-mono);
  color: var(--accent-active);
}
.sem-usedby {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sem-usedby-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
}
.sem-usedby-label {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  margin-right: var(--sp-1);
}
.sem-ref {
  padding: 1px var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  background: var(--surface-muted);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
}
.sem-ref:hover {
  border-color: var(--accent);
  color: var(--accent-active);
}
.sem-ref:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
/* unresolved references (example questions / rule / lesson text) are shown
   as plain text — the dashed edge says "not a link", no dead click target */
.sem-ref.is-static {
  border-style: dashed;
  color: var(--text-secondary);
  font-family: var(--font-sans);
  cursor: default;
}
.sem-trail {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.sem-trail-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
}
.sem-trail-subject {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── draft diff ──────────────────────────────────────────────── */
.sem-bad-draft {
  padding: var(--sp-3);
  border: 1px solid var(--danger);
  border-radius: var(--r-md);
  background: var(--danger-bg);
  font-size: var(--fs-xs);
  color: var(--danger-text);
}
.sem-bad-draft p {
  margin: var(--sp-1) 0 0;
}
/* blueprint prefill: context, not an error — why this form is prefilled */
.sem-blueprint-note {
  margin-bottom: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-default);
  border-left: 3px solid var(--warn, var(--accent));
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.sem-diff {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  overflow: hidden;
}
.sem-diff-head {
  display: grid;
  grid-template-columns: 7em 1fr 1fr;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--surface-muted);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.sem-diff-head span:first-child {
  grid-column: 1;
}
.sem-diff-row {
  display: grid;
  grid-template-columns: 7em 1fr 1fr;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.sem-diff-row.is-changed {
  background: var(--accent-soft);
  color: var(--text-primary);
}
.sem-diff-f {
  color: var(--text-tertiary);
}
.sem-diff-cell {
  font-family: var(--font-mono);
}
/* ── change payloads(评审抽屉:冻结的变更内容) ── */
.sem-payload {
  margin-bottom: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  overflow: hidden;
}
.sem-payload-head {
  padding: var(--sp-1) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}
.sem-payload pre {
  margin: 0;
  padding: var(--sp-3);
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
  white-space: pre;
}
.sem-confirm-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
}
.sem-dialog-select {
  width: 100%;
}
</style>
