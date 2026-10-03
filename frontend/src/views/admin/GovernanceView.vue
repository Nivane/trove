<!--
  GovernanceView — P5 治理中心(§2 / §3.2 / §6)。

  一个页面 = 治理地图 + KPI 行 + 四个 Tab(收件箱 / 覆盖与体检 / 漂移与版本 /
  血缘与数据地图)。三条贯穿全页的纪律:

    · **URL 即状态**(§3.2 冻结键表:tab / ds / kind / q / sort / page /
      only_gap / status / level / table)—— 筛选可分享、刷新不丢、浏览器
      后退能撤销一次筛选(useListQuery push 档);
    · **诚实三态**(§6):503/跳过只写「检测未能完成 · skip_reason」并保留
      上一次结果,绝不化成「0 条漂移」;`count_exact:false` 渲染「≥ N」;
      null 与 0 分开(null → 「— / 未取到」);
    · **动作与专业页同源**(附录 A 规则一):确认/驳回/回滚/裁定全部调各专业页
      既有端点,治理中心只是第二个入口;漂移裁定必须带 reason(后端门禁)。

  跨源扇出一律逐源独立(§4.4g):一个源挂了,其他源的数照常显示,
  失败源进 degraded 明细 —— 上限是「部分未取到」,不是整页失败。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '../../components/base/PageHeader.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import ConfirmDialog from '../../components/base/ConfirmDialog.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import DegradedNotice from '../../components/ops/DegradedNotice.vue'
import GovernanceMap from '../../components/governance/GovernanceMap.vue'
import InboxTable from '../../components/governance/InboxTable.vue'
import InboxBulkBar from '../../components/governance/InboxBulkBar.vue'
import CoverageTable from '../../components/governance/CoverageTable.vue'
import AssetHealthPanel from '../../components/governance/AssetHealthPanel.vue'
import DriftTable from '../../components/governance/DriftTable.vue'
import DriftDrawer from '../../components/governance/DriftDrawer.vue'
import VersionTimeline from '../../components/governance/VersionTimeline.vue'
import RollbackDialog from '../../components/governance/RollbackDialog.vue'
import LineagePanel from '../../components/governance/LineagePanel.vue'
import TableDetailDrawer from '../../components/governance/TableDetailDrawer.vue'
import { useListQuery } from '../../composables/useListQuery'
import { apiGet } from '../../api/http'
import { fetchOverview } from '../../api/overview'
import {
  GOVERNANCE_DRIFT_STATUSES,
  GOVERNANCE_INBOX_SORTS,
  GOVERNANCE_TABS,
  GOVERNANCE_TODO_KINDS,
  GOVERNANCE_TODOS_LIMIT,
  batchSemanticDrafts,
  confirmExample,
  confirmLesson,
  confirmPreferenceDraft,
  confirmSemanticDraft,
  confirmSkill,
  declareExternalDrift,
  fetchDriftList,
  fetchDriftRuns,
  fetchGovernanceCoverage,
  fetchGovernanceTodos,
  fetchSemanticHistory,
  rejectExample,
  rejectLesson,
  rejectPreferenceDraft,
  rejectSemanticDraft,
  rejectSkill,
  resolveDrift,
  rollbackSemantic,
  runDriftCheck,
  waiveDrift,
} from '../../api/governance'
import type { GovernanceTab } from '../../api/governance'
import type { BulkGroup, BulkResultRow } from '../../components/governance/InboxBulkBar.vue'
import type { RollbackTarget } from '../../components/governance/RollbackDialog.vue'
import type { DataTableSort } from '../../components/base/DataTable.vue'
import type {
  DatasourceInfo,
  GovernanceCoverage,
  GovernanceCoverageSource,
  GovernanceDriftItem,
  GovernanceTodoItem,
  GovernanceTodos,
  SemanticHistoryEntry,
} from '../../api/types'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'

const ui = useUiStore()
const router = useRouter()
const route = useRoute()

/* ── URL 状态(§3.2 冻结键表)────────────────────────────── */

const { values } = useListQuery(
  {
    tab: 'inbox',
    ds: '',
    kind: '',
    q: '',
    sort: 'oldest',
    page: '1',
    only_gap: '',
    status: 'open',
    level: '',
    table: '',
  },
  { mode: 'push' },
)

const activeTab = computed<GovernanceTab>(() =>
  (GOVERNANCE_TABS as readonly string[]).includes(values.tab)
    ? (values.tab as GovernanceTab)
    : 'inbox',
)

// §2.3 统一页头:根面包屑=管理台(→ /admin),末项=本页。
const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('govTitle', ui.lang) },
])

const PAGE_SIZE = GOVERNANCE_TODOS_LIMIT
const pageNum = computed(() => Math.max(1, Number.parseInt(values.page, 10) || 1))

function setTab(next: GovernanceTab) {
  if (activeTab.value === next) return
  // Tab 切换 = 一次显式导航:带上全局键(tab/ds),清掉其他 Tab 的局部键。
  const query: Record<string, string> = {}
  if (next !== 'inbox') query.tab = next
  if (values.ds) query.ds = values.ds
  void router.push({ path: route.path, query })
}

const TABS: { key: GovernanceTab; label: Parameters<typeof t>[0] }[] = [
  { key: 'inbox', label: 'govTabInbox' },
  { key: 'coverage', label: 'govTabCoverage' },
  { key: 'drift', label: 'govTabDrift' },
  { key: 'lineage', label: 'govTabLineage' },
]

/* 治理地图:默认展开(§2.2 它是第一屏),可收起。 */
const mapOpen = ref(true)

/* ── 数据源清单(各 Tab 共用)────────────────────────────── */

const dsNames = ref<string[]>([])
const dsListError = ref('')

