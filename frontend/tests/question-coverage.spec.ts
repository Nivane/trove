/**
 * QuestionCoverageView (/admin/question-coverage) — 逐题编译级体检页。
 *
 * 钉住的是页面**自己的**行为（后端契约在 tests/api/test_question_coverage.py）：
 *   · 成本确认是开始的前置闸：未勾选时按钮禁用；勾选后 POST 带 confirm_cost=true；
 *   · 覆盖率口径原样渲染：`rate: null` 渲染「无样本 · 无法计算」，绝不显示 0%；
 *   · 缺口草稿只发 `{all: true}` / `{seqs}`，弹窗写明 pending 不自动生效；
 *   · 运行中状态可取消（协作取消的文案如实），中断态给出「重新运行」；
 *   · 对比上一轮只展示后端给的 Δ 与 digest 提示，页面自己不算口径；
 *   · CSV 走 multipart（apiPostForm），解析预览来自后端而非前端计数；
 *   · 路由/导航/双语文案就位，且该页与 analyst 无关（admin-only）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import {
  createMemoryHistory,
  createRouter,
  type RouteRecordRaw,
  type Router,
} from 'vue-router'

vi.mock('../src/api/http', () => ({
  apiFetch: vi.fn(),
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPostForm: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

import { apiGet, apiPost, apiPostForm } from '../src/api/http'
import { messages } from '../src/i18n'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import { NAV_ITEMS, navItemsFor } from '../src/components/layout/navModel'
import { requiredRoles, router as appRouter } from '../src/router'
import QuestionCoverageView from '../src/views/admin/QuestionCoverageView.vue'
import type {
  CoverageDiff,
  CoverageDraftsResult,
  CoverageGapGroup,
  CoverageOutcome,
  CoverageParsed,
  CoveragePreflight,
  CoverageReport,
  CoverageResult,
  CoverageRunCreated,
  CoverageRunList,
  CoverageRunMeta,
  CoverageStatus,
  CoverageSummary,
} from '../src/api/questionCoverage'

const LANG = 'en' as const
const DS = 'demo'
const RUN = 'run-current'
const BASE = 'run-previous'
const EVALUATED: CoverageOutcome[] = [
  'certified',
  'compiled',
  'partial',
  'soft_miss',
  'refused',
  'unmatched',
]

/* ── fixtures ─────────────────────────────────────────────────────────── */

function countMap(
  overrides: Partial<Record<CoverageOutcome, number>> = {},
): Record<CoverageOutcome, number> {
  return {
    certified: 0,
    compiled: 0,
    partial: 0,
    soft_miss: 0,
    refused: 0,
    unmatched: 0,
    not_query: 0,
    unplanned: 0,
    error: 0,
    skipped: 0,
    ...overrides,
  }
}

const FULL_METRICS: CoverageSummary['metrics'] = {
  deterministic_coverage: { num: 2, den: 2, rate: 1 },
  governed_coverage: { num: 2, den: 2, rate: 1 },
  gap_rate: { num: 0, den: 2, rate: 0 },
  refusal_rate: { num: 0, den: 2, rate: 0 },
  unmatched_rate: { num: 0, den: 2, rate: 0 },
  passthrough_rate: { num: 0, den: 2, rate: 0 },
}

const EMPTY_METRICS: CoverageSummary['metrics'] = {
  deterministic_coverage: { num: 0, den: 0, rate: null },
  governed_coverage: { num: 0, den: 0, rate: null },
  gap_rate: { num: 0, den: 0, rate: null },
  refusal_rate: { num: 0, den: 0, rate: null },
  unmatched_rate: { num: 0, den: 0, rate: null },
  passthrough_rate: { num: 0, den: 0, rate: null },
}

function makeSummary(patch: Partial<CoverageSummary> = {}): CoverageSummary {
  const counts = patch.counts ?? countMap({ certified: 1, compiled: 1 })
  const byOutcome = (Object.keys(counts) as CoverageOutcome[]).map((outcome) => ({
    outcome,
    count: counts[outcome],
    evaluated: EVALUATED.includes(outcome),
  }))
  return {
    taxonomy_version: 1,
    question_total: 2,
    evaluated: 2,
    unmeasured: 0,
    by_outcome: byOutcome,
    by_miss_reason: [],
    by_miss_class: { hard: 0, soft: 0 },
    metrics: FULL_METRICS,
    warnings: [],
    llm_calls_total: 3,
    ...patch,
    counts,
  }
}

