<!--
  EvalsetTab — KB console 的「验收集」页签(设计 §9.2)。

  这一页的每个数字都有三态纪律,和 KB 其余页签同一套:
    · ``latest_verdict: null`` = **未测**,不是 "0";没跑过就照实写「未测」;
    · readiness ``available:false`` = **读不出**(资产坏 / run 存储坏),计数与
      覆盖一律渲染 ``—`` —— 补成 0 会把「没读到」洗成「测到了且为零」;
    · 生成报告的 ``status:"empty"`` 只有在没有 ``evalset.busy`` 时才是
      「没东西可收割」;锁竞争必须单独显示(另一个写者握着锁)。

  动作路径全部走服务端既有端点;确认类动作(试跑成本门、核验背书、拒绝、
  退役)一律先过 ConfirmDialog —— 成本与不可逆性要在点击之前可见。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  Archive,
  Check,
  Eye,
  Pencil,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  X,
} from 'lucide-vue-next'
import ConfirmDialog from '../base/ConfirmDialog.vue'
import StatePanel from '../base/StatePanel.vue'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import { notifyError, notifySuccess } from '../../utils/notify'
import { tierBadge } from './evalsetTier'
import {
  confirmEvalsetBatch,
  confirmEvalsetItem,
  createEvalsetItem,
  evalsetErrorCode,
  fetchEvalsetItem,
  fetchEvalsetItems,
  fetchEvalsetReadiness,
  fetchEvalsetRuns,
  fetchGenerateStatus,
  fetchRunStatus,
  generateEvalset,
  patchEvalsetItem,
  previewEvalsetItem,
  rejectEvalsetItem,
  retireEvalsetItem,
  runConfirmation,
  runEvalset,
  vouchEvalsetItem,
  type EvalsetCounts,
  type EvalsetDegraded,
  type EvalsetIssue,
  type EvalsetItem,
  type EvalsetPreview,
  type EvalsetReadiness,
  type EvalsetRunConfirmation,
  type EvalsetTask,
  type GenerateReport,
  type RunSummary,
} from '../../api/evalset'

type I18nKey = Parameters<typeof t>[0]

const props = withDefaults(
  defineProps<{
    datasource: string
    /** URL 里的 tier 过滤(空串 = 全部);确认门的分层选择仍以 runTier 为准。 */
    tier?: string
  }>(),
  { tier: '' },
)

const emit = defineEmits<{
  (e: 'countChange', count: number): void
}>()

const ui = useUiStore()
function tr(key: I18nKey, params?: number | string): string {
  return t(key, ui.lang, params)
}
function badge(tier: string) {
  return tierBadge(tier || 'unknown', (key) => t(key as I18nKey, ui.lang))
}

/* ── 分层 / 计数 ──────────────────────────────────────────────────── */

const SCORING_TIERS = ['verified', 'semantic', 'synthetic'] as const
const TIER_ORDER = ['verified', 'semantic', 'synthetic', 'benchmark', 'unknown'] as const
const scoringTiers = [...SCORING_TIERS]
const runnableTiers = [...SCORING_TIERS]

function tierRank(tier: string): number {
  const index = (TIER_ORDER as readonly string[]).indexOf(tier)
  return index < 0 ? TIER_ORDER.length : index
}

const counts = ref<EvalsetCounts | null>(null)
const byTier = ref<Record<string, { confirmed: number; pending: number }>>({})

/* ── 数据 ─────────────────────────────────────────────────────────── */

const items = ref<EvalsetItem[]>([])
const issues = ref<EvalsetIssue[]>([])
const degraded = ref<EvalsetDegraded[]>([])
const readiness = ref<EvalsetReadiness | null>(null)
const runs = ref<RunSummary[]>([])
const loading = ref(false)
const loadError = ref('')
const selectedQids = ref<string[]>([])
const batchBusy = ref(false)
const batchErrors = ref<{ qid: string; message: string }[]>([])
/** duplicate_of 指向的题目可能不在当前筛选结果里:按 qid 补取展示。 */
const duplicateQuestions = ref<Record<string, string>>({})

const pendingItems = computed(() => items.value.filter((item) => item.status === 'pending'))
const confirmedItems = computed(() => items.value.filter((item) => item.status === 'confirmed'))

/**
 * 可批量确认的行 = 单条确认也走得通的行。`needs_reference` 单条禁用确认、
 * `duplicate_of` 单条要求 force —— 批量既不带 force 也不该绕过这两道闸,
 * 否则选中即注定逐条失败(批量是「少点几次鼠标」,不是放宽判定)。
 */
function batchSelectable(item: EvalsetItem): boolean {
  return item.status === 'pending' && !item.needs_reference && !item.duplicate_of
}

const selectableItems = computed(() => pendingItems.value.filter(batchSelectable))

const allPendingSelected = computed(
  () =>
    selectableItems.value.length > 0 &&
    selectableItems.value.every((item) => selectedQids.value.includes(item.qid)),
)

/** 已确认按 tier 分组;未知 tier 排在已知之后(以实际 tier 为准,不丢行)。 */
const confirmedGroups = computed(() => {
  const groups = new Map<string, EvalsetItem[]>()
  for (const item of confirmedItems.value) {
    const tier = item.tier || 'unknown'
    const bucket = groups.get(tier)
    if (bucket) bucket.push(item)
    else groups.set(tier, [item])
  }
  return [...groups.entries()]
    .sort((a, b) => tierRank(a[0]) - tierRank(b[0]) || a[0].localeCompare(b[0]))
    .map(([tier, groupItems]) => ({ tier, items: groupItems }))
})

const coverage = computed(() => readiness.value?.coverage ?? null)
const coverageNoModel = computed(
  () => (readiness.value?.degraded ?? []).some((entry) => entry.code === 'no_semantic_model'),
)

/** latest_runs 的每一项都有三态:null = 未测,对象 = 那一次的摘要。 */
const latestRunRows = computed(() => {
  const latest = readiness.value?.latest_runs ?? {}
  return Object.keys(latest)
    .sort((a, b) => tierRank(a) - tierRank(b) || a.localeCompare(b))
    .map((tier) => ({ tier, run: latest[tier] ?? null }))
})