async function loadDatasources() {
  try {
    const body = await apiGet<{ datasources: DatasourceInfo[] }>('/v1/admin/datasources')
    dsNames.value = (body.datasources ?? []).map((d) => d.name)
    dsListError.value = ''
  } catch (e) {
    dsNames.value = []
    dsListError.value = e instanceof Error ? e.message : String(e)
  }
}

/* ── KPI 行(overview.todos 的六类 + 合计)────────────────── */

const overview = ref<Awaited<ReturnType<typeof fetchOverview>> | null>(null)

async function loadOverview() {
  try {
    overview.value = await fetchOverview('24h')
  } catch {
    overview.value = null // 取不到就整体显示 —,不是 0
  }
}

const KIND_KEY: Record<string, Parameters<typeof t>[0]> = {
  kb_lesson: 'govKindKbLesson',
  kb_example: 'govKindKbExample',
  semantic_draft: 'govKindSemanticDraft',
  skill_draft: 'govKindSkillDraft',
  memory_preference: 'govKindMemoryPref',
  drift: 'govKindDrift',
}

function kindLabel(kind: string): string {
  const key = KIND_KEY[kind]
  return key ? t(key, ui.lang) : kind
}

function kpiItem(kind: string) {
  return overview.value?.todos?.items.find((i) => i.kind === kind) ?? null
}

/** null → —;count_exact:false → ≥ N(§6-C)。 */
function kpiValue(kind: string): string {
  const it = kpiItem(kind)
  if (!it || it.count === null) return '—'
  return it.count_exact ? String(it.count) : t('govAtLeast', ui.lang, it.count)
}

const kpiTotalText = computed(() => {
  const todos = overview.value?.todos
  if (!todos) return '—'
  return todos.count_exact ? String(todos.total) : t('govAtLeast', ui.lang, todos.total)
})

const kpiKindCount = computed(() => {
  const items = overview.value?.todos?.items ?? []
  return items.filter(
    (i) => (GOVERNANCE_TODO_KINDS as readonly string[]).includes(i.kind) && i.count !== null,
  ).length
})

function pickKind(kind: string) {
  values.tab = 'inbox'
  values.kind = values.kind === kind ? '' : kind
  values.page = '1'
}

const asOf = computed(() =>
  overview.value?.generated_at ? fmtDateTime(overview.value.generated_at) : '',
)

/* ── ① 收件箱 ─────────────────────────────────────────────── */

const todos = ref<GovernanceTodos | null>(null)
const inboxLoading = ref(false)
const inboxError = ref('')
const selected = ref<(string | number)[]>([])
const busyId = ref<string | null>(null)

const inboxFiltered = computed(() => Boolean(values.kind || values.q))

const inboxDegraded = computed(() =>
  (todos.value?.degraded ?? []).map((d) => ({
    block: d.kind,
    source: d.ds ?? '',
    error: d.error,
    at: d.at,
  })),
)

/**
 * total 是下界吗:降级腿漏取的条目后端如实「少算」(不落 0、也不硬标 null),
 * 契约里没有 count_exact —— 下界信号只在 degraded[] 里(§6-B:降级要显示为
 * 「≥ N」,不显示假精确值)。与当前切片相交的降级腿才影响这里显示的数:
 * kind 在请求的类别内(未筛 = 六类),且 ds 命中源筛选(或 ds 为 null 的
 * 全局腿/整体枚举失败腿 —— 它们与源筛选无关)。
 */
const totalIsFloor = computed(() => {
  const payload = todos.value
  if (!payload || payload.total === null) return false
  const wanted: readonly string[] = values.kind
    ? [values.kind]
    : GOVERNANCE_TODO_KINDS
  return payload.degraded.some(
    (d) =>
      wanted.includes(d.kind) &&
      (!values.ds || d.ds === null || d.ds === values.ds),
  )
})

const inboxSort = computed<DataTableSort | null>(() =>
  values.sort === 'newest'
    ? { key: 'created_at', dir: 'desc' }
    : values.sort === 'oldest'
      ? { key: 'created_at', dir: 'asc' }
      : null,
)

function onInboxSort(sort: DataTableSort | null) {
  if (!sort) return
  values.sort = sort.dir === 'desc' ? 'newest' : 'oldest'
}

const SORT_KEY: Record<string, Parameters<typeof t>[0]> = {
  oldest: 'govInboxSortOldest',
  newest: 'govInboxSortNewest',
  confidence: 'govInboxSortConfidence',
  severity: 'govInboxSortSeverity',
}

async function loadInbox() {
  inboxLoading.value = true
  inboxError.value = ''
  try {
    todos.value = await fetchGovernanceTodos({
      kind: values.kind,
      ds: values.ds,
      q: values.q,
      sort: values.sort,
      limit: PAGE_SIZE,
      offset: (pageNum.value - 1) * PAGE_SIZE,
    })
  } catch (e) {
    todos.value = null
    inboxError.value = e instanceof Error ? e.message : String(e)
  } finally {
    inboxLoading.value = false
  }
}

const nextDisabled = computed(() => {
  const items = todos.value?.items?.length ?? 0
  if (items < PAGE_SIZE) return true
  // 下界 total 不构成「没有下一页」的证据 —— 少取到的条目可能就在下一页。
  if (totalIsFloor.value) return false
  const total = todos.value?.total
  return total !== null && total !== undefined && pageNum.value * PAGE_SIZE >= total
})

function clearInboxFilters() {
  values.kind = ''
  values.q = ''
  values.page = '1'
}

function goDrift(item: GovernanceTodoItem) {
  values.tab = 'drift'
  values.ds = item.ds || values.ds
  values.q = ''
  values.level = ''
  values.status = 'open'
}

function openEdit(item: GovernanceTodoItem) {
  const href = item.actionable.edit_url
  if (href) void router.push(href)
}