const RESULTS: CoverageResult[] = [
  {
    seq: 0,
    question: 'How many loans?',
    outcome: 'certified',
    outcome_reason: 'fast_path',
    intent: 'query',
    plan_typed: false,
    miss_class: '',
    miss_reason: '',
    miss_component: '',
    template_name: 'count_loans',
    llm_calls: 0,
    elapsed_ms: 12,
    gaps: [],
    detail: {
      matched_tables: ['loan'],
      sql_preview: 'SELECT COUNT(*) FROM loan',
      compile_meta: { outcome: 'compiled' },
    },
  },
  {
    seq: 1,
    question: 'Total amount by region?',
    outcome: 'compiled',
    outcome_reason: 'compile_met',
    intent: 'query',
    plan_typed: true,
    miss_class: '',
    miss_reason: '',
    miss_component: '',
    template_name: '',
    llm_calls: 1,
    elapsed_ms: 812,
    gaps: [],
    detail: {
      matched_tables: ['loan', 'account'],
      plan_preview: 'QueryPlan(metric=total_amount, dim=region)',
      plan_validation: { status: 'ok', errors: [] },
      compile_meta: { outcome: 'compiled' },
    },
  },
]

const GAP_GROUPS: CoverageGapGroup[] = [
  {
    kind: 'metric',
    reason: 'no_metric_match',
    component: 'loan_amount',
    label: { zh: '未声明的指标', en: 'Undeclared metric' },
    count: 1,
    draftable: true,
    specs: [{ kind: 'metric', name: 'loan_amount' }],
    workbench: '/admin/semantic?ds=demo&tab=datasets&q=loan_amount',
    samples: ['How many loans?'],
    seqs: [0],
  },
]

function runMeta(id: string, patch: Partial<CoverageRunMeta> = {}): CoverageRunMeta {
  return {
    run_id: id,
    datasource: DS,
    status: 'done',
    created_by: 'admin',
    created_at: '2026-10-18T10:00:00Z',
    started_at: '2026-10-18T10:00:01Z',
    finished_at: '2026-10-18T10:00:30Z',
    heartbeat_at: '2026-10-18T10:00:30Z',
    cancel_requested: false,
    question_count: 2,
    processed_count: 2,
    model: 'deepseek-chat',
    semantic_digest: 'digest-current',
    taxonomy_version: 1,
    summary: makeSummary(),
    error: '',
    task_id: 'task-1',
    questions: ['How many loans?', 'Total amount by region?'],
    ...patch,
  }
}

function makeStatus(patch: Partial<CoverageStatus> = {}): CoverageStatus {
  return {
    run_id: RUN,
    datasource: DS,
    status: 'done',
    created_by: 'admin',
    created_at: '2026-10-18T10:00:00Z',
    started_at: '2026-10-18T10:00:01Z',
    finished_at: '2026-10-18T10:00:30Z',
    heartbeat_at: '2026-10-18T10:00:30Z',
    cancel_requested: false,
    question_count: 2,
    processed_count: 2,
    model: 'deepseek-chat',
    semantic_digest: 'digest-current',
    taxonomy_version: 1,
    error: '',
    counts: countMap({ certified: 1, compiled: 1 }),
    llm_calls_total: 3,
    task: null,
    summary: makeSummary(),
    ...patch,
  }
}

function makeReport(patch: Partial<CoverageReport> = {}): CoverageReport {
  return {
    run: runMeta(RUN, { previous_done_run_id: BASE }),
    summary: makeSummary(),
    results: RESULTS,
    gap_groups: GAP_GROUPS,
    ...patch,
  }
}

function makeDiff(patch: Partial<CoverageDiff> = {}): CoverageDiff {
  return {
    run_id: RUN,
    base_run_id: BASE,
    reason: '',
    base_created_at: '2026-10-17T10:00:00Z',
    base_semantic_digest: 'digest-previous',
    semantic_digest: 'digest-current',
    semantic_changed: true,
    base_taxonomy_version: 1,
    taxonomy_version: 1,
    taxonomy_changed: false,
    matched: 2,
    base_only: 0,
    current_only: 0,
    changes: { improved: 1, regressed: 0, changed: 0, same: 1, not_comparable: 0 },
    questions: [
      {
        seq: 0,
        base_seq: 0,
        question: 'How many loans?',
        base_outcome: 'partial',
        outcome: 'certified',
        base_reason: '',
        reason: '',
        change: 'improved',
      },
    ],
    summary_delta: {
      deterministic_coverage: {
        base: { num: 1, den: 2, rate: 0.5 },
        current: { num: 2, den: 2, rate: 1 },
        delta: 0.5,
      },
      governed_coverage: {
        base: { num: 1, den: 2, rate: 0.5 },
        current: { num: 2, den: 2, rate: 1 },
        delta: 0.5,
      },
    },
    ...patch,
  }
}