/** 去重:list / readiness / runs 三处降级可能报同一件事。 */
function dedupeDegraded(entries: EvalsetDegraded[]): EvalsetDegraded[] {
  const seen = new Set<string>()
  const out: EvalsetDegraded[] = []
  for (const entry of entries) {
    const key = `${entry.code}\u0000${entry.message}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(entry)
  }
  return out
}

function errorText(err: unknown): string {
  const code = evalsetErrorCode(err)
  const message =
    err && typeof err === 'object' && 'message' in err
      ? String((err as { message: unknown }).message)
      : String(err ?? '')
  if (code && message && !message.includes(code)) return `${code}: ${message}`
  return message || code
}

/** 在途 load 的序号:同 ds/tier 的并发刷新也按「后发起者赢」排序。 */
let loadSeq = 0

async function load(): Promise<void> {
  // 捕获 load 起点的身份:await 之后 props 可能已经指向另一个源/层,
  // 旧响应绝不能渲染到新名字下(也不能把旧 qid 的计数发出去)。
  const ds = props.datasource
  const tier = props.tier ?? ''
  const seq = ++loadSeq
  const stale = () =>
    seq !== loadSeq || props.datasource !== ds || (props.tier ?? '') !== tier

  if (!ds) {
    items.value = []
    counts.value = null
    byTier.value = {}
    issues.value = []
    degraded.value = []
    readiness.value = null
    runs.value = []
    loadError.value = ''
    selectedQids.value = []
    duplicateQuestions.value = {}
    // 空源是同步收尾:在途的旧 load 会因 seq 失配跳过 finally,这里必须自己落旗。
    loading.value = false
    emit('countChange', 0)
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const filters = tier ? { tier } : {}
    const [list, ready, history] = await Promise.all([
      fetchEvalsetItems(ds, filters),
      fetchEvalsetReadiness(ds),
      fetchEvalsetRuns(ds),
    ])
    if (stale()) return
    items.value = list.items ?? []
    counts.value = list.counts ?? null
    byTier.value = list.by_tier ?? {}
    issues.value = list.issues ?? []
    readiness.value = ready
    runs.value = history.runs ?? []
    duplicateQuestions.value = {}
    await resolveDuplicateQuestions(ds, list.items ?? [], stale)
    if (stale()) return
    degraded.value = dedupeDegraded([
      ...(list.degraded ?? []),
      ...(ready.degraded ?? []),
      ...(history.degraded ?? []),
    ])
    // 选择只保留仍在可批量确认队列里的 id(刷新后行的身份不变,但状态可能已变)
    const selectable = new Set(
      items.value.filter((item) => batchSelectable(item)).map((item) => item.qid),
    )
    selectedQids.value = selectedQids.value.filter((qid) => selectable.has(qid))
  } catch (err) {
    if (stale()) return
    items.value = []
    counts.value = null
    byTier.value = {}
    issues.value = []
    degraded.value = []
    readiness.value = null
    runs.value = []
    loadError.value = errorText(err)
    duplicateQuestions.value = {}
  } finally {
    // 过期响应不碰 loading / 计数;接管这次切换的那次 load 会收尾。
    if (!stale()) {
      loading.value = false
      emit('countChange', items.value.length)
    }
  }
}

watch(
  () => [props.datasource, props.tier] as const,
  () => {
    void load()
  },
  { immediate: true },
)

/* ── 行内动作 ─────────────────────────────────────────────────────── */

const busyQid = ref<Record<string, string>>({})
const expandedSql = ref<Record<string, boolean>>({})

function isBusy(qid: string): boolean {
  return !!busyQid.value[qid]
}
async function withBusy<T>(qid: string, action: string, fn: () => Promise<T>): Promise<T> {
  busyQid.value[qid] = action
  try {
    return await fn()
  } finally {
    delete busyQid.value[qid]
  }
}

function toggleSql(qid: string) {
  expandedSql.value[qid] = !expandedSql.value[qid]
}
function oneLine(sql: string): string {
  const text = String(sql || '').replace(/\s+/g, ' ').trim()
  return text.length > 90 ? `${text.slice(0, 90)}…` : text
}
function tagText(tags: Record<string, unknown> | undefined): string {
  if (!tags || typeof tags !== 'object') return ''
  return Object.entries(tags)
    .map(([key, value]) => {
      const values = Array.isArray(value) ? value.map(String) : [String(value)]
      return `${key}: ${values.join(', ')}`
    })
    .join(' · ')
}
function questionOf(qid: string): string {
  return (
    items.value.find((item) => item.qid === qid)?.question ||
    duplicateQuestions.value[qid] ||
    qid
  )
}

/** 补取「重复对象」的问题文案;读不到就退回 qid(不伪装成完整)。 */
async function resolveDuplicateQuestions(
  ds: string,
  rows: EvalsetItem[],
  stale: () => boolean,
): Promise<void> {
  const known = new Set(rows.map((item) => item.qid))
  const missing = [
    ...new Set(
      rows
        .map((item) => item.duplicate_of)
        .filter(
          (qid): qid is string =>
            !!qid && !known.has(qid) && !duplicateQuestions.value[qid],
        ),
    ),
  ]
  await Promise.all(
    missing.map(async (qid) => {
      try {
        // 用捕获的 ds,不读 live props —— 请求发出后源可能已经切走。
        const other = await fetchEvalsetItem(ds, qid)
        if (stale()) return
        duplicateQuestions.value[qid] = other.question
      } catch {
        /* 读不到就保留 qid —— 但绝不编一句话 */
      }
    }),
  )
}
function cellText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
function canVouch(item: EvalsetItem): boolean {
  // 与后端 ``_VOUCHABLE_ORIGINS`` 同源:人工/pipeline 来源不需要升级。
  return item.reference_origin === 'semantic_compiler' || item.reference_origin === 'llm'
}
function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${Math.round(value * 100)}%`
}

function toggleSelect(qid: string) {
  const item = items.value.find((row) => row.qid === qid)
  if (!item || !batchSelectable(item)) return
  selectedQids.value = selectedQids.value.includes(qid)
    ? selectedQids.value.filter((value) => value !== qid)
    : [...selectedQids.value, qid]
}
function toggleSelectAll() {
  selectedQids.value = allPendingSelected.value
    ? []
    : selectableItems.value.map((item) => item.qid)
}

async function doConfirm(item: EvalsetItem, force: boolean) {
  await withBusy(item.qid, 'confirm', async () => {
    try {
      await confirmEvalsetItem(props.datasource, item.qid, force)
      notifySuccess(tr('kbStatusConfirmed'))
      await load()
    } catch (err) {
      notifyError(errorText(err))
    }
  })
}

async function doBatchConfirm() {
  if (!selectedQids.value.length) return
  batchBusy.value = true
  batchErrors.value = []
  try {
    const result = await confirmEvalsetBatch(props.datasource, selectedQids.value)
    const failures = (result.results ?? [])
      .filter((row) => !row.ok)
      .map((row) => ({
        qid: row.qid,
        message: row.error?.message || row.error?.code || '',
      }))
    batchErrors.value = failures
    if (result.applied) notifySuccess(tr('kbEvalsetBatchApplied', result.applied))
    if (result.failed) notifyError(tr('kbEvalsetBatchFailed', result.failed))
    selectedQids.value = failures.map((row) => row.qid)
    await load()
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    batchBusy.value = false
  }
}

/* ── 拒绝 / 退役 / 核验 ───────────────────────────────────────────── */

const rejectTarget = ref<EvalsetItem | null>(null)
const rejectOpen = ref(false)
const rejectReason = ref('')
const rejecting = ref(false)

function askReject(item: EvalsetItem) {
  rejectTarget.value = item
  rejectReason.value = ''
  rejectOpen.value = true
}
async function doReject() {
  const item = rejectTarget.value
  if (!item) return
  rejecting.value = true
  try {
    await rejectEvalsetItem(props.datasource, item.qid, rejectReason.value)
    rejectOpen.value = false
    notifySuccess(tr('kbEvalsetRejected'))
    await load()
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    rejecting.value = false
  }
}

const retireTarget = ref<EvalsetItem | null>(null)
const retireOpen = ref(false)
const retireReason = ref('')
const retiring = ref(false)

function askRetire(item: EvalsetItem) {
  retireTarget.value = item
  retireReason.value = ''
  retireOpen.value = true
}
async function doRetire() {
  const item = retireTarget.value
  if (!item) return
  retiring.value = true
  try {
    await retireEvalsetItem(props.datasource, item.qid, retireReason.value)
    retireOpen.value = false
    notifySuccess(tr('kbEvalsetRetired'))
    await load()
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    retiring.value = false
  }
}

const vouchTarget = ref<EvalsetItem | null>(null)
const vouchOpen = ref(false)
const vouchNote = ref('')
const vouching = ref(false)

function askVouch(item: EvalsetItem) {
  vouchTarget.value = item
  vouchNote.value = ''
  vouchOpen.value = true
}
async function doVouch() {
  const item = vouchTarget.value
  if (!item) return
  if (!vouchNote.value.trim()) {
    notifyError(tr('kbEvalsetVouchNoteRequired'))
    return
  }
  vouching.value = true
  try {
    await vouchEvalsetItem(props.datasource, item.qid, vouchNote.value)
    vouchOpen.value = false
    notifySuccess(tr('kbEvalsetVouched'))
    await load()
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    vouching.value = false
  }
}

/* ── 新建 / 编辑 ──────────────────────────────────────────────────── */

const formOpen = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const formTarget = ref<EvalsetItem | null>(null)
const formQuestion = ref('')
const formSql = ref('')
const formTags = ref('{}')
const formNote = ref('')
const formForce = ref(false)
const formSaving = ref(false)
const formError = ref('')

function openCreate() {
  formMode.value = 'create'
  formTarget.value = null
  formQuestion.value = ''
  formSql.value = ''
  formTags.value = '{}'
  formNote.value = ''
  formForce.value = false
  formError.value = ''
  formOpen.value = true
}

function openEdit(item: EvalsetItem) {
  formMode.value = 'edit'
  formTarget.value = item
  formQuestion.value = item.question
  formSql.value = item.reference_sql
  formTags.value = JSON.stringify(item.tags ?? {}, null, 2)
  formNote.value = ''
  formForce.value = false
  formError.value = ''
  formOpen.value = true
}

async function saveForm() {
  formError.value = ''
  const question = formQuestion.value.trim()
  const referenceSql = formSql.value.trim()
  if (!question || !referenceSql) {
    formError.value = tr('kbEvalsetRequired')
    return
  }
  let tags: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(formTags.value.trim() || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not-object')
    tags = parsed as Record<string, unknown>
  } catch {
    formError.value = tr('kbEvalsetInvalidTags')
    return
  }
  const isConfirmedEdit =
    formMode.value === 'edit' && formTarget.value?.status === 'confirmed'
  if (isConfirmedEdit && !formNote.value.trim()) {
    // 服务层同一道门:改一道已在评分集合里的题必须说明为什么。
    formError.value = tr('kbEvalsetNoteRequired')
    return
  }
  formSaving.value = true
  try {
    if (formMode.value === 'create') {
      await createEvalsetItem(props.datasource, {
        question,
        reference_sql: referenceSql,
        tags,
        note: formNote.value,
        force: formForce.value,
      })
    } else if (formTarget.value) {
      await patchEvalsetItem(props.datasource, formTarget.value.qid, {
        question,
        reference_sql: referenceSql,
        tags,
        note: formNote.value,
        force: formForce.value,
      })
    }
    formOpen.value = false
    notifySuccess(tr('kbEvalsetSaved'))
    await load()
  } catch (err) {
    formError.value = errorText(err)
  } finally {
    formSaving.value = false
  }
}

/* ── 预览(GET only,R30) ──────────────────────────────────────────── */

const previewOpen = ref(false)
const previewItem = ref<EvalsetItem | null>(null)
const previewData = ref<EvalsetPreview | null>(null)
const previewLoading = ref(false)
const previewError = ref('')

async function openPreview(item: EvalsetItem) {
  previewItem.value = item
  previewData.value = null
  previewError.value = ''
  previewLoading.value = true
  previewOpen.value = true
  try {
    previewData.value = await previewEvalsetItem(props.datasource, item.qid)
  } catch (err) {
    // 403 evalset.masking_refused 也走这里:拒绝时响应里没有行。
    previewError.value = errorText(err)
  } finally {
    previewLoading.value = false
  }
}

/* ── 后台任务:生成 / 试跑 ────────────────────────────────────────── */

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
let disposed = false
onBeforeUnmount(() => {
  disposed = true
})

const generateOpen = ref(false)
const generateBusy = ref(false)
const generateRunning = ref(false)
const generateLimit = ref(30)
const generateRewrite = ref(false)
const generateSources = ref<string[]>(['semantic'])
const generateSourceOptions = ['semantic', 'synthetic', 'production'] as const
const generateReport = ref<GenerateReport | null>(null)
const generateReportBusy = computed(
  () =>
    !!generateReport.value &&
    generateReport.value.status === 'empty' &&
    (generateReport.value.degraded ?? []).some((entry) => entry.code === 'evalset.busy'),
)

function askGenerate() {
  generateOpen.value = true
}

async function doGenerate() {
  if (!generateSources.value.length) {
    notifyError(tr('kbEvalsetRequired'))
    return
  }
  generateBusy.value = true
  // 新一次收割开始 → 旧报告不再代表「当前」;轮询完成前不显示任何旧结论。
  generateReport.value = null
  const datasource = props.datasource
  try {
    await generateEvalset(datasource, {
      sources: generateSources.value,
      limit: generateLimit.value,
      rewrite: generateRewrite.value,
    })
    generateOpen.value = false
    generateBusy.value = false
    await pollGenerate(datasource)
    await load()
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    generateBusy.value = false
  }
}

async function pollGenerate(datasource: string) {
  generateRunning.value = true
  try {
    for (;;) {
      await sleep(2000)
      if (disposed || datasource !== props.datasource) return
      const status = await fetchGenerateStatus(datasource)
      if (status.status === 'running') continue
      generateReport.value = status.report ?? null
      if (status.status === 'error') {
        notifyError(status.error || tr('kbEvalsetGenerateFailed'))
        return
      }
      if (status.status === 'idle') {
        notifyError(tr('kbEvalsetLost'))
        return
      }
      const report = status.report
      if (report && report.status === 'empty') {
        // busy 与「没东西可收割」是两件事:前者是锁竞争,后者才是空结果。
        notifyError(generateReportBusy.value ? tr('kbEvalsetGenerateBusy') : tr('kbEvalsetGenerateEmpty'))
      } else {
        notifySuccess(tr('kbEvalsetGenerateDone'))
      }
      return
    }
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    generateRunning.value = false
  }
}

const runTier = ref<string>(
  props.tier && (SCORING_TIERS as readonly string[]).includes(props.tier)
    ? props.tier
    : 'verified',
)
const runCompileOnly = ref(false)
const runOpen = ref(false)
const runBusy = ref(false)
const runRunning = ref(false)
const runInfo = ref<EvalsetRunConfirmation | null>(null)
const runStage = ref<EvalsetTask | null>(null)

watch(
  () => props.tier,
  (tier) => {
    if (tier && (SCORING_TIERS as readonly string[]).includes(tier)) runTier.value = tier
  },
)

/** 试跑按钮:compile_only 直接发(零 LLM);否则先探一次成本门(400)。 */
async function askRun() {
  if (runRunning.value) return
  if (runCompileOnly.value) {
    await startRun({ tier: runTier.value, compile_only: true })
    return
  }
  runBusy.value = true
  try {
    // 不传 confirm:后端回 400 confirmation_required,detail 带 n_items /
    // tier / model。这是成本门的第一步,不是错误路径。
    await runEvalset(props.datasource, { tier: runTier.value, compile_only: false })
    // 契约保证走不到这里;若后端放宽了门,照常开始轮询而不是假装没发生。
    await pollRun(props.datasource)
  } catch (err) {
    const info = runConfirmation(err)
    if (!info) {
      notifyError(errorText(err))
      return
    }
    runInfo.value = info
    runOpen.value = true
  } finally {
    runBusy.value = false
  }
}

async function startRun(body: { tier: string; compile_only: boolean; confirm?: boolean }) {
  runBusy.value = true
  const datasource = props.datasource
  try {
    await runEvalset(datasource, body)
    runOpen.value = false
    runBusy.value = false
    await pollRun(datasource)
    await load()
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    runBusy.value = false
  }
}

async function confirmRun() {
  await startRun({
    tier: runInfo.value?.tier || runTier.value,
    compile_only: false,
    confirm: true,
  })
}

async function pollRun(datasource: string) {
  runRunning.value = true
  try {
    for (;;) {
      await sleep(2000)
      if (disposed || datasource !== props.datasource) return
      const status = await fetchRunStatus(datasource)
      runStage.value = status
      if (status.status === 'running') continue
      if (status.status === 'done') {
        notifySuccess(tr('kbEvalsetRunDone'))
        return
      }
      if (status.status === 'error') {
        notifyError(status.error || tr('kbEvalsetRunFailed'))
        return
      }
      notifyError(tr('kbEvalsetLost'))
      return
    }
  } catch (err) {
    notifyError(errorText(err))
  } finally {
    runRunning.value = false
  }
}
</script>

<template>
  <section class="evalset-tab" :aria-label="tr('kbEvalsetTitle')">
    <div class="ev-toolbar">
      <h3 class="ev-title">{{ tr('kbEvalsetTitle') }}</h3>
      <span class="spacer" />
      <el-select
        v-model="runTier"
        class="ev-tier-select"
        :aria-label="tr('kbEvalsetRunTier')"
        :disabled="runRunning || runBusy || generateRunning"
      >
        <el-option
          v-for="tierName in runnableTiers"
          :key="tierName"
          :value="tierName"
          :label="badge(tierName).label"
        />
      </el-select>
      <label class="ev-check">
        <input
          v-model="runCompileOnly"
          type="checkbox"
          :disabled="runRunning || runBusy || generateRunning"
        >
        {{ tr('kbEvalsetCompileOnly') }}
      </label>
      <el-button
        :loading="runBusy || runRunning"
        :disabled="generateRunning || generateBusy"
        @click="askRun"
      >
        <Play :size="14" class="btn-icon" />
        {{ tr('kbEvalsetRun') }}
      </el-button>
      <el-button
        :loading="generateBusy || generateRunning"
        :disabled="runBusy || runRunning"
        @click="askGenerate"
      >
        <Sparkles :size="14" class="btn-icon" />
        {{ tr('kbEvalsetGenerate') }}
      </el-button>
      <el-button type="primary" @click="openCreate">{{ tr('kbEvalsetNew') }}</el-button>
    </div>

    <div v-if="runRunning || generateRunning" class="ev-running" role="status">
      <RefreshCw :size="14" class="ev-spin" aria-hidden="true" />
      {{ tr('kbEvalsetRunning') }}
      <span v-if="runStage?.stage" class="ev-muted">{{ runStage.stage }}</span>
      <span v-if="runStage?.progress != null" class="ev-muted">{{ runStage.progress }}%</span>
    </div>

    <div v-if="counts" class="ev-counts" role="group" :aria-label="tr('kbEvalsetTitle')">
      <span class="ev-count">
        <b>{{ counts.pending }}</b> {{ tr('kbStatusPending') }}
      </span>
      <span v-for="tierName in scoringTiers" :key="tierName" class="ev-count">
        <b>{{ byTier[tierName]?.confirmed ?? 0 }}</b>
        <span class="ev-tier" :class="badge(tierName).className">{{ badge(tierName).label }}</span>
      </span>
      <span class="ev-count"><b>{{ counts.rejected }}</b> {{ tr('kbEvalsetRejected') }}</span>
      <span class="ev-count"><b>{{ counts.retired }}</b> {{ tr('kbEvalsetRetired') }}</span>
    </div>

    <div v-if="issues.length" class="ev-banner is-danger" role="alert">
      <TriangleAlert :size="14" aria-hidden="true" />
      <div>
        <strong>{{ tr('kbEvalsetIssues') }}</strong>
        <ul class="ev-plain-list">
          <li v-for="(issue, index) in issues" :key="index">
            {{ issue.code }}<template v-if="issue.path"> ({{ issue.path }})</template>:
            {{ issue.message }}
          </li>
        </ul>
      </div>
    </div>

    <div v-if="degraded.length" class="ev-banner" role="status">
      <TriangleAlert :size="14" aria-hidden="true" />
      <div>
        <div v-for="(entry, index) in degraded" :key="index" class="ev-degraded-row">
          {{ entry.code }}: {{ entry.message }}
        </div>
      </div>
    </div>

    <div v-if="loadError" class="ev-banner is-danger" role="alert">{{ loadError }}</div>

    <StatePanel
      v-if="loading && !items.length && !loadError"
      mode="loading"
      :title="tr('kbEvalsetTitle')"
    />
    <StatePanel
      v-else-if="!loading && !items.length && !loadError && !counts"
      mode="empty"
      :title="tr('kbEvalsetEmpty')"
    />

    <template v-else>
      <!-- ── 待审队列 ─────────────────────────────────────────────── -->
      <section class="ev-section">
        <div class="ev-section-head">
          <h4 class="ev-section-title">{{ tr('kbEvalsetPendingQueue') }}</h4>
          <span class="ev-muted">{{ pendingItems.length }}</span>
          <span class="spacer" />
          <el-button
            v-if="selectedQids.length"
            size="small"
            type="primary"
            :loading="batchBusy"
            @click="doBatchConfirm"
          >
            {{ tr('kbEvalsetConfirmBatch', selectedQids.length) }}
          </el-button>
        </div>

        <div v-if="batchErrors.length" class="ev-banner is-danger" role="alert">
          <div v-for="failure in batchErrors" :key="failure.qid" class="ev-degraded-row">
            {{ failure.qid }}: {{ failure.message }}
          </div>
        </div>

        <div v-if="!pendingItems.length" class="ev-muted ev-empty-line">
          {{ tr('kbEvalsetEmpty') }}
        </div>
        <div v-else class="ev-table-wrap">
          <table class="ev-table">
            <thead>
              <tr>
                <th class="ev-col-check">
                  <input
                    type="checkbox"
                    :checked="allPendingSelected"
                    :aria-label="tr('kbEvalsetSelectAll')"
                    :disabled="!selectableItems.length"
                    @change="toggleSelectAll"
                  >
                </th>
                <th>{{ tr('kbQuestion') }}</th>
                <th>{{ tr('kbEvalsetTier') }}</th>
                <th>{{ tr('kbColSql') }}</th>
                <th>{{ tr('kbTags') }}</th>
                <th>{{ tr('kbTime') }}</th>
                <th>{{ tr('actions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in pendingItems"
                :key="item.qid"
                :class="{ 'is-warn': item.needs_reference, 'is-dup': item.duplicate_of }"
              >
                <td class="ev-col-check">
                  <input
                    type="checkbox"
                    :checked="selectedQids.includes(item.qid)"
                    :aria-label="item.question"
                    :disabled="!batchSelectable(item)"
                    @change="toggleSelect(item.qid)"
                  >
                </td>
                <td class="ev-cell-question">
                  <div class="ev-question">{{ item.question }}</div>
                  <div class="ev-badges">
                    <span class="ev-source">{{ item.source }}</span>
                    <span class="ev-origin">{{ item.reference_origin }}</span>
                    <span v-if="item.match && item.match !== 'exact'" class="ev-pill is-muted">
                      {{ item.match }}
                    </span>
                    <span v-if="item.needs_reference" class="ev-pill is-warn">
                      {{ tr('kbEvalsetNeedsReference') }}
                    </span>
                    <span v-if="item.leakage" class="ev-pill is-danger">
                      {{ tr('kbEvalsetLeakage') }}
                    </span>
                    <span v-if="item.duplicate_of" class="ev-pill is-warn">
                      {{ tr('kbEvalsetDuplicate') }}
                    </span>
                  </div>
                  <div v-if="item.duplicate_of" class="ev-dup">
                    {{ tr('kbEvalsetDuplicateOf', questionOf(item.duplicate_of)) }}
                  </div>
                </td>
                <td>
                  <span class="ev-tier" :class="badge(item.tier).className">
                    {{ badge(item.tier).label }}
                  </span>
                </td>
                <td class="ev-cell-sql">
                  <button type="button" class="ev-link" @click="toggleSql(item.qid)">
                    {{ expandedSql[item.qid] ? tr('kbEvalsetHideSql') : tr('kbEvalsetShowSql') }}
                  </button>
                  <pre v-if="expandedSql[item.qid]" class="ev-sql">{{ item.reference_sql }}</pre>
                  <span v-else class="ev-sql-line" :title="item.reference_sql">
                    {{ oneLine(item.reference_sql) || '—' }}
                  </span>
                </td>
                <td class="ev-cell-tags">{{ tagText(item.tags) || '—' }}</td>
                <td class="ev-cell-time">{{ fmtDateTime(item.created_at) || '—' }}</td>
                <td class="ev-cell-actions">
                  <button
                    type="button"
                    class="ev-icon-btn"
                    :title="tr('kbEvalsetPreview')"
                    :disabled="isBusy(item.qid)"
                    @click="openPreview(item)"
                  >
                    <Eye :size="14" />
                  </button>
                  <button
                    type="button"
                    class="ev-icon-btn is-ok"
                    :title="tr('kbEvalsetConfirm')"
                    :disabled="item.needs_reference || isBusy(item.qid)"
                    @click="doConfirm(item, false)"
                  >
                    <Check :size="14" />
                  </button>
                  <button
                    v-if="item.duplicate_of"
                    type="button"
                    class="ev-icon-btn is-warn"
                    :title="tr('kbEvalsetForce')"
                    :disabled="item.needs_reference || isBusy(item.qid)"
                    @click="doConfirm(item, true)"
                  >
                    <ShieldCheck :size="14" />
                  </button>
                  <button
                    type="button"
                    class="ev-icon-btn is-danger"
                    :title="tr('kbEvalsetReject')"
                    :disabled="isBusy(item.qid)"
                    @click="askReject(item)"
                  >
                    <X :size="14" />
                  </button>
                  <button
                    type="button"
                    class="ev-icon-btn"
                    :title="tr('kbEvalsetEdit')"
                    :disabled="isBusy(item.qid)"
                    @click="openEdit(item)"
                  >
                    <Pencil :size="14" />
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ── 已确认(按 tier 分组) ───────────────────────────────── -->
      <section class="ev-section">
        <div class="ev-section-head">
          <h4 class="ev-section-title">{{ tr('kbEvalsetConfirmed') }}</h4>
          <span class="ev-muted">{{ confirmedItems.length }}</span>
        </div>
        <div v-if="!confirmedItems.length" class="ev-muted ev-empty-line">
          {{ tr('kbEvalsetEmpty') }}
        </div>
        <div v-for="group in confirmedGroups" :key="group.tier" class="ev-group">
          <div class="ev-group-head">
            <span class="ev-tier" :class="badge(group.tier).className">
              {{ badge(group.tier).label }}
            </span>
            <span class="ev-muted">{{ group.items.length }}</span>
          </div>
          <div class="ev-table-wrap">
            <table class="ev-table">
              <thead>
                <tr>
                  <th>{{ tr('kbQuestion') }}</th>
                  <th>{{ tr('kbEvalsetVerdict') }}</th>
                  <th>{{ tr('kbColProvenance') }}</th>
                  <th>{{ tr('kbTime') }}</th>
                  <th>{{ tr('actions') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in group.items" :key="item.qid">
                  <td class="ev-cell-question">
                    <div class="ev-question">{{ item.question }}</div>
                    <div class="ev-badges">
                      <span v-if="item.leakage" class="ev-pill is-danger">
                        {{ tr('kbEvalsetLeakage') }}
                      </span>
                      <span v-if="item.duplicate_of" class="ev-pill is-warn">
                        {{ tr('kbEvalsetDuplicate') }}
                      </span>
                    </div>
                  </td>
                  <td class="ev-cell-verdict">
                    <template v-if="item.latest_verdict">
                      <span class="ev-verdict">{{ item.latest_verdict.verdict ?? '—' }}</span>
                      <span class="ev-muted">{{ fmtDateTime(item.latest_verdict.at) || '—' }}</span>
                      <span
                        v-if="item.latest_verdict.reference_error"
                        class="ev-pill is-danger"
                        :title="cellText(item.latest_verdict.reference_error)"
                      >
                        ref
                      </span>
                    </template>
                    <span v-else class="ev-pill is-muted">{{ tr('kbEvalsetUnmeasured') }}</span>
                  </td>
                  <td class="ev-cell-provenance">
                    <span class="ev-source">{{ item.source }}</span>
                    <span class="ev-origin">{{ item.reference_origin }}</span>
                  </td>
                  <td class="ev-cell-time">{{ fmtDateTime(item.created_at) || '—' }}</td>
                  <td class="ev-cell-actions">
                    <button
                      type="button"
                      class="ev-icon-btn"
                      :title="tr('kbEvalsetPreview')"
                      :disabled="isBusy(item.qid)"
                      @click="openPreview(item)"
                    >
                      <Eye :size="14" />
                    </button>
                    <button
                      type="button"
                      class="ev-icon-btn"
                      :title="tr('kbEvalsetEdit')"
                      :disabled="isBusy(item.qid)"
                      @click="openEdit(item)"
                    >
                      <Pencil :size="14" />
                    </button>
                    <button
                      type="button"
                      class="ev-icon-btn is-ok"
                      :title="tr('kbEvalsetVouch')"
                      :disabled="!canVouch(item) || isBusy(item.qid)"
                      @click="askVouch(item)"
                    >
                      <ShieldCheck :size="14" />
                    </button>
                    <button
                      type="button"
                      class="ev-icon-btn is-danger"
                      :title="tr('kbEvalsetRetire')"
                      :disabled="isBusy(item.qid)"
                      @click="askRetire(item)"
                    >
                      <Archive :size="14" />
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── 就绪 / 覆盖 ─────────────────────────────────────────── -->
      <section v-if="readiness" class="ev-section">
        <div class="ev-section-head">
          <h4 class="ev-section-title">{{ tr('kbEvalsetCoverage') }}</h4>
          <span v-if="!readiness.available" class="ev-pill is-danger">
            {{ tr('kbEvalsetUnavailable') }}
          </span>
          <span v-if="readiness.evalset_rev" class="ev-muted">
            rev {{ readiness.evalset_rev }}
          </span>
        </div>

        <div class="ev-ready-counts">
          <span>{{ tr('kbStatusPending') }} {{ readiness.counts?.pending ?? '—' }}</span>
          <span>{{ tr('kbStatusConfirmed') }} {{ readiness.counts?.confirmed ?? '—' }}</span>
          <span>{{ tr('kbEvalsetRejected') }} {{ readiness.counts?.rejected ?? '—' }}</span>
          <span>{{ tr('kbEvalsetRetired') }} {{ readiness.counts?.retired ?? '—' }}</span>
        </div>

        <div v-if="coverage" class="ev-cov-grid">
          <div class="ev-cov-block">
            <span class="ev-cov-label">{{ tr('kbEvalsetCoveredMetrics') }}</span>
            <span class="ev-cov-value">{{ coverage.metrics.covered }} / {{ coverage.metrics.total }}</span>
            <div v-if="coverage.metrics.missing.length" class="ev-missing">
              {{ tr('kbEvalsetMissing') }}: {{ coverage.metrics.missing.join(', ') }}
            </div>
          </div>
          <div class="ev-cov-block">
            <span class="ev-cov-label">{{ tr('kbEvalsetCoveredDimensions') }}</span>
            <span class="ev-cov-value">
              {{ coverage.dimensions.covered }} / {{ coverage.dimensions.total }}
            </span>
            <div v-if="coverage.dimensions.missing.length" class="ev-missing">
              {{ tr('kbEvalsetMissing') }}: {{ coverage.dimensions.missing.join(', ') }}
            </div>
          </div>
          <div class="ev-cov-block">
            <span class="ev-cov-label">{{ tr('kbEvalsetCoveredGrains') }}</span>
            <span class="ev-cov-value">
              {{ coverage.time_grains.covered.length }} / {{ coverage.time_grains.covered.length + coverage.time_grains.missing.length }}
            </span>
            <div v-if="coverage.time_grains.missing.length" class="ev-missing">
              {{ tr('kbEvalsetMissing') }}: {{ coverage.time_grains.missing.join(', ') }}
            </div>
          </div>
          <div class="ev-cov-block">
            <span class="ev-cov-label">{{ tr('kbEvalsetCoveredPatterns') }}</span>
            <span class="ev-cov-value">
              {{ coverage.patterns.covered.length }} / {{ coverage.patterns.covered.length + coverage.patterns.missing.length }}
            </span>
            <div v-if="coverage.patterns.missing.length" class="ev-missing">
              {{ tr('kbEvalsetMissing') }}: {{ coverage.patterns.missing.join(', ') }}
            </div>
          </div>
          <div class="ev-cov-block">
            <span class="ev-cov-label">{{ tr('kbEvalsetUntagged') }}</span>
            <span class="ev-cov-value">{{ coverage.untagged }}</span>
          </div>
        </div>
        <div v-else class="ev-muted">
          {{ coverageNoModel ? tr('kbEvalsetNoModel') : tr('kbEvalsetUnavailable') }}
        </div>

        <div class="ev-runs">
          <div class="ev-subtitle">{{ tr('kbEvalsetLatestRuns') }}</div>
          <div v-for="entry in latestRunRows" :key="entry.tier" class="ev-run-row">
            <span class="ev-tier" :class="badge(entry.tier).className">
              {{ badge(entry.tier).label }}
            </span>
            <template v-if="entry.run">
              <span class="ev-muted">{{ fmtDateTime(entry.run.at) || '—' }}</span>
              <span>n {{ entry.run.n ?? '—' }}</span>
              <span>EX {{ pct(entry.run.ex) }}</span>
              <span>compile {{ pct(entry.run.compile_hit) }}</span>
              <span>completion {{ pct(entry.run.completion) }}</span>
              <span v-if="entry.run.reference_errors" class="ev-pill is-warn">
                ref error {{ entry.run.reference_errors }}
              </span>
              <span v-if="entry.run.kb_rev_changed_mid_run" class="ev-changed">
                {{ tr('kbEvalsetKbChanged') }}
              </span>
            </template>
            <span v-else class="ev-pill is-muted">{{ tr('kbEvalsetUnmeasured') }}</span>
          </div>
        </div>
      </section>

      <!-- ── 试跑历史 ───────────────────────────────────────────── -->
      <section class="ev-section">
        <h4 class="ev-section-title">{{ tr('kbEvalsetRunsHistory') }}</h4>
        <div v-if="!runs.length" class="ev-muted ev-empty-line">
          {{ tr('kbEvalsetRunsEmpty') }}
        </div>
        <div v-else class="ev-table-wrap">
          <table class="ev-table">
            <thead>
              <tr>
                <th>{{ tr('kbEvalsetTier') }}</th>
                <th>{{ tr('kbTime') }}</th>
                <th>n</th>
                <th>EX</th>
                <th>compile</th>
                <th>completion</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(run, index) in runs" :key="run.run_id || index">
                <td>
                  <span class="ev-tier" :class="badge(run.tier || '').className">
                    {{ badge(run.tier || '').label }}
                  </span>
                </td>
                <td class="ev-cell-time">{{ fmtDateTime(run.at) || '—' }}</td>
                <td>{{ run.n ?? '—' }}</td>
                <td>{{ pct(run.ex) }}</td>
                <td>{{ pct(run.compile_hit) }}</td>
                <td>{{ pct(run.completion) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <!-- ── 最近的生成报告(busy 与 empty 分开) ─────────────────── -->
    <div
      v-if="generateReport && !generateRunning"
      class="ev-report"
      :class="{ 'is-warn': generateReportBusy }"
      role="status"
    >
      <div class="ev-report-head">
        <strong>{{ tr('kbEvalsetGenerateReport') }}</strong>
        <span class="ev-pill">{{ generateReport.status }}</span>
      </div>
      <div v-if="generateReportBusy" class="ev-report-busy">
        {{ tr('kbEvalsetGenerateBusy') }}
      </div>
      <div v-else-if="generateReport.status === 'empty'" class="ev-muted">
        {{ tr('kbEvalsetGenerateEmpty') }}
      </div>
      <div class="ev-report-counts">
        <span>{{ tr('kbEvalsetReportAdded', generateReport.added) }}</span>
        <span>{{ tr('kbEvalsetReportSkipped', generateReport.skipped) }}</span>
        <span>{{ tr('kbEvalsetReportDuplicates', generateReport.duplicates) }}</span>
        <span>{{ tr('kbEvalsetReportConflicts', generateReport.conflicts) }}</span>
        <span>{{ tr('kbEvalsetReportLeakage', generateReport.leakage) }}</span>
      </div>
      <ul v-if="generateReport.issues?.length" class="ev-plain-list">
        <li v-for="(issue, index) in generateReport.issues" :key="index">
          {{ issue.code }}: {{ issue.message }}
        </li>
      </ul>
    </div>

    <!-- ── 新建 / 编辑 ─────────────────────────────────────────── -->
    <el-dialog
      v-model="formOpen"
      :title="formMode === 'create' ? tr('kbEvalsetNewTitle') : tr('kbEvalsetEditTitle')"
      width="620"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveForm">
        <el-form-item :label="tr('kbQuestion')">
          <el-input v-model="formQuestion" />
        </el-form-item>
        <el-form-item :label="tr('kbColSql')">
          <el-input v-model="formSql" type="textarea" :rows="5" class="ev-mono" />
        </el-form-item>
        <el-form-item :label="tr('kbTags')">
          <el-input
            v-model="formTags"
            type="textarea"
            :rows="2"
            :placeholder="tr('kbEvalsetTagsHint')"
            class="ev-mono"
          />
        </el-form-item>
        <el-form-item :label="tr('kbNote')">
          <el-input v-model="formNote" type="textarea" :rows="2" />
        </el-form-item>
        <el-checkbox v-model="formForce">{{ tr('kbEvalsetForce') }}</el-checkbox>
        <div v-if="formError" class="ev-banner is-danger" role="alert">{{ formError }}</div>
      </el-form>
      <template #footer>
        <el-button @click="formOpen = false">{{ tr('cancel') }}</el-button>
        <el-button type="primary" :loading="formSaving" @click="saveForm">
          {{ tr('save') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── 预览 ────────────────────────────────────────────────── -->
    <el-dialog v-model="previewOpen" :title="tr('kbEvalsetPreview')" width="820" class="admin-dialog">
      <div v-if="previewLoading" class="ev-muted">{{ tr('kbEvalsetRunning') }}</div>
      <div v-else-if="previewError" class="ev-banner is-danger" role="alert">
        {{ previewError }}
      </div>
      <template v-else-if="previewData">
        <div class="ev-preview-meta">
          <strong class="ev-question">{{ previewItem?.question }}</strong>
          <span class="ev-pill" :class="previewData.masking === 'off' ? 'is-muted' : 'is-ok'">
            {{ previewData.masking === 'off' ? tr('kbEvalsetMaskingOff') : tr('kbEvalsetMasked') }}
          </span>
          <span class="ev-muted">{{ previewData.rows.length }}</span>
        </div>
        <div class="ev-preview-scroll">
          <table class="ev-table is-preview">
            <thead>
              <tr>
                <th v-for="column in previewData.columns" :key="column">{{ column }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!previewData.rows.length">
                <td :colspan="Math.max(1, previewData.columns.length)">
                  {{ tr('kbEvalsetPreviewEmpty') }}
                </td>
              </tr>
              <tr v-for="(row, rowIndex) in previewData.rows" :key="rowIndex">
                <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cellText(cell) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </el-dialog>

    <!-- ── 试跑成本确认(题数 / tier / 模型 / compile_only) ─────── -->
    <ConfirmDialog
      v-model="runOpen"
      :title="tr('kbEvalsetRunConfirm')"
      :confirm-text="tr('kbEvalsetRun')"
      :cancel-text="tr('cancel')"
      :loading="runBusy"
      @confirm="confirmRun"
    >
      <dl class="ev-dl">
        <div>
          <dt>{{ tr('kbEvalsetRunNItems') }}</dt>
          <dd>{{ runInfo?.n_items ?? '—' }}</dd>
        </div>
        <div>
          <dt>{{ tr('kbEvalsetRunTier') }}</dt>
          <dd>
            <span v-if="runInfo" class="ev-tier" :class="badge(runInfo.tier).className">
              {{ badge(runInfo.tier).label }}
            </span>
            <span v-else>—</span>
          </dd>
        </div>
        <div>
          <dt>{{ tr('kbEvalsetRunModel') }}</dt>
          <dd>{{ runInfo?.model || '—' }}</dd>
        </div>
        <div>
          <dt>{{ tr('kbEvalsetCompileOnly') }}</dt>
          <dd>{{ tr('kbEvalsetCompileOnlyNo') }}</dd>
        </div>
      </dl>
      <template #impact>{{ tr('kbEvalsetRunConfirmImpact') }}</template>
    </ConfirmDialog>

    <!-- ── 从历史收割 ──────────────────────────────────────────── -->
    <ConfirmDialog
      v-model="generateOpen"
      :title="tr('kbEvalsetGenerate')"
      :confirm-text="tr('kbEvalsetGenerate')"
      :cancel-text="tr('cancel')"
      :loading="generateBusy"
      @confirm="doGenerate"
    >
      <div class="ev-form-row">
        <span>{{ tr('kbEvalsetGenerateLimit') }}</span>
        <el-input-number
          v-model="generateLimit"
          :min="1"
          :max="100"
          size="small"
          :disabled="generateBusy"
        />
      </div>
      <div class="ev-form-row">
        <label v-for="source in generateSourceOptions" :key="source" class="ev-check">
          <input
            v-model="generateSources"
            type="checkbox"
            :value="source"
            :disabled="generateBusy"
          >
          {{ source }}
        </label>
      </div>
      <label class="ev-check">
        <input v-model="generateRewrite" type="checkbox" :disabled="generateBusy">
        {{ tr('kbEvalsetRewrite') }}
      </label>
      <template #impact>{{ tr('kbEvalsetGenerateImpact') }}</template>
    </ConfirmDialog>

    <!-- ── 拒绝 ────────────────────────────────────────────────── -->
    <ConfirmDialog
      v-model="rejectOpen"
      :title="tr('kbEvalsetReject')"
      :confirm-text="tr('kbEvalsetReject')"
      :cancel-text="tr('cancel')"
      danger
      :loading="rejecting"
      @confirm="doReject"
    >
      <p class="ev-question">{{ rejectTarget?.question }}</p>
      <el-input
        v-model="rejectReason"
        type="textarea"
        :rows="3"
        :placeholder="tr('kbEvalsetRejectReason')"
      />
      <template #impact>{{ tr('kbEvalsetRejectImpact') }}</template>
    </ConfirmDialog>

    <!-- ── 退役 ────────────────────────────────────────────────── -->
    <ConfirmDialog
      v-model="retireOpen"
      :title="tr('kbEvalsetRetire')"
      :confirm-text="tr('kbEvalsetRetire')"
      :cancel-text="tr('cancel')"
      danger
      :loading="retiring"
      @confirm="doRetire"
    >
      <p class="ev-question">{{ retireTarget?.question }}</p>
      <el-input
        v-model="retireReason"
        type="textarea"
        :rows="3"
        :placeholder="tr('kbEvalsetRetireReason')"
      />
      <template #impact>{{ tr('kbEvalsetRetireImpact') }}</template>
    </ConfirmDialog>

    <!-- ── 核验 SQL(第二次确认) ───────────────────────────────── -->
    <ConfirmDialog
      v-model="vouchOpen"
      :title="tr('kbEvalsetVouchTitle')"
      :confirm-text="tr('kbEvalsetVouch')"
      :cancel-text="tr('cancel')"
      :loading="vouching"
      @confirm="doVouch"
    >
      <p class="ev-question">{{ vouchTarget?.question }}</p>
      <el-input
        v-model="vouchNote"
        type="textarea"
        :rows="3"
        :placeholder="tr('kbEvalsetVouchNote')"
      />
      <template #impact>{{ tr('kbEvalsetVouchImpact') }}</template>
    </ConfirmDialog>
  </section>
</template>

<style scoped>
.evalset-tab {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.ev-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.ev-toolbar .spacer {
  flex: 1;
}
.ev-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.ev-tier-select {
  width: 150px;
}

.ev-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  cursor: pointer;
}

.ev-running {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.ev-spin {
  animation: ev-rotate 0.9s linear infinite;
}
@keyframes ev-rotate {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .ev-spin {
    animation: none;
  }
}

.ev-counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.ev-count {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.ev-count b {
  color: var(--text-primary);
}

.ev-banner {
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.ev-banner.is-danger {
  border-color: var(--danger-border, var(--danger));
  background: var(--danger-bg);
  color: var(--danger-text);
}
.ev-plain-list {
  margin: var(--sp-1) 0 0;
  padding-left: var(--sp-4);
}

.ev-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.ev-section-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.ev-section-head .spacer {
  flex: 1;
}
.ev-section-title {
  margin: 0;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
}
.ev-subtitle {
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: var(--sp-1);
}
.ev-empty-line {
  padding: var(--sp-2) 0;
}

.ev-table-wrap {
  overflow-x: auto;
}
.ev-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-2xs);
}
.ev-table th {
  text-align: left;
  padding: var(--sp-1) var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  font-weight: 500;
  white-space: nowrap;
}
.ev-table td {
  padding: var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-primary);
  vertical-align: top;
}
.ev-table tr.is-warn {
  background: var(--warn-bg);
}
.ev-table tr.is-dup td:first-child {
  box-shadow: inset 3px 0 0 var(--warn-border, var(--warn));
}
.ev-col-check {
  width: 28px;
}
.ev-cell-question {
  min-width: 240px;
}
.ev-question {
  font-weight: 500;
  line-height: var(--lh-tight);
  overflow-wrap: anywhere;
}
.ev-badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  margin-top: var(--sp-1);
}
.ev-source,
.ev-origin {
  padding: 0 var(--sp-1);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
}
.ev-origin {
  background: var(--accent-soft);
  color: var(--accent-active);
}
.ev-dup {
  margin-top: var(--sp-1);
  color: var(--warn-text, var(--warn));
}
.ev-cell-sql {
  min-width: 220px;
  max-width: 340px;
}
.ev-link {
  padding: 0;
  color: var(--accent-active);
  font-size: var(--fs-2xs);
  text-decoration: underline;
}
.ev-sql {
  margin: var(--sp-1) 0 0;
  padding: var(--sp-2);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-2xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.ev-sql-line {
  display: block;
  margin-top: var(--sp-1);
  color: var(--text-tertiary);
  font-family: var(--font-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ev-cell-tags {
  max-width: 180px;
  overflow-wrap: anywhere;
}
.ev-cell-time {
  white-space: nowrap;
}
.ev-cell-verdict {
  min-width: 160px;
}
.ev-verdict {
  display: block;
  font-family: var(--font-mono, monospace);
}
.ev-cell-provenance {
  white-space: nowrap;
}
.ev-cell-actions {
  white-space: nowrap;
}

.ev-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  margin-right: var(--sp-1);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-secondary);
}
.ev-icon-btn:not(:disabled):hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.ev-icon-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.ev-icon-btn.is-ok {
  color: var(--ok-text);
}
.ev-icon-btn.is-warn {
  color: var(--warn-text);
}
.ev-icon-btn.is-danger {
  color: var(--danger-text);
}
.ev-icon-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

.ev-tier {
  display: inline-flex;
  align-items: center;
  padding: 0 var(--sp-1);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  font-weight: 500;
  white-space: nowrap;
}
.tier-verified {
  background: var(--ok-bg);
  color: var(--ok-text);
}
.tier-semantic {
  background: var(--accent-soft);
  color: var(--accent-active);
}
.tier-synthetic {
  background: var(--warn-bg);
  color: var(--warn-text);
}
.tier-benchmark {
  background: var(--surface-muted);
  color: var(--text-secondary);
}
.tier-unknown {
  background: transparent;
  border: 1px dashed var(--border-strong);
  color: var(--text-secondary);
}

.ev-pill {
  display: inline-flex;
  align-items: center;
  padding: 0 var(--sp-1);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  white-space: nowrap;
  background: var(--surface-muted);
  color: var(--text-secondary);
}
.ev-pill.is-ok {
  background: var(--ok-bg);
  color: var(--ok-text);
}
.ev-pill.is-warn {
  background: var(--warn-bg);
  color: var(--warn-text);
}
.ev-pill.is-danger {
  background: var(--danger-bg);
  color: var(--danger-text);
}
.ev-pill.is-muted {
  background: var(--surface-muted);
  color: var(--text-secondary);
}

.ev-group {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.ev-group-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.ev-ready-counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.ev-cov-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-2);
}
.ev-cov-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}
.ev-cov-label {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.ev-cov-value {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.ev-missing {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  overflow-wrap: anywhere;
}
.ev-runs {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.ev-run-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
  border-bottom: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
}
.ev-changed {
  padding: 0 var(--sp-2);
  border-radius: var(--r-sm);
  background: var(--danger-bg);
  color: var(--danger-text);
  font-weight: 600;
}
.ev-muted {
  color: var(--text-secondary);
}

.ev-report {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  font-size: var(--fs-2xs);
}
.ev-report.is-warn {
  border-color: var(--warn-border, var(--warn));
  background: var(--warn-bg);
}
.ev-report-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.ev-report-busy {
  color: var(--warn-text);
  font-weight: 600;
}
.ev-report-counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  color: var(--text-secondary);
}

.ev-dl {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.ev-dl > div {
  display: flex;
  gap: var(--sp-3);
}
.ev-dl dt {
  flex: none;
  width: 90px;
  color: var(--text-secondary);
}
.ev-dl dd {
  margin: 0;
  color: var(--text-primary);
}
.ev-form-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.ev-preview-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  flex-wrap: wrap;
}
.ev-preview-scroll {
  max-height: 50vh;
  overflow: auto;
}
.ev-mono :deep(textarea) {
  font-family: var(--font-mono, monospace);
}
</style>