/* 单条动作:按 kind 分派到各专业页端点(§4.2)。 */
async function actOnItem(item: GovernanceTodoItem, action: 'confirm' | 'reject') {
  busyId.value = item.id
  try {
    if (item.kind === 'kb_lesson') {
      if (action === 'confirm') await confirmLesson(item.ds, item.id)
      else await rejectLesson(item.ds, item.id)
    } else if (item.kind === 'kb_example') {
      if (action === 'confirm') await confirmExample(item.ds, item.title, item.summary)
      else await rejectExample(item.ds, item.title, item.summary)
    } else if (item.kind === 'semantic_draft') {
      if (action === 'confirm') await confirmSemanticDraft(item.ds, item.id)
      else await rejectSemanticDraft(item.ds, item.id)
    } else if (item.kind === 'skill_draft') {
      if (action === 'confirm') await confirmSkill(item.id)
      else await rejectSkill(item.id)
    } else if (item.kind === 'memory_preference') {
      if (action === 'confirm') await confirmPreferenceDraft(item.id)
      else await rejectPreferenceDraft(item.id)
    } else {
      throw new Error(`no single-item endpoint for kind ${item.kind}`)
    }
    await loadInbox()
    await loadOverview()
  } catch (e) {
    inboxError.value = `${t('govActionFailed', ui.lang)}: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    busyId.value = null
  }
}

/* 批量:同类同源分组;语义草稿走批量端点,其余逐条(§2.3)。 */
const bulkBusy = ref(false)
const bulkResults = ref<BulkResultRow[]>([])
const bulkHasResults = ref(false)
/** 上一批的方向 —— 失败行的单条重试沿用同一方向(confirm / reject)。 */
const bulkLastAction = ref<'confirm' | 'reject'>('confirm')

const bulkGroups = computed<BulkGroup[]>(() => {
  const items = (todos.value?.items ?? []).filter(
    (i) => selected.value.includes(i.id) && i.kind !== 'drift',
  )
  const map = new Map<string, BulkGroup>()
  for (const it of items) {
    const key = `${it.kind}:${it.ds}`
    const group = map.get(key) ?? {
      kind: it.kind,
      ds: it.ds,
      label: kindLabel(it.kind),
      ids: [] as string[],
    }
    group.ids.push(it.id)
    map.set(key, group)
  }
  return [...map.values()]
})

function itemBy(id: string): GovernanceTodoItem | null {
  return (todos.value?.items ?? []).find((i) => i.id === id) ?? null
}

async function runBulk(action: 'confirm' | 'reject', group: BulkGroup) {
  bulkBusy.value = true
  bulkHasResults.value = false
  bulkLastAction.value = action
  const rows: BulkResultRow[] = []
  try {
    if (group.kind === 'semantic_draft') {
      const res = await batchSemanticDrafts(group.ds, action, group.ids)
      for (const r of res.results ?? []) {
        rows.push({
          id: r.id,
          title: itemBy(r.id)?.title ?? r.id,
          ok: r.ok,
          error: r.error ?? '',
        })
      }
    } else {
      const settled = await Promise.allSettled(
        group.ids.map(async (id) => {
          const item = itemBy(id)
          if (!item) throw new Error('item no longer on this page')
          await actOnItemQuiet(item, action)
        }),
      )
      settled.forEach((s, i) => {
        const id = group.ids[i]
        rows.push({
          id,
          title: itemBy(id)?.title ?? id,
          ok: s.status === 'fulfilled',
          error:
            s.status === 'rejected'
              ? s.reason instanceof Error
                ? s.reason.message
                : String(s.reason)
              : '',
        })
      })
    }
  } catch (e) {
    // 整组请求都没发出去(网络级失败):也要显式落一行,不能静默。
    for (const id of group.ids) {
      rows.push({
        id,
        title: itemBy(id)?.title ?? id,
        ok: false,
        error: e instanceof Error ? e.message : String(e),
      })
    }
  } finally {
    bulkBusy.value = false
    bulkResults.value = rows
    bulkHasResults.value = true
    selected.value = []
    await loadInbox()
    await loadOverview()
  }
}

/** 批量里的单条(不吞失败:抛给 allSettled 收集)。 */
async function actOnItemQuiet(item: GovernanceTodoItem, action: 'confirm' | 'reject') {
  if (item.kind === 'kb_lesson') {
    if (action === 'confirm') await confirmLesson(item.ds, item.id)
    else await rejectLesson(item.ds, item.id)
  } else if (item.kind === 'kb_example') {
    if (action === 'confirm') await confirmExample(item.ds, item.title, item.summary)
    else await rejectExample(item.ds, item.title, item.summary)
  } else if (item.kind === 'semantic_draft') {
    if (action === 'confirm') await confirmSemanticDraft(item.ds, item.id)
    else await rejectSemanticDraft(item.ds, item.id)
  } else if (item.kind === 'skill_draft') {
    if (action === 'confirm') await confirmSkill(item.id)
    else await rejectSkill(item.id)
  } else if (item.kind === 'memory_preference') {
    if (action === 'confirm') await confirmPreferenceDraft(item.id)
    else await rejectPreferenceDraft(item.id)
  } else {
    throw new Error(`no single-item endpoint for kind ${item.kind}`)
  }
}

/** 失败行的单条重试:只重发这一条,方向沿用上一批(§6-2)。 */
async function retryBulkRow(row: BulkResultRow) {
  const item = itemBy(row.id)
  if (!item) return
  bulkBusy.value = true
  try {
    await actOnItemQuiet(item, bulkLastAction.value)
    bulkResults.value = bulkResults.value.map((r) =>
      r.id === row.id ? { ...r, ok: true, error: '' } : r,
    )
  } catch (e) {
    bulkResults.value = bulkResults.value.map((r) =>
      r.id === row.id
        ? { ...r, ok: false, error: e instanceof Error ? e.message : String(e) }
        : r,
    )
  } finally {
    bulkBusy.value = false
    await loadInbox()
  }
}

/* ── ② 覆盖与体检 ─────────────────────────────────────────── */

const coverage = ref<GovernanceCoverage | null>(null)
const coverageLoading = ref(false)
const coverageError = ref('')

const coverageFiltered = computed(() => Boolean(values.q || values.only_gap))

const coverageRows = computed<GovernanceCoverageSource[]>(() => {
  const list = coverage.value?.sources ?? []
  const q = values.q.trim().toLowerCase()
  return list.filter((s) => {
    if (q && !s.ds.toLowerCase().includes(q)) return false
    if (values.only_gap === '1') {
      const gap =
        (s.uncovered_tables?.length ?? 0) > 0 ||
        (s.asked_unmodeled?.length ?? 0) > 0 ||
        (s.refused?.count ?? 0) > 0
      if (!gap) return false
    }
    return true
  })
})

async function loadCoverage() {
  coverageLoading.value = true
  coverageError.value = ''
  try {
    coverage.value = await fetchGovernanceCoverage({ ds: values.ds })
  } catch (e) {
    coverage.value = null
    coverageError.value = e instanceof Error ? e.message : String(e)
  } finally {
    coverageLoading.value = false
  }
}

function clearCoverageFilters() {
  values.q = ''
  values.only_gap = ''
}

const assetsOpen = ref(false)
const assetsDs = ref('')

function openAssets(ds: string) {
  assetsDs.value = ds
  assetsOpen.value = true
}

function goModel(payload: { ds: string; table: string }) {
  // 深链到语义页的对应数据源位置(§6-5)——不是首页,也不是当前页原地。
  void router.push(`/admin/semantic?ds=${encodeURIComponent(payload.ds)}`)
}

/* ── ③ 漂移与版本 ─────────────────────────────────────────── */

interface DriftSourceState {
  items: GovernanceDriftItem[]
  loading: boolean
  error: string
  checked: boolean
  stale: boolean
  skipReason: string
  lastRunAt: string
}

const EMPTY_DRIFT: DriftSourceState = {
  items: [],
  loading: false,
  error: '',
  checked: true,
  stale: false,
  skipReason: '',
  lastRunAt: '',
}

const driftMap = ref<Record<string, DriftSourceState>>({})
const driftNotice = ref('')

const driftSources = computed(() => (values.ds ? [values.ds] : dsNames.value))

/** 取数路径专用:缺席则建(写操作写在自己建的那份上)。 */
function ensureDrift(ds: string): DriftSourceState {
  if (!driftMap.value[ds]) driftMap.value[ds] = { ...EMPTY_DRIFT }
  return driftMap.value[ds]
}

/** 只读视图(模板用):缺席 = 只读空态,渲染期不改状态。 */
function driftState(ds: string): DriftSourceState {
  return driftMap.value[ds] ?? EMPTY_DRIFT
}

const driftFiltered = computed(() => Boolean(values.q || values.level))

function driftItems(ds: string): GovernanceDriftItem[] {
  const items = driftState(ds).items ?? []
  const q = values.q.trim().toLowerCase()
  if (!q) return items
  return items.filter((i) =>
    [i.subject, i.kind, i.source, i.datasource].some((v) => (v ?? '').toLowerCase().includes(q)),
  )
}

async function loadDriftSource(ds: string) {
  const st = ensureDrift(ds)
  st.loading = true
  st.error = ''
  const listOpts: { status?: string; level?: string; includeWaived?: boolean } = {}
  if (values.status && values.status !== 'all') listOpts.status = values.status
  else listOpts.includeWaived = true
  if (values.level) listOpts.level = values.level
  const [listRes, runsRes] = await Promise.allSettled([
    fetchDriftList(ds, listOpts),
    fetchDriftRuns(ds),
  ])
  if (listRes.status === 'fulfilled') st.items = listRes.value.items ?? []
  else st.error = listRes.reason instanceof Error ? listRes.reason.message : String(listRes.reason)
  if (runsRes.status === 'fulfilled') {
    const runs = runsRes.value.runs ?? []
    st.checked = runs.length > 0
    const last = runs[0]
    if (last) {
      st.stale = last.status !== 'ok'
      st.skipReason = last.skip_reason ?? ''
      st.lastRunAt = last.started_at
    }
  }
  st.loading = false
}

async function checkDrift(ds: string) {
  const st = ensureDrift(ds)
  st.loading = true
  driftNotice.value = ''
  try {
    // 503 带完整报告 → 这里 resolve 出 skipped 报告,画面写「检测未能完成」。
    const report = await runDriftCheck(ds)
    await loadDriftSource(ds)
    st.checked = true
    st.stale = report.status !== 'ok'
    st.skipReason = report.skip_reason ?? ''
  } catch (e) {
    st.error = e instanceof Error ? e.message : String(e)
  } finally {
    st.loading = false
  }
}

function clearDriftFilters() {
  values.q = ''
  values.level = ''
  values.status = 'open'
}

/* 裁定(resolve / waive):reason 必填,后端门禁同样 min_length=1。 */
const driftAction = ref<{ kind: 'resolve' | 'waive'; item: GovernanceDriftItem } | null>(null)
const driftReason = ref('')
const driftReasonError = ref('')
const driftActing = ref(false)

function openDriftAction(kind: 'resolve' | 'waive', item: GovernanceDriftItem) {
  driftAction.value = { kind, item }
  driftReason.value = ''
  driftReasonError.value = ''
}

async function confirmDriftAction() {
  const target = driftAction.value
  if (!target) return
  if (!driftReason.value.trim()) {
    driftReasonError.value = t('govDriftReasonRequired', ui.lang)
    return
  }
  driftActing.value = true
  try {
    if (target.kind === 'resolve') await resolveDrift(target.item.datasource, target.item.id, driftReason.value.trim())
    else await waiveDrift(target.item.datasource, target.item.id, driftReason.value.trim())
    driftNotice.value = `${t('govActionDone', ui.lang)} · ${target.item.subject}`
    driftAction.value = null
    await loadDriftSource(target.item.datasource)
  } catch (e) {
    driftReasonError.value = `${t('govActionFailed', ui.lang)}: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    driftActing.value = false
  }
}