function makeDrafts(patch: Partial<CoverageDraftsResult> = {}): CoverageDraftsResult {
  return {
    run_id: RUN,
    datasource: DS,
    selected_questions: 1,
    requested: 2,
    truncated: false,
    truncated_assets: 0,
    counts: { created: 1, exists: 1, invalid: 0, not_draftable: 0, stale: 0 },
    results: [
      {
        seq: 0,
        question: 'How many loans?',
        kind: 'metric',
        name: 'loan_amount',
        reason: 'no_metric_match',
        status: 'created',
        draft_id: 'draft-1',
        errors: [],
      },
      {
        seq: 1,
        question: 'Total amount by region?',
        kind: 'field',
        name: 'region',
        reason: 'no_field_match',
        status: 'exists',
        draft_id: 'draft-2',
        errors: [],
      },
    ],
    ...patch,
  }
}

function makePreflight(patch: Partial<CoveragePreflight> = {}): CoveragePreflight {
  return {
    datasource: DS,
    question_count: 2,
    llm_question_count: 1,
    not_query: 0,
    certified: 1,
    unmatched: 0,
    needs_plan: 1,
    failed: 0,
    model: 'deepseek-chat',
    model_source: 'model_fast',
    estimated_calls: { typical: 1, cap: 16 },
    hard_call_cap: 210,
    semantic_digest: 'digest-current',
    blockers: [],
    warnings: [],
    active_run_id: '',
    ...patch,
  }
}

function makeParsed(patch: Partial<CoverageParsed> = {}): CoverageParsed {
  return {
    questions: ['How many loans?', 'Total amount by region?'],
    question_count: 2,
    source_label: 'csv:qs.csv',
    header: ['question'],
    encoding: 'utf-8',
    rows_seen: 2,
    duplicates: 0,
    skipped_blank: 0,
    skipped_long: 0,
    truncated: false,
    warnings: [],
    issues: [],
    issues_truncated: false,
    ...patch,
  }
}

function makeCreated(patch: Partial<CoverageRunCreated> = {}): CoverageRunCreated {
  return {
    run_id: RUN,
    task_id: 'task-1',
    status: 'queued',
    datasource: DS,
    question_count: 2,
    model: 'deepseek-chat',
    estimated_calls: { typical: 1, cap: 16 },
    hard_call_cap: 210,
    semantic_digest: 'digest-current',
    ...patch,
  }
}

/* ── mock routing ─────────────────────────────────────────────────────── */

let datasources: { name: string }[]
let preflightBody: CoveragePreflight
let statusBody: CoverageStatus
let reportBody: CoverageReport
let diffBody: CoverageDiff
let draftsBody: CoverageDraftsResult
let parsedBody: CoverageParsed
let createdBody: CoverageRunCreated
let historyBody: CoverageRunList
const calls = {
  get: [] as string[],
  post: [] as [string, unknown][],
  forms: [] as [string, FormData][],
}

async function apiGetImpl(url: string): Promise<unknown> {
  const u = String(url)
  calls.get.push(u)
  if (u.endsWith('/v1/admin/datasources')) return { datasources }
  if (/\/question-coverage\/runs\/[^/?]+\/status$/.test(u)) return statusBody
  if (/\/question-coverage\/runs\/[^/?]+\/diff(\?|$)/.test(u)) return diffBody
  if (/\/question-coverage\/runs\?/.test(u)) return historyBody
  if (/\/question-coverage\/runs\/[^/?]+$/.test(u)) return reportBody
  throw new Error(`unexpected GET ${u}`)
}

async function apiPostImpl(url: string, body?: unknown): Promise<unknown> {
  const u = String(url)
  calls.post.push([u, body])
  if (u.endsWith('/preflight')) return preflightBody
  if (/\/runs\/[^/]+\/drafts$/.test(u)) return draftsBody
  if (/\/runs\/[^/]+\/cancel$/.test(u)) return { run_id: RUN, status: 'cancelling' }
  if (u.endsWith('/question-coverage/runs')) return createdBody
  throw new Error(`unexpected POST ${u}`)
}

async function apiPostFormImpl(
  url: string,
  form: FormData,
): Promise<unknown> {
  calls.forms.push([String(url), form])
  return parsedBody
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = LANG
  vi.clearAllMocks()
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  datasources = [{ name: DS }]
  preflightBody = makePreflight()
  statusBody = makeStatus()
  reportBody = makeReport()
  diffBody = makeDiff()
  draftsBody = makeDrafts()
  parsedBody = makeParsed()
  createdBody = makeCreated()
  historyBody = {
    datasource: DS,
    runs: [
      runMeta(RUN, { previous_done_run_id: BASE }),
      runMeta(BASE, { created_at: '2026-10-17T10:00:00Z', semantic_digest: 'digest-previous' }),
    ],
    limit: 20,
    offset: 0,
    has_more: false,
  }
  calls.get.length = 0
  calls.post.length = 0
  calls.forms.length = 0
  ;(apiGet as unknown as ReturnType<typeof vi.fn>).mockImplementation(apiGetImpl)
  ;(apiPost as unknown as ReturnType<typeof vi.fn>).mockImplementation(apiPostImpl)
  ;(apiPostForm as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    apiPostFormImpl,
  )
})

/* ── mounting ─────────────────────────────────────────────────────────── */

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function settle(ms = 25) {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, ms))
  await flushPromises()
}