/* L4 声明(没有检测器,只有人的告知)。 */
const declareOpen = ref(false)
const declareDs = ref('')
const declareSubject = ref('')
const declareDetail = ref('')
const declareSeverity = ref<'info' | 'warning' | 'critical'>('warning')
const declareBusy = ref(false)
const declareError = ref('')

function toggleDeclare() {
  declareOpen.value = !declareOpen.value
  declareError.value = ''
  if (declareOpen.value && !declareDs.value) declareDs.value = values.ds || dsNames.value[0] || ''
}

async function submitDeclare() {
  if (!declareDs.value || !declareSubject.value.trim()) {
    declareError.value = t('govDriftDeclareRequired', ui.lang)
    return
  }
  declareBusy.value = true
  declareError.value = ''
  try {
    await declareExternalDrift({
      datasource: declareDs.value,
      subject: declareSubject.value.trim(),
      kind: 'semantics_changed',
      detail: declareDetail.value.trim() ? { description: declareDetail.value.trim() } : {},
      severity: declareSeverity.value,
    })
    driftNotice.value = t('govActionDone', ui.lang)
    declareOpen.value = false
    declareSubject.value = ''
    declareDetail.value = ''
    await loadDriftSource(declareDs.value)
  } catch (e) {
    declareError.value = `${t('govActionFailed', ui.lang)}: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    declareBusy.value = false
  }
}

/* 漂移详情抽屉。 */
const driftDrawerOpen = ref(false)
const driftDrawerDs = ref('')
const driftDrawerId = ref<number | null>(null)

function openDriftDetail(item: GovernanceDriftItem) {
  driftDrawerDs.value = item.datasource
  driftDrawerId.value = item.id
  driftDrawerOpen.value = true
}

/* 版本与回滚(单源)。 */
const history = ref<SemanticHistoryEntry[]>([])
const historyLoading = ref(false)
const historyError = ref('')

async function loadHistory() {
  if (!values.ds) {
    history.value = []
    return
  }
  historyLoading.value = true
  historyError.value = ''
  try {
    const body = await fetchSemanticHistory(values.ds)
    history.value = body.history ?? []
  } catch (e) {
    history.value = []
    historyError.value = e instanceof Error ? e.message : String(e)
  } finally {
    historyLoading.value = false
  }
}

const rollbackOpen = ref(false)
const rollbackTarget = ref<RollbackTarget | null>(null)
const rollbackBusy = ref(false)
const rollbackError = ref('')

function openRollback(entry: SemanticHistoryEntry) {
  rollbackTarget.value = { ds: values.ds, sha: entry.sha }
  rollbackError.value = ''
  rollbackOpen.value = true
}

async function confirmRollback() {
  const target = rollbackTarget.value
  if (!target) return
  rollbackBusy.value = true
  rollbackError.value = ''
  try {
    await rollbackSemantic(target.ds, target.sha)
    rollbackOpen.value = false
    driftNotice.value = t('govRollDone', ui.lang)
    await Promise.all([loadHistory(), loadCoverage()])
  } catch (e) {
    rollbackError.value = e instanceof Error ? e.message : String(e)
  } finally {
    rollbackBusy.value = false
  }
}

/* ── ④ 血缘与数据地图 ─────────────────────────────────────── */

/** 抽屉打开时属于哪个源 —— URL 只有 ds 一个键,靠它区分「换源」与「开表」。 */
const drawerDs = ref('')

function openTable(payload: { name: string; ds: string }) {
  drawerDs.value = payload.ds
  values.ds = payload.ds
  values.table = payload.name
}

const tableDrawerOpen = computed(() => values.table !== '')

function closeTableDrawer() {
  values.table = ''
}

function onTableDrawerToggle(v: boolean) {
  if (!v) closeTableDrawer()
}

function onDriftDialogToggle(v: boolean) {
  if (!v) driftAction.value = null
}

function goDatasources() {
  void router.push('/admin/datasources')
}

/* ── 取数编排(懒加载:切到哪个 Tab 才取哪个)────────────── */

function loadActive() {
  if (activeTab.value === 'inbox') void loadInbox()
  else if (activeTab.value === 'coverage') void loadCoverage()
  else if (activeTab.value === 'drift') {
    void loadDriftSources()
    void loadHistory()
  }
}

async function loadDriftSources() {
  await Promise.all(driftSources.value.map((ds) => loadDriftSource(ds)))
}

watch(
  () => [activeTab.value, values.ds, values.kind, values.q, values.sort, values.page] as const,
  () => {
    if (activeTab.value === 'inbox') void loadInbox()
  },
  { immediate: true },
)

watch(
  () => [activeTab.value, values.ds] as const,
  () => {
    if (activeTab.value === 'coverage') void loadCoverage()
  },
  { immediate: true },
)

watch(
  () => [activeTab.value, values.ds, values.status, values.level] as const,
  () => {
    if (activeTab.value === 'drift') {
      void loadDriftSources()
      void loadHistory()
    }
  },
  { immediate: true },
)

watch(
  () => values.ds,
  (next) => {
    // 换源 = 换问题:旧版本窗与漂移抽屉都不再成立;表抽屉只在
    // 「换了源、但表还是上一个源开的那张」时关闭(openTable 自己换源不算)。
    rollbackOpen.value = false
    driftDrawerOpen.value = false
    if (values.table && next !== drawerDs.value) values.table = ''
  },
)

watch(dsNames, () => {
  if (activeTab.value === 'drift') void loadDriftSources()
})

void loadDatasources()
void loadOverview()

function reload() {
  void loadDatasources()
  void loadOverview()
  loadActive()
}

const activeLoading = computed(
  () =>
    inboxLoading.value ||
    coverageLoading.value ||
    historyLoading.value ||
    Object.values(driftMap.value).some((s) => s.loading),
)
</script>

<template>
  <div class="admin-view gov-page" :aria-busy="activeLoading || undefined">
    <PageHeader
      :title="t('govTitle', ui.lang)"
      :breadcrumbs="crumbs"
    >
      <template #description>
        <span>{{ t('govDesc', ui.lang) }}</span>
        <span v-if="asOf" class="gov-asof"> · {{ t('govAsOf', ui.lang, asOf) }}</span>
      </template>
      <template #actions>
        <div class="tab-seg" role="tablist" :aria-label="t('govTitle', ui.lang)">
          <button
            v-for="tab in TABS"
            :key="tab.key"
            type="button"
            role="tab"
            class="seg-btn"
            :class="{ 'is-active': activeTab === tab.key }"
            :aria-selected="activeTab === tab.key"
            @click="setTab(tab.key)"
          >
            {{ t(tab.label, ui.lang) }}
          </button>
        </div>
        <button type="button" class="seg-btn" @click="mapOpen = !mapOpen">
          {{ mapOpen ? t('govMapHide', ui.lang) : t('govMapShow', ui.lang) }}
        </button>
        <el-button :loading="activeLoading" @click="reload">{{ t('refresh', ui.lang) }}</el-button>
      </template>
    </PageHeader>

    <GovernanceMap v-if="mapOpen" />

    <!-- KPI 行:每块是一个入口(点击 = 按类筛选收件箱);null 渲染 —,不渲染 0。 -->
    <section class="gov-kpis" :aria-label="t('govKpiTotal', ui.lang)">
      <KpiTile
        v-for="kind in GOVERNANCE_TODO_KINDS"
        :key="kind"
        :label="kindLabel(kind)"
        :value="kpiValue(kind)"
        :active="activeTab === 'inbox' && values.kind === kind"
        @click="pickKind(kind)"
      />
      <KpiTile
        :label="t('govKpiTotal', ui.lang)"
        :value="kpiTotalText"
        :sub="t('govKpiKindCount', ui.lang, kpiKindCount)"
        :active="activeTab === 'inbox' && values.kind === ''"
        @click="pickKind('')"
      />
    </section>
    <p class="gov-kpi-hint">{{ t('govKpiGoHint', ui.lang) }}</p>

    <!-- ① 收件箱 -->
    <section v-if="activeTab === 'inbox'" class="gov-section">
      <div class="gov-filters">
        <el-select
          v-model="values.kind"
          class="filter-select"
          clearable
          :aria-label="t('govInboxKindAll', ui.lang)"
          :placeholder="t('govInboxKindAll', ui.lang)"
        >
          <el-option
            v-for="k in GOVERNANCE_TODO_KINDS"
            :key="k"
            :value="k"
            :label="kindLabel(k)"
          />
        </el-select>
        <el-select
          v-model="values.sort"
          class="filter-select"
          :aria-label="t('govInboxSortOldest', ui.lang)"
        >
          <el-option
            v-for="s in GOVERNANCE_INBOX_SORTS"
            :key="s"
            :value="s"
            :label="t(SORT_KEY[s], ui.lang)"
          />
        </el-select>
        <el-input
          v-model="values.q"
          class="filter-input"
          clearable
          :placeholder="t('govInboxSearch', ui.lang)"
        />
        <span class="gov-spacer" />
        <span class="gov-page mono">{{ t('govInboxPageOf', ui.lang, pageNum) }}</span>
        <el-button :disabled="pageNum <= 1" @click="values.page = String(pageNum - 1)">
          {{ t('govInboxPrev', ui.lang) }}
        </el-button>
        <el-button :disabled="nextDisabled" @click="values.page = String(pageNum + 1)">
          {{ t('govInboxNext', ui.lang) }}
        </el-button>
      </div>

      <details v-if="inboxDegraded.length" class="gov-degraded">
        <summary>
          {{ t('govDegradedSummary', ui.lang, inboxDegraded.length) }} ·
          {{ t('govDegradedToggle', ui.lang) }}
        </summary>
        <DegradedNotice :items="inboxDegraded" />
      </details>

      <InboxBulkBar
        :selected-count="selected.length"
        :groups="bulkGroups"
        :busy="bulkBusy"
        :results="bulkResults"
        :has-results="bulkHasResults"
        @confirm="runBulk('confirm', $event)"
        @reject="runBulk('reject', $event)"
        @clear="selected = []"
        @retry="retryBulkRow"
      />

      <StatePanel
        v-if="inboxError"
        mode="error"
        :title="t('govPageErrorTitle', ui.lang)"
        :description="t('govPageErrorDesc', ui.lang)"
        :detail="inboxError"
        :retry-text="t('retry', ui.lang)"
        @retry="loadInbox"
      />
      <InboxTable
        v-else
        :items="todos?.items ?? []"
        :loading="inboxLoading"
        :selected="selected"
        :sort="inboxSort"
        :busy-id="busyId"
        :filtered="inboxFiltered"
        :total="todos?.total ?? null"
        :total-floor="totalIsFloor"
        @update:selected="selected = $event"
        @update:sort="onInboxSort"
        @confirm="actOnItem($event, 'confirm')"
        @reject="actOnItem($event, 'reject')"
        @go-drift="goDrift"
        @open-edit="openEdit"
        @clear-filters="clearInboxFilters"
      />
    </section>

    <!-- ② 覆盖与体检 -->
    <section v-else-if="activeTab === 'coverage'" class="gov-section">
      <div class="gov-filters">
        <el-input
          v-model="values.q"
          class="filter-input"
          clearable
          :placeholder="t('govCovSearch', ui.lang)"
        />
        <label class="gov-check">
          <input v-model="values.only_gap" type="checkbox" true-value="1" false-value="">
          {{ t('govCovOnlyGap', ui.lang) }}
        </label>
      </div>

      <details v-if="coverage?.degraded?.length" class="gov-degraded">
        <summary>
          {{ t('govDegradedSummary', ui.lang, coverage.degraded.length) }} ·
          {{ t('govDegradedToggle', ui.lang) }}
        </summary>
        <DegradedNotice
          :items="
            (coverage?.degraded ?? []).map((d) => ({
              block: d.ds ?? 'datasources',
              source: d.ds ?? '',
              error: d.error,
              at: d.at,
            }))
          "
        />
      </details>

      <StatePanel
        v-if="coverageError"
        mode="error"
        :title="t('govPageErrorTitle', ui.lang)"
        :description="t('govPageErrorDesc', ui.lang)"
        :detail="coverageError"
        :retry-text="t('retry', ui.lang)"
        @retry="loadCoverage"
      />
      <!-- sources:null = 源都列不出来 —— 是「未取到」,不是「没有源」。 -->
      <StatePanel
        v-else-if="coverage && coverage.sources === null"
        mode="empty"
        :title="t('govNotFetched', ui.lang)"
        :description="t('govCovLegGap', ui.lang)"
      />
      <CoverageTable
        v-else
        :sources="coverageRows"
        :degraded="coverage?.degraded ?? []"
        :loading="coverageLoading"
        :filtered="coverageFiltered"
        @open-details="openAssets"
        @go-model="goModel"
        @clear-filters="clearCoverageFilters"
      />
    </section>

    <!-- ③ 漂移与版本 -->
    <section v-else-if="activeTab === 'drift'" class="gov-section">
      <div class="gov-filters">
        <el-select
          v-model="values.status"
          class="filter-select"
          :aria-label="t('govDriftFilterStatus', ui.lang)"
        >
          <el-option
            v-for="s in GOVERNANCE_DRIFT_STATUSES"
            :key="s"
            :value="s"
            :label="
              s === 'open'
                ? t('govDriftStatusOpen', ui.lang)
                : s === 'waived'
                  ? t('govDriftStatusWaived', ui.lang)
                  : s === 'resolved'
                    ? t('govDriftStatusResolved', ui.lang)
                    : t('govDriftStatusAll', ui.lang)
            "
          />
        </el-select>
        <el-select
          v-model="values.level"
          class="filter-select"
          clearable
          :aria-label="t('govDriftLevelAll', ui.lang)"
          :placeholder="t('govDriftLevelAll', ui.lang)"
        >
          <el-option value="L1" :label="t('govDriftLevelL1', ui.lang)" />
          <el-option value="L2" :label="t('govDriftLevelL2', ui.lang)" />
          <el-option value="L4" :label="t('govDriftLevelL4', ui.lang)" />
        </el-select>
        <el-input
          v-model="values.q"
          class="filter-input"
          clearable
          :placeholder="t('govInboxSearch', ui.lang)"
        />
        <span class="gov-spacer" />
        <el-button v-if="values.ds" :loading="driftState(values.ds).loading" @click="checkDrift(values.ds)">
          {{ t('govDriftCheck', ui.lang) }}
        </el-button>
        <el-button @click="toggleDeclare">{{ t('govDriftDeclare', ui.lang) }}</el-button>
      </div>

      <p v-if="driftNotice" class="gov-notice" role="status">{{ driftNotice }}</p>
      <p v-if="dsListError" class="gov-warn">
        {{ t('govNotFetched', ui.lang) }}: {{ dsListError }}
      </p>

      <!-- L4 声明:没有检测器,人为告知也要留痕。 -->
      <section v-if="declareOpen" class="gov-declare">
        <p class="gov-declare-desc">{{ t('govDriftDeclareDesc', ui.lang) }}</p>
        <div class="gov-declare-grid">
          <el-select v-model="declareDs" class="filter-select" :aria-label="t('govCovDs', ui.lang)">
            <el-option v-for="d in dsNames" :key="d" :value="d" :label="d" />
          </el-select>
          <el-input
            v-model="declareSubject"
            class="filter-input"
            :placeholder="t('govDriftDeclareSubject', ui.lang)"
          />
          <el-select
            v-model="declareSeverity"
            class="filter-select"
            :aria-label="t('govDriftDeclareSeverity', ui.lang)"
          >
            <el-option value="info" label="info" />
            <el-option value="warning" label="warning" />
            <el-option value="critical" label="critical" />
          </el-select>
          <el-input
            v-model="declareDetail"
            class="filter-input"
            :placeholder="t('govDriftDeclareDetail', ui.lang)"
          />
          <el-button type="primary" :loading="declareBusy" @click="submitDeclare">
            {{ t('govDriftDeclareSubmit', ui.lang) }}
          </el-button>
        </div>
        <p v-if="declareError" class="gov-warn">{{ declareError }}</p>
      </section>

      <StatePanel
        v-if="driftSources.length === 0"
        mode="empty"
        :title="t('govCovEmpty', ui.lang)"
        :description="t('govCovEmptyDesc', ui.lang)"
      />

      <section v-for="ds in driftSources" :key="ds" class="drift-source">
        <div class="drift-source-head">
          <h3 class="drift-source-title mono">{{ ds }}</h3>
          <span v-if="driftState(ds).lastRunAt" class="drift-run mono">
            {{
              t(
                'govDriftRunAt',
                ui.lang,
                fmtDateTime(driftState(ds).lastRunAt) || driftState(ds).lastRunAt,
              )
            }}
          </span>
          <el-button
            size="small"
            :loading="driftState(ds).loading"
            @click="checkDrift(ds)"
          >
            {{ t('govDriftCheck', ui.lang) }}
          </el-button>
        </div>

        <StatePanel
          v-if="driftState(ds).error"
          mode="error"
          :title="t('govDriftError', ui.lang)"
          :detail="driftState(ds).error"
          :retry-text="t('retry', ui.lang)"
          @retry="loadDriftSource(ds)"
        />
        <DriftTable
          v-else
          :ds="ds"
          :items="driftItems(ds)"
          :loading="driftState(ds).loading"
          :checked="driftState(ds).checked"
          :stale="driftState(ds).stale"
          :skip-reason="driftState(ds).skipReason"
          :filtered="driftFiltered"
          @detail="openDriftDetail"
          @resolve="(item) => openDriftAction('resolve', item)"
          @waive="(item) => openDriftAction('waive', item)"
          @recheck="checkDrift(ds)"
          @go-datasources="goDatasources"
          @clear-filters="clearDriftFilters"
        />
      </section>

      <VersionTimeline
        v-if="values.ds"
        :ds="values.ds"
        :entries="history"
        :loading="historyLoading"
        :error="historyError"
        @rollback="openRollback"
        @retry="loadHistory"
      />
      <p v-else class="gov-muted">{{ t('govVerPickDs', ui.lang) }}</p>
    </section>

    <!-- ④ 血缘与数据地图 -->
    <section v-else class="gov-section">
      <div class="gov-filters">
        <el-input
          v-model="values.q"
          class="filter-input"
          clearable
          :placeholder="t('govLinSearch', ui.lang)"
        />
      </div>
      <LineagePanel :q="values.q" :ds="values.ds" @open="openTable" />
      <TableDetailDrawer
        :model-value="tableDrawerOpen"
        :table="values.table"
        :ds="drawerDs || values.ds"
        @update:model-value="onTableDrawerToggle"
      />
    </section>

    <!-- 漂移详情抽屉 -->
    <DriftDrawer
      v-model="driftDrawerOpen"
      :ds="driftDrawerDs"
      :drift-id="driftDrawerId"
      @resolve="(item) => openDriftAction('resolve', item)"
      @waive="(item) => openDriftAction('waive', item)"
    />

    <!-- 覆盖 · 资产体检抽屉 -->
    <AssetHealthPanel v-model="assetsOpen" :ds="assetsDs" />

    <!-- 裁定确认(reason 必填,进审计) -->
    <ConfirmDialog
      :model-value="driftAction !== null"
      :title="
        driftAction?.kind === 'waive'
          ? t('govDriftWaiveTitle', ui.lang)
          : t('govDriftResolveTitle', ui.lang)
      "
      :confirm-text="
        driftAction?.kind === 'waive' ? t('govDriftWaive', ui.lang) : t('govDriftResolve', ui.lang)
      "
      :cancel-text="t('cancel', ui.lang)"
      :danger="driftAction?.kind === 'waive'"
      :loading="driftActing"
      @update:model-value="onDriftDialogToggle"
      @confirm="confirmDriftAction"
    >
      <div class="gov-reason">
        <p class="gov-reason-subject mono">{{ driftAction?.item.subject }}</p>
        <label class="gov-reason-label">{{ t('govDriftReason', ui.lang) }}</label>
        <input v-model="driftReason" class="gov-reason-input" type="text">
        <p class="gov-reason-hint">{{ t('govDriftReasonHint', ui.lang) }}</p>
        <p v-if="driftReasonError" class="gov-warn">{{ driftReasonError }}</p>
      </div>
    </ConfirmDialog>

    <!-- 回滚确认(受影响文件清单运行时取自后端) -->
    <RollbackDialog
      v-model="rollbackOpen"
      :ds="rollbackTarget?.ds ?? ''"
      :sha="rollbackTarget?.sha ?? ''"
      :loading="rollbackBusy"
      :error="rollbackError"
      @confirm="confirmRollback"
    />
  </div>
</template>

<style scoped>
.gov-page {
  padding-bottom: var(--sp-6);
}

.tab-seg,
.win-seg {
  display: inline-flex;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  overflow: hidden;
}
.seg-btn {
  padding: 2px 10px;
  border: none;
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  cursor: pointer;
  white-space: nowrap;
}
.seg-btn + .seg-btn {
  border-left: 1px solid var(--border-subtle);
}
.seg-btn.is-active {
  background: var(--accent-soft, var(--surface-sunken));
  color: var(--accent);
  font-weight: 600;
}

.gov-asof {
  color: var(--text-tertiary);
}

.gov-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--sp-2);
  margin-bottom: 2px;
}
.gov-kpi-hint {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.gov-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.gov-filters {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.filter-select {
  width: 160px;
}
.filter-input {
  width: 220px;
}
.gov-spacer {
  flex: 1;
}
.gov-page {
  font-variant-numeric: tabular-nums;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}
.gov-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.gov-degraded {
  border: 1px solid var(--warn-border, var(--border-default));
  border-radius: var(--r-md);
  padding: var(--sp-1) var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.gov-degraded summary {
  cursor: pointer;
}

.gov-notice {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--accent);
}
.gov-warn {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--danger-text);
  font-weight: 600;
}
.gov-muted {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.gov-declare {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  padding: var(--sp-3);
}
.gov-declare-desc {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.gov-declare-grid {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  align-items: center;
}

.drift-source {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  padding: var(--sp-3);
}
.drift-source-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.drift-source-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
}
.drift-run {
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}

.gov-reason-subject {
  margin: 0 0 var(--sp-2);
  font-weight: 600;
}
.gov-reason-label {
  display: block;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  margin-bottom: 2px;
}
.gov-reason-input {
  width: 100%;
  padding: 4px 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-xs);
}
.gov-reason-hint {
  margin: 4px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.mono {
  font-family: var(--font-mono, monospace);
}
</style>