async function mountView(url = '/admin/question-coverage') {
  wrapper?.unmount()
  const routes: RouteRecordRaw[] = [
    { path: '/', name: 'chat', component: { render: () => null } },
    { path: '/admin', component: { render: () => null } },
    {
      path: '/admin/question-coverage',
      name: 'admin-question-coverage',
      component: { render: () => null },
    },
    { path: '/admin/semantic', name: 'admin-semantic', component: { render: () => null } },
    { path: '/admin/kb', component: { render: () => null } },
  ]
  router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(url)
  await router.isReady()
  wrapper = mount(QuestionCoverageView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await settle()
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

function bodyText(): string {
  return document.body.textContent ?? ''
}

function findButton(root: ParentNode, text: string): HTMLElement {
  const btn = Array.from(root.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  )
  if (!btn) throw new Error(`button not found: ${text}`)
  return btn as HTMLElement
}

function dialogConfirm(): HTMLElement {
  const btn = document.body.querySelector('.confirm-btn.is-solid')
  if (!btn) throw new Error('dialog confirm button not found')
  return btn as HTMLElement
}

/* ── 路由 / 导航 / 文案 ───────────────────────────────────────────────── */

describe('route, navigation and copy', () => {
  it('registers an admin-only route and a modeling-group nav item', () => {
    const item = NAV_ITEMS.find((i) => i.key === 'question-coverage')
    expect(item).toBeTruthy()
    expect(item!.path).toBe('/admin/question-coverage')
    expect(item!.group).toBe('modeling')
    // 未声明 visibleFor = 仅 admin（默认拒绝，与路由守卫同一条规则）。
    expect(item!.visibleFor).toBeUndefined()
    expect(navItemsFor('admin').some((i) => i.key === 'question-coverage')).toBe(true)
    expect(navItemsFor('analyst').some((i) => i.key === 'question-coverage')).toBe(false)

    const resolved = appRouter.resolve('/admin/question-coverage')
    expect(resolved.matched.length).toBeGreaterThan(0)
    expect(requiredRoles(resolved)).toEqual(['admin'])
  })

  it('ships the qc copy in both languages', () => {
    for (const key of [
      'qcTitle',
      'qcKpiDeterministic',
      'qcGapDraftable',
      'qcDiffSemanticChanged',
      'qcOutcomeSoftMiss',
      'qcDraftStatusStale',
    ] as const) {
      expect(messages.zh[key], key).toBeTruthy()
      expect(messages.en[key], key).toBeTruthy()
      expect(messages.en[key], key).not.toBe(messages.zh[key])
    }
  })
})

/* ── 粘贴 → 预检 → 确认成本 → 开跑 → 报告 ─────────────────────────────── */

describe('paste → preflight → start → report', () => {
  it('gates start on cost confirmation and renders the report it comes back with', async () => {
    const view = await mountView()

    await view.find('textarea').setValue('How many loans?\nTotal amount by region?')
    // 预检防抖 400ms。
    await settle(430)

    expect(bodyText()).toContain('Cost preflight')
    expect(bodyText()).toContain('deepseek-chat')
    expect(bodyText()).toContain('model_fast')
    expect(bodyText()).toContain('Hard cap')
    expect(bodyText()).toContain('does not execute SQL')

    const preflightCall = calls.post.find(([u]) => u.endsWith('/preflight'))
    expect(preflightCall).toBeTruthy()
    expect((preflightCall![1] as { questions: string[] }).questions).toEqual([
      'How many loans?',
      'Total amount by region?',
    ])

    // 未勾选成本 → 开始键禁用。
    expect(findButton(document.body, 'Start checkup').hasAttribute('disabled')).toBe(true)

    await view.find('input[type="checkbox"]').setValue(true)
    await settle()
    const start = findButton(document.body, 'Start checkup')
    expect(start.hasAttribute('disabled')).toBe(false)

    start.click()
    await settle()

    const create = calls.post.find(([u]) => u.endsWith('/question-coverage/runs'))
    expect(create).toBeTruthy()
    expect((create![1] as { confirm_cost: boolean }).confirm_cost).toBe(true)
    expect((create![1] as { questions: string[] }).questions.length).toBe(2)

    // 状态终态即拉报告 + diff + 历史。
    expect(calls.get.some((u) => u.endsWith(`/runs/${RUN}/status`))).toBe(true)
    expect(calls.get.some((u) => u.endsWith(`/runs/${RUN}`))).toBe(true)

    const text = bodyText()
    expect(text).toContain('Report')
    expect(text).toContain('Deterministic coverage')
    expect(text).toContain('100.0%')
    // 副标题 = N/M · 对比上一轮 Δ（Δ 来自后端 summary_delta，原样渲染）。
    expect(text).toContain('2/2 · vs previous run +50.0pp')
    // 分布与明细。
    expect(text).toContain('Fast-path hit')
    expect(text).toContain('How many loans?')
    expect(text).toContain('Total amount by region?')
    // 缺口汇总来自后端分组。
    expect(text).toContain('Gap summary')
    expect(text).toContain('Undeclared metric')
    expect(text).toContain('Draftable')
    // 两轮语义模型有变更 → 对比块的诚实提示。
    expect(text).toContain('Run comparison')
    expect(text).toContain('The semantic model changed between runs')

    // 打开第二行（完整编译）的明细抽屉：计划/锚定来自结果行。
    const detailButtons = Array.from(document.body.querySelectorAll('button')).filter(
      (b) => (b.textContent ?? '').trim() === 'Details',
    )
    expect(detailButtons.length).toBe(2)
    ;(detailButtons[1] as HTMLElement).click()
    await settle()
    expect(bodyText()).toContain('Anchored datasets')
    expect(bodyText()).toContain('account')
    expect(bodyText()).toContain('QueryPlan(metric=total_amount, dim=region)')
  })
})

/* ── CSV ──────────────────────────────────────────────────────────────── */

describe('csv input', () => {
  it('uploads multipart and renders the server parse preview', async () => {
    const view = await mountView()
    ;(view.vm as unknown as { mode: string }).mode = 'csv'
    await settle()

    const input = view.find('input[type="file"]')
    const file = new File(['question\nHow many loans?\n'], 'qs.csv', {
      type: 'text/csv',
    })
    Object.defineProperty(input.element, 'files', {
      value: [file],
      configurable: true,
    })
    await input.trigger('change')
    await settle()

    expect(calls.forms.length).toBe(1)
    expect(calls.forms[0][1].get('file')).toBe(file)

    const text = bodyText()
    expect(text).toContain('Parse preview')
    expect(text).toContain('Questions kept')
    expect(text).toContain('utf-8')
    expect(text).toContain('question')
    expect(text).toContain('Rows scanned')
  })
})

/* ── 草稿：只发 all/seqs，永远 pending ─────────────────────────────────── */

describe('drafting gaps', () => {
  it('asks for confirmation, posts {all:true} and renders per-asset results', async () => {
    await mountView(`/admin/question-coverage?ds=${DS}&run=${RUN}`)

    expect(bodyText()).toContain('Gap summary')
    findButton(document.body, 'Draft all draftable gaps').click()
    await settle()
    expect(bodyText()).toContain('Create pending drafts?')
    expect(bodyText()).toContain('only land as pending drafts')

    dialogConfirm().click()
    await settle()

    const draftCall = calls.post.find(([u]) => u.endsWith('/drafts'))
    expect(draftCall).toBeTruthy()
    expect(draftCall![1]).toEqual({ all: true })

    const text = bodyText()
    expect(text).toContain('Draft results')
    expect(text).toContain('Created · 1')
    expect(text).toContain('Already exists · 1')
    expect(text).toContain('metric:loan_amount')
    // 页面上不存在任何「确认草稿」动作 —— 本页只会生成 pending。
    expect(text).not.toContain('Confirm drafts')
  })
})

/* ── 取消 ─────────────────────────────────────────────────────────────── */

describe('cancel', () => {
  it('confirms, posts cancel and reflects the cooperative-request state', async () => {
    statusBody = makeStatus({
      status: 'running',
      processed_count: 1,
      finished_at: null,
      summary: undefined,
    })
    await mountView(`/admin/question-coverage?ds=${DS}&run=${RUN}`)

    expect(bodyText()).toContain('Running')
    expect(bodyText()).toContain('1 questions processed')
    findButton(document.body, 'Cancel checkup').click()
    await settle()
    expect(bodyText()).toContain('Cancel this checkup?')

    dialogConfirm().click()
    await settle()

    expect(calls.post.some(([u]) => u.endsWith('/cancel'))).toBe(true)
    expect(bodyText()).toContain('Cancellation requested')
  })
})

/* ── 中断 / 运行级失败 / 空样本 ───────────────────────────────────────── */

describe('interrupted, failed and no-sample', () => {
  it('interrupted keeps finished results and offers a rerun from stored questions', async () => {
    statusBody = makeStatus({ status: 'interrupted', finished_at: null })
    const view = await mountView(`/admin/question-coverage?ds=${DS}&run=${RUN}`)

    expect(bodyText()).toContain('This checkup was interrupted')
    findButton(document.body, 'Run again').click()
    await settle()

    const ta = view.find('textarea').element as HTMLTextAreaElement
    expect(ta.value).toContain('How many loans?')
    expect(ta.value).toContain('Total amount by region?')
  })

  it('run-level failure is loud, and null rates never render as 0%', async () => {
    statusBody = makeStatus({ status: 'error', error: 'boom: model unavailable' })
    await mountView(`/admin/question-coverage?ds=${DS}&run=${RUN}`)
    expect(bodyText()).toContain('Run failed')
    expect(bodyText()).toContain('boom: model unavailable')

    wrapper?.unmount()
    statusBody = makeStatus()
    reportBody = makeReport({
      summary: makeSummary({
        counts: countMap(),
        evaluated: 0,
        question_total: 2,
        metrics: EMPTY_METRICS,
      }),
      gap_groups: [],
    })
    await mountView(`/admin/question-coverage?ds=${DS}&run=${RUN}`)
    const text = bodyText()
    expect(text).toContain('No sample · not computable')
    expect(text).toContain('—')
    // 空缺口不是错误：如实说没有编译级缺口。
    expect(text).toContain('No compile-level gaps detected.')
  })

  it('preflight blockers disable start and deep-link to the fix', async () => {
    preflightBody = makePreflight({
      blockers: [
        {
          code: 'no_semantic_model',
          message: 'This datasource has no semantic model yet.',
          href: '/admin/kb?ds=demo',
        },
      ],
    })
    const view = await mountView()
    await view.find('textarea').setValue('How many loans?')
    await settle(430)

    expect(bodyText()).toContain('This datasource has no semantic model yet.')
    expect(bodyText()).toContain('Initialize the knowledge base')

    await view.find('input[type="checkbox"]').setValue(true)
    await settle()
    expect(findButton(document.body, 'Start checkup').hasAttribute('disabled')).toBe(true)
  })

  it('empty datasource list keeps the preflight section in an honest empty state', async () => {
    datasources = []
    await mountView()
    expect(bodyText()).toContain(
      'Add questions and pick a datasource to preflight automatically.',
    )
    expect(() => findButton(document.body, 'Start checkup')).toThrow()
  })
})
