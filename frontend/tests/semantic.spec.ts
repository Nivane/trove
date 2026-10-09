/**
 * SemanticView — the semantic workbench (P2).
 *
 * Exercised end-to-end against a mock of the *server* contract the page
 * really consumes (GET /v1/admin/semantic/{ds}, /history, /admin/audit,
 * POST /validate · /preview · /drafts · /drafts/batch · /drift/check) plus
 * a real router, so the assertions pin the page's own behaviour:
 *
 *  · KPIs as entry points, state in the URL (tab/q/sort/order/prob read
 *    back; ordering is two keys per §4.3 U2, never sort=key:dir);
 *  · the five states, including the one that matters most: a *skipped*
 *    drift check renders as "did not complete", never as "no drift";
 *  · problems are structured rows with locate / blast-radius / fix;
 *  · the editor validates before it creates, the dry run renders its SQL
 *    and rows, and apply is gated on a passing validation;
 *  · batch approval is one request with per-item failures and retry.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import SemanticView from '../src/views/admin/SemanticView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
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

import { apiGet, apiPost, ApiError } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'

const DESTS = [
  { name: 'demo', type: 'sqlite', default: true, status: 'connected' },
  { name: 'cold', type: 'mysql', status: 'disconnected' },
]

const METRICS = [
  {
    name: 'avg_salary',
    expression: 'AVG(account.salary)',
    synonyms: [],
    datasets: ['account'],
    definition: '',
  },
  {
    name: 'total_loan_amount',
    expression: 'SUM(loan.amount)',
    synonyms: ['loan volume'],
    datasets: ['loan'],
    definition: 'Sum of all loan amounts',
  },
]

const DATASETS = [
  {
    name: 'loan',
    source: 'loan',
    primary_key: ['loan_id'],
    description: 'One row per loan',
    fields: [
      { name: 'amount', expression: 'amount', datatype: 'decimal', semantic_role: 'measure' },
      {
        name: 'status',
        expression: 'status',
        datatype: 'varchar',
        semantic_role: 'enum',
        enum_display: { A: 'Active', B: 'Closed' },
        value_aliases: { A: ['open'] },
      },
    ],
  },
  {
    name: 'account',
    source: 'account',
    primary_key: ['account_id'],
    description: '',
    fields: [{ name: 'salary', expression: 'salary', datatype: 'decimal', semantic_role: 'measure' }],
  },
]

const ISSUE_ITEMS = [
  {
    severity: 'error',
    code: 'expr_parse',
    target: { kind: 'metric', name: 'total_loan_amount' },
    message: '指标「total_loan_amount」表达式无法解析',
    hint: '检查括号/函数名/方言',
  },
  {
    severity: 'warning',
    code: 'lint',
    target: { kind: 'unknown', name: '' },
    message: 'unclassified lint note',
    hint: '见原文',
  },
]

const DRIFT_ITEMS = [
  {
    level: 'L2',
    severity: 'critical',
    subject: 'loan',
    detail: { dataset: 'loan', note: 'physical table missing' },
    first_seen_at: '2026-09-01T08:00:00Z',
    seen_count: 3,
    drift_id: 7,
    impact: {
      metrics: ['total_loan_amount'],
      examples: ['Which month had the most loans?'],
      rules: [],
      lessons: ['loan rate is settlement-only'],
    },
  },
]

const PENDING_DRAFTS = [
  {
    id: 'd1',
    kind: 'metric',
    action: 'upsert',
    name: 'avg_salary',
    note: 'add avg salary',
    created_at: '2026-09-28T10:00:00Z',
    payload: { expression: 'AVG(account.salary)' },
    diff: {
      kind: 'metric',
      name: 'avg_salary',
      action: 'upsert',
      before: null,
      fields: [
        { f: 'expression', before: '', after: 'AVG(account.salary)', changed: true },
        { f: 'datasets', before: '', after: 'account', changed: true },
      ],
    },
  },
  {
    id: 'd2',
    kind: 'field',
    action: 'upsert',
    name: 'loan.rate',
    created_at: '2026-09-29T10:00:00Z',
    payload: { expression: 'rate' },
    diff: {
      kind: 'field',
      name: 'loan.rate',
      action: 'upsert',
      before: null,
      fields: [{ f: 'expression', before: '', after: 'rate', changed: true }],
    },
  },
]

const HISTORY = [
  { sha: 'abc1234def', author: 'admin', date: '2026-09-30T09:00:00Z', subject: 'kb: add metric' },
]

const AUDIT = [
  {
    id: 1,
    ts: '2026-09-30T09:10:00Z',
    username: 'admin',
    action: 'semantic.draft.confirm',
    status: 200,
    details: { datasource: 'demo', name: 'total_loan_amount' },
  },
  {
    id: 2,
    ts: '2026-09-30T09:11:00Z',
    username: 'other',
    action: 'semantic.draft.confirm',
    status: 200,
    details: { datasource: 'sales', name: 'avg_salary' },
  },
]

let DETAIL: Record<string, unknown>
let FAIL_DETAIL: ApiError | null = null
let FAIL_CHANGE_DETAIL: ApiError | null = null
let DRIFT_CHECK_ERROR: ApiError | null = null
let VALIDATE: (body: Record<string, any>) => Record<string, unknown>
let PREVIEW: (body: Record<string, any>) => Record<string, unknown>
let BATCH: (body: Record<string, any>) => Record<string, unknown>
/** 深链首绘测试用:变更列表一条 + 详情一条(评审端点形状)。 */
const CHANGE_DETAIL = {
  id: 'c1',
  datasource: 'demo',
  origin: 'manual',
  status: 'open',
  question: '为什么退款率上升?',
  note: 'refund_rate 口径调整',
  author: 'bob',
  created_at: '2026-10-05T09:00:00Z',
  subjects: [{ kind: 'metric', name: 'refund_rate' }],
  impact: { metrics: [], examples: [], rules: [], lessons: [] },
  verification: null,
}

const OK_VALIDATE = {
  ok: true,
  errors: [],
  warnings: [],
  normalized: { expression: 'SUM(loan.amount)' },
}

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

function resetFixtures() {
  DETAIL = {
    enabled: true,
    model: {
      name: 'demo_model',
      metrics: METRICS,
      datasets: DATASETS,
      relationships: [],
    },
    issues: ISSUE_ITEMS.map((i) => i.message),
    issue_items: ISSUE_ITEMS,
    drafts: {
      pending: PENDING_DRAFTS,
      applied: [
        {
          id: 'a1',
          kind: 'metric',
          action: 'upsert',
          name: 'old_metric',
          status: 'applied',
          created_at: '2026-09-01T00:00:00Z',
        },
      ],
      rejected: [],
    },
    drift: {
      status: 'ok',
      checked_at: '2026-10-01T10:00:00Z',
      items: DRIFT_ITEMS,
    },
  }
  FAIL_DETAIL = null
  FAIL_CHANGE_DETAIL = null
  DRIFT_CHECK_ERROR = null
  VALIDATE = () => OK_VALIDATE
  PREVIEW = () => ({
    sql: 'SELECT AVG(account.salary) AS avg_salary',
    columns: ['avg_salary'],
    rows: [[85.2]],
    row_count: 1,
  })
  BATCH = (body) => ({
    results: body.ids.map((id: string) => ({ id, ok: true })),
    applied: body.ids.length,
    failed: 0,
  })
}

function mockApi() {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path === '/v1/admin/datasources') return { datasources: clone(DESTS) }
    if (/^\/v1\/admin\/semantic\/[^/]+$/.test(path)) {
      if (FAIL_DETAIL) throw FAIL_DETAIL
      return { semantic: clone(DETAIL) }
    }
    if (path.includes('/history')) return { history: clone(HISTORY) }
    if (/\/changes\/[^/]+$/.test(path)) {
      if (FAIL_CHANGE_DETAIL) throw FAIL_CHANGE_DETAIL
      return { change: clone(CHANGE_DETAIL) }
    }
    if (/\/changes$/.test(path)) return { changes: [clone(CHANGE_DETAIL)] }
    if (path.startsWith('/v1/admin/audit')) return clone({ audit: AUDIT, total: AUDIT.length })
    return {}
  })

  ;(apiPost as any).mockImplementation(async (path: string, body: Record<string, any> = {}) => {
    if (path.endsWith('/validate')) return clone(VALIDATE(body))
    if (path.endsWith('/preview')) return clone(PREVIEW(body))
    if (path.endsWith('/drafts/batch')) return clone(BATCH(body))
    if (/\/drafts\/[^/]+\/(confirm|reject)$/.test(path)) return { draft: {} }
    if (path.endsWith('/drafts')) return { draft: { id: 'new' } }
    if (path.startsWith('/v1/admin/drift/check')) {
      if (DRIFT_CHECK_ERROR) throw DRIFT_CHECK_ERROR
      return { drift: { status: 'ok', items: [] } }
    }
    return {}
  })
}

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountView(url = '/admin/semantic?ds=demo') {
  wrapper?.unmount()
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/kb', name: 'admin-kb', component: { render: () => null } },
      { path: '/admin/semantic', component: { render: () => null } },
    ],
  })
  await router.push(url)
  await router.isReady()
  wrapper = mount(SemanticView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

async function settle(ms = 60) {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, ms))
  await flushPromises()
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

function findButton(root: ParentNode, text: string): HTMLButtonElement {
  const el = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) =>
    (b.textContent ?? '').trim().includes(text),
  )
  if (!el) throw new Error(`button not found: ${text}`)
  return el
}

function kpiTile(view: VueWrapper, label: string) {
  const tile = view.findAll('.kpi-tile').find((el) => el.text().includes(label))
  if (!tile) throw new Error(`KPI tile not found: ${label}`)
  return tile
}

function tabBtn(view: VueWrapper, label: string) {
  const tab = view.findAll('.sem-tab').find((el) => el.text().includes(label))
  if (!tab) throw new Error(`tab not found: ${label}`)
  return tab
}

function rowByText(view: VueWrapper, text: string) {
  const row = view.findAll('.dt-row').find((el) => el.text().includes(text))
  if (!row) throw new Error(`row not found: ${text}`)
  return row
}

function drawer(): HTMLElement {
  const panel = document.body.querySelector<HTMLElement>('.drawer-panel')
  if (!panel) throw new Error('drawer not open')
  return panel
}

function confirmDialog(): HTMLElement {
  const panel = document.body.querySelector<HTMLElement>('.confirm-panel')
  if (!panel) throw new Error('confirm dialog not open')
  return panel
}

function dialogInput(): HTMLInputElement {
  const inputs = Array.from(
    document.body.querySelectorAll<HTMLInputElement>('.el-dialog input'),
  ).filter((i) => !i.closest('.el-select'))
  if (!inputs.length) throw new Error('dialog input not found')
  return inputs[0]
}

function dialogTextarea(): HTMLTextAreaElement {
  const ta = document.body.querySelector<HTMLTextAreaElement>('.el-dialog textarea')
  if (!ta) throw new Error('dialog textarea not found')
  return ta
}

async function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

function calls(path: string): Record<string, unknown>[] {
  return (apiPost as any).mock.calls
    .filter((c: unknown[]) => c[0] === path)
    .map((c: unknown[]) => (c[1] ?? {}) as Record<string, unknown>)
}

beforeEach(() => {
  resetFixtures()
  pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = 'en'
  ;(apiPost as any).mockResolvedValue({})
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('SemanticView', () => {
  it('renders KPIs, tabs and problems from the detail contract', async () => {
    mockApi()
    const view = await mountView()

    const paths = (apiGet as any).mock.calls.map((c: unknown[]) => String(c[0]))
    expect(paths).toContain('/v1/admin/datasources')
    expect(paths).toContain('/v1/admin/semantic/demo')

    // KPIs are real counts; problems decompose into lint + drift
    expect(kpiTile(view, 'Metrics').text()).toContain('2')
    expect(kpiTile(view, 'Datasets').text()).toContain('2')
    expect(kpiTile(view, 'Fields').text()).toContain('3')
    expect(kpiTile(view, 'Pending').text()).toContain('2')
    const problems = kpiTile(view, 'Open problems')
    expect(problems.text()).toContain('3')
    expect(problems.text()).toContain('3 = 2 lint + 1 drift')

    // tabs carry the same counts
    const count = (label: string) => tabBtn(view, label).find('.tab-badge').text()
    expect(count('Metrics')).toBe('2')
    expect(count('Datasets')).toBe('2')
    expect(count('Fields')).toBe('3')
    expect(count('Pending')).toBe('2')

    // context strip: last commit + drift checked time
    const text = bodyText()
    expect(text).toContain('Last commit')
    expect(text).toContain('Drift checked')

    // problems panel: structured issue rows + drift with lifecycle
    expect(text).toContain('Validation issues (2)')
    expect(text).toContain('指标「total_loan_amount」表达式无法解析')
    expect(text).toContain('检查括号/函数名/方言')
    expect(text).toContain('Drift (1)')
    expect(text).toContain('physical table missing')
    expect(text).toContain('Seen 3×')

    // rows render
    expect(rowByText(view, 'total_loan_amount')).toBeTruthy()
  })

  it('KPI problems tile is an entry point: filters to errors, toggles back off', async () => {
    mockApi()
    const view = await mountView()

    await kpiTile(view, 'Open problems').trigger('click')
    await settle()
    expect(router.currentRoute.value.query.prob).toBe('error')
    expect(bodyText()).toContain('Validation issues (1)')
    expect(bodyText()).not.toContain('unclassified lint note')
    // critical drift survives the error filter
    expect(bodyText()).toContain('physical table missing')

    await kpiTile(view, 'Open problems').trigger('click')
    await settle()
    expect(router.currentRoute.value.query.prob).toBeUndefined()
    expect(bodyText()).toContain('unclassified lint note')
  })

  it('reads tab, search and sort back from the URL', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=fields&q=salary')

    expect(tabBtn(view, 'Fields').classes()).toContain('is-active')
    const search = view.find('.sem-search input')
    expect((search.element as HTMLInputElement).value).toBe('salary')
    expect(rowByText(view, 'salary')).toBeTruthy()
    expect(view.findAll('.dt-row')).toHaveLength(1)
    expect(view.find('.view-count').text()).toContain('1 items')
  })

  it('sorting reorders rows and is written to the URL', async () => {
    mockApi()
    const view = await mountView()

    // default: name asc
    expect(view.findAll('.dt-row')[0].text()).toContain('avg_salary')

    const nameHeader = view
      .findAll('.dt-sort')
      .find((b) => b.text().includes('Name'))!
    await nameHeader.trigger('click')
    await settle()
    // §4.3 U2: sort and order are two keys, never the packed sort=name:asc
    expect(router.currentRoute.value.query.sort).toBe('name')
    expect(router.currentRoute.value.query.order).toBe('asc')

    await nameHeader.trigger('click')
    await settle()
    expect(router.currentRoute.value.query.sort).toBe('name')
    expect(router.currentRoute.value.query.order).toBe('desc')
    expect(view.findAll('.dt-row')[0].text()).toContain('total_loan_amount')

    // last-changed sort is audit-derived: the confirmed asset floats up, "—" sinks
    const changedHeader = view
      .findAll('.dt-sort')
      .find((b) => b.text().includes('Last changed'))!
    await changedHeader.trigger('click')
    await settle()
    expect(router.currentRoute.value.query.sort).toBe('changed')
    expect(router.currentRoute.value.query.order).toBe('desc')
    const first = view.findAll('.dt-row')[0]
    expect(first.text()).toContain('total_loan_amount')
    expect(first.text()).not.toContain('—')
  })

  it('asset drawer: definition, expression, anchors, references, history + audit', async () => {
    mockApi()
    const view = await mountView()

    await rowByText(view, 'total_loan_amount').trigger('click')
    await settle()

    const panel = drawer()
    const text = panel.textContent ?? ''
    expect(text).toContain('total_loan_amount')
    expect(text).toContain('Sum of all loan amounts')
    expect(text).toContain('SUM(loan.amount)')
    expect(text).toContain('Anchored datasets')
    expect(text).toContain('loan')
    // "referenced by" is composed from the drift blast radius snapshot
    expect(text).toContain('Referenced by')
    // history + the audit trail for this asset only (sales' entry is excluded)
    expect(text).toContain('kb: add metric')
    expect(text).toContain('abc1234')
    expect(text).toContain('semantic.draft.confirm')
  })

  it('field drawer renders the value dictionary (enum display + aliases)', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=fields')

    await rowByText(view, 'status').trigger('click')
    await settle()

    const text = drawer().textContent ?? ''
    expect(text).toContain('Value dictionary')
    expect(text).toContain('Enum display')
    expect(text).toContain('Active')
    expect(text).toContain('Value aliases')
    expect(text).toContain('open')
  })

  it('validates before creating: field errors land on the field, refusal blocks the draft', async () => {
    mockApi()
    VALIDATE = () => ({
      ok: false,
      errors: [
        {
          severity: 'error',
          code: 'dup_metric',
          target: { kind: 'metric', name: 'x_metric' },
          message: '指标「x_metric」重复定义',
          hint: '同名指标只保留一条',
        },
      ],
      warnings: [],
      normalized: { expression: '' },
    })
    const view = await mountView()

    findButton(view.element, 'New asset').click()
    await flushPromises()
    findButton(document.body, 'Add metric').click()
    await flushPromises()

    await type(dialogInput(), 'x_metric')
    await type(dialogTextarea(), 'SUM(loan.amount)')
    findButton(document.body, 'Create draft').click()
    // el-form-item debounces its error slot by 100ms
    await settle(200)

    const err = document.body.querySelector('.el-form-item__error')
    expect(err?.textContent).toContain('指标「x_metric」重复定义')
    expect(calls('/v1/admin/semantic/demo/drafts')).toHaveLength(0)

    // a passing validation lets the same dialog create the draft
    VALIDATE = () => OK_VALIDATE
    findButton(document.body, 'Create draft').click()
    await settle()
    const posts = calls('/v1/admin/semantic/demo/drafts')
    expect(posts).toHaveLength(1)
    expect(posts[0]).toMatchObject({ kind: 'metric', action: 'upsert', name: 'x_metric' })
    expect((posts[0].payload as Record<string, unknown>).expression).toBe('SUM(loan.amount)')
  })

  it('editor: dry run renders SQL + rows, and Create draft waits for a passing validate', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=fields')

    await rowByText(view, 'amount').trigger('click')
    await settle()
    findButton(drawer(), 'Edit').click()
    await flushPromises()

    const draftBtn = findButton(drawer(), 'Create draft')
    expect(draftBtn.disabled).toBe(true)

    findButton(drawer(), 'Dry run').click()
    await settle()

    const previewCalls = calls('/v1/admin/semantic/demo/preview')
    expect(previewCalls).toHaveLength(1)
    expect(previewCalls[0]).toMatchObject({ kind: 'field', name: 'loan.amount' })
    expect(drawer().textContent).toContain('SELECT AVG(account.salary) AS avg_salary')
    expect(drawer().textContent).toContain('1 rows')
    expect(drawer().textContent).toContain('85.2')

    findButton(drawer(), 'Validate').click()
    await settle()
    expect(drawer().textContent).toContain('Validation passed')
    expect(findButton(drawer(), 'Create draft').disabled).toBe(false)
  })

  it('draft drawer shows the server diff and gates apply on validation', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=pending')

    await rowByText(view, 'avg_salary').trigger('click')
    await settle()

    const panel = drawer()
    const text = panel.textContent ?? ''
    expect(text).toContain('Draft · avg_salary')
    expect(text).toContain('Before')
    expect(text).toContain('After')
    expect(text).toContain('AVG(account.salary)')

    // validate-on-open ran against the draft's own payload
    const validated = calls('/v1/admin/semantic/demo/validate')
    expect(validated).toHaveLength(1)
    expect(validated[0]).toMatchObject({ kind: 'metric', name: 'avg_salary' })

    expect(text).toContain('Validation passed')
    const confirmBtn = findButton(panel, 'Confirm')
    expect(confirmBtn.disabled).toBe(false)
    confirmBtn.click()
    await settle()
    expect(calls('/v1/admin/semantic/demo/drafts/d1/confirm')).toHaveLength(1)

    // a failing validation keeps Confirm disabled and says why
    VALIDATE = () => ({
      ok: false,
      errors: [
        {
          severity: 'error',
          code: 'expr_parse',
          target: { kind: 'field', name: 'loan.rate' },
          message: '表达式无法解析',
          hint: '',
        },
      ],
      warnings: [],
      normalized: { expression: '' },
    })
    await rowByText(view, 'loan.rate').trigger('click')
    await settle()
    expect(drawer().textContent).toContain('Validation failed')
    expect(drawer().textContent).toContain('表达式无法解析')
    expect(findButton(drawer(), 'Confirm').disabled).toBe(true)
  })

  it('batch apply is one request; per-item failures are listed and retried alone', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=pending')

    const boxes = document.body.querySelectorAll<HTMLButtonElement>(
      '[aria-label="Select d1"], [aria-label="Select d2"]',
    )
    expect(boxes).toHaveLength(2)
    // one click per tick: the table re-emits selection from its own props
    boxes[0].click()
    await flushPromises()
    boxes[1].click()
    await flushPromises()
    expect(bodyText()).toContain('2 selected')

    findButton(view.element, 'Apply selected').click()
    await settle()
    const confirmPanel = confirmDialog()
    expect(confirmPanel.textContent).toContain('Apply 2 drafts')
    expect(confirmPanel.textContent).toContain('avg_salary')

    // first attempt: d2 refuses the gate
    BATCH = (body) =>
      body.ids.length > 1
        ? {
            results: [
              { id: 'd1', ok: true },
              { id: 'd2', ok: false, error: 'certification gate refused' },
            ],
            applied: 1,
            failed: 1,
          }
        : { results: body.ids.map((id: string) => ({ id, ok: true })), applied: 1, failed: 0 }
    findButton(confirmPanel, 'Apply selected').click()
    await settle()

    expect(bodyText()).toContain('Partial failures')
    expect(bodyText()).toContain('loan.rate')
    expect(bodyText()).toContain('certification gate refused')

    const retry = findButton(document.body.querySelector('.partial-panel')!, 'Retry')
    retry.click()
    await settle()

    const batchCalls = calls('/v1/admin/semantic/demo/drafts/batch')
    expect(batchCalls).toHaveLength(2)
    expect(batchCalls[1]).toMatchObject({ ids: ['d2'], action: 'confirm' })
    expect(bodyText()).not.toContain('Partial failures')
    expect(bodyText()).toContain('2 applied')
  })

  it('?draft=<id> is a deep link straight into the draft drawer', async () => {
    mockApi()
    await mountView('/admin/semantic?ds=demo&tab=pending&draft=d1')
    await settle()
    const text = drawer().textContent ?? ''
    expect(text).toContain('Draft · avg_salary')
    expect(text).toContain('AVG(account.salary)')
  })

  it('a skipped drift check reads as "did not complete", never as clean', async () => {
    mockApi()
    DETAIL.drift = { status: 'skipped', skip_reason: 'catalog unavailable', items: [] }
    const view = await mountView()

    expect(bodyText()).toContain('Drift check did not complete')
    expect(bodyText()).toContain('catalog unavailable')
    expect(bodyText()).not.toContain('No drift detected')

    // a 503 on re-check keeps the state "not checked" and says why
    DRIFT_CHECK_ERROR = new ApiError(503, 'drift check did not complete')
    findButton(view.element, 'Re-run drift check').click()
    await settle()
    expect(bodyText()).toContain('Drift check failed')
    expect(bodyText()).toContain('never washed into "clean"')
    expect(bodyText()).toContain('Drift check did not complete')
    expect(bodyText()).not.toContain('No drift detected')
  })

  it('expands the drift blast radius and offers a fix target', async () => {
    mockApi()
    const view = await mountView()

    const impactBtn = findButton(view.element.querySelector('.sem-drift-row')!, 'Blast radius')
    impactBtn.click()
    await settle()
    const row = view.element.querySelector('.sem-drift-row')!
    expect((row.textContent ?? '')).toContain('Metrics (1)')
    expect((row.textContent ?? '')).toContain('total_loan_amount')

    // [Fix model] opens the drifted asset's drawer
    findButton(row as HTMLElement, 'Fix model').click()
    await settle()
    const panel = drawer()
    expect(panel.textContent).toContain('loan')

    // "who uses it" carries the drift snapshot's four groups: asset names stay
    // clickable, example/rule/lesson texts render as plain (non-dead) chips
    expect(panel.textContent).toContain('Referenced by')
    const refs = panel.querySelector('.sem-usedby')!
    expect(refs.textContent).toContain('Metrics')
    expect(refs.textContent).toContain('total_loan_amount')
    expect(refs.textContent).toContain('Examples')
    expect(refs.textContent).toContain('Which month had the most loans?')
    expect(refs.textContent).toContain('Lessons')
    expect(refs.textContent).toContain('loan rate is settlement-only')
    expect(refs.querySelector('.sem-ref.is-static')).toBeTruthy()
  })

  it('shows the KB-init CTA when the datasource has no semantic model', async () => {
    mockApi()
    DETAIL.enabled = false
    DETAIL.model = null
    const view = await mountView()

    expect(bodyText()).toContain('This datasource has no semantic model yet')
    findButton(view.element.querySelector('.state-panel')!, 'Initialize in Knowledge base').click()
    await settle()
    expect(router.currentRoute.value.path).toBe('/admin/kb')
    expect(router.currentRoute.value.query.ds).toBe('demo')
  })

  it('a failed load clears the page; retry reloads it', async () => {
    mockApi()
    FAIL_DETAIL = new ApiError(500, 'semantic exploded')
    const view = await mountView()

    expect(bodyText()).toContain('Failed to load the semantic model')
    expect(bodyText()).toContain('semantic exploded')
    expect(view.findAll('.dt-row')).toHaveLength(0)

    FAIL_DETAIL = null
    findButton(view.element.querySelector('.state-panel')!, 'Retry').click()
    await settle()
    expect(view.findAll('.dt-row').length).toBeGreaterThan(0)
    expect(bodyText()).not.toContain('Failed to load the semantic model')
  })

  // ── A2: blueprint deep links + conflicted drafts ──────────────
  // 冲突草稿不能确认(服务端守卫 + 按钮前置禁用),出路是「以此为蓝本
  // 新建」:抽屉按钮走 payload,拒绝卡深链走 bp_* 查询参数 —— 两条路汇进
  // 同一个预填器。参数只活一次导航(消费即从 URL 清掉)。

  it('bp_* deep link opens the prefilled dialog and drops the params from the URL', async () => {
    mockApi()
    await mountView(
      '/admin/semantic?ds=demo&tab=pending&bp_draft_kind=metric&bp_draft_name=avg_amount' +
        '&bp_expression=AVG(loan.amount)&bp_datasets=loan&bp_conflict_message=' +
        encodeURIComponent('指标「avg_amount」已声明'),
    )
    await settle()

    // prefilled dialog: name + expression + the conflict diagnostic
    expect(dialogInput().value).toBe('avg_amount')
    expect(dialogTextarea().value).toBe('AVG(loan.amount)')
    expect(bodyText()).toContain('Prefilled from the draft')
    expect(bodyText()).toContain('指标「avg_amount」已声明')

    // consumed exactly once: bp_* gone from the URL, unrelated keys survive
    const q = router.currentRoute.value.query
    expect(q.bp_draft_kind).toBeUndefined()
    expect(q.bp_expression).toBeUndefined()
    expect(q.ds).toBe('demo')
    expect(q.tab).toBe('pending')

    // and the prefill reaches the payload actually drafted
    findButton(document.body, 'Create draft').click()
    await settle()
    const posts = calls('/v1/admin/semantic/demo/drafts')
    expect(posts).toHaveLength(1)
    expect(posts[0]).toMatchObject({ kind: 'metric', name: 'avg_amount' })
    expect(posts[0].payload).toMatchObject({
      expression: 'AVG(loan.amount)',
      datasets: ['loan'],
    })
  })

  it('a field blueprint splits dataset.field and drafts under that dataset', async () => {
    mockApi()
    await mountView(
      '/admin/semantic?ds=demo&bp_draft_kind=field&bp_draft_name=loan.grade&bp_expression=grade',
    )
    await settle()
    expect(dialogInput().value).toBe('grade')
    findButton(document.body, 'Create draft').click()
    await settle()
    const posts = calls('/v1/admin/semantic/demo/drafts')
    expect(posts).toHaveLength(1)
    expect(posts[0]).toMatchObject({ kind: 'field', name: 'loan.grade' })
    expect((posts[0].payload as Record<string, unknown>).expression).toBe('grade')
  })

  it('a dotless field blueprint never guesses a dataset — the guard blocks instead', async () => {
    mockApi()
    await mountView('/admin/semantic?ds=demo&bp_draft_kind=field&bp_draft_name=grade')
    await settle()
    expect(dialogInput().value).toBe('grade')
    findButton(document.body, 'Create draft').click()
    await settle()
    expect(calls('/v1/admin/semantic/demo/drafts')).toHaveLength(0)
  })

  it('bp_draft_id deep link opens the draft drawer (the confirm exit)', async () => {
    mockApi()
    await mountView(
      '/admin/semantic?ds=demo&tab=pending&bp_draft_kind=metric&bp_draft_name=avg_salary&bp_draft_id=d1',
    )
    await settle()
    expect(drawer().textContent).toContain('Draft · avg_salary')
    expect(router.currentRoute.value.query.bp_draft_id).toBeUndefined()
    expect(router.currentRoute.value.query.draft).toBe('d1')
  })

  it('a conflicted draft: pill, banner, confirm pre-disabled, blueprint button prefills', async () => {
    mockApi()
    DETAIL.drafts.pending.push({
      id: 'd3',
      kind: 'metric',
      action: 'upsert',
      name: 'avg_loan_amount',
      note: 'refuse-conflict:name_declared:how much is an average loan?',
      created_at: '2026-10-05T08:00:00Z',
      payload: { expression: 'AVG(loan.amount)', datasets: ['loan'] },
      conflict: { code: 'name_declared', message: '指标「avg_loan_amount」已声明' },
    })
    const view = await mountView('/admin/semantic?ds=demo&tab=pending')
    await settle()

    const row = rowByText(view, 'avg_loan_amount')
    expect(row.text()).toContain('Conflict')

    await row.trigger('click')
    await settle()
    const panel = drawer()
    expect(panel.textContent).toContain('Conflict')
    expect(panel.textContent).toContain('指标「avg_loan_amount」已声明')
    // the draft validates fine — the disabled Confirm is purely the
    // conflict guard (server refuses it too), not a validation failure
    expect(panel.textContent).toContain('Validation passed')
    expect(findButton(panel, 'Confirm').disabled).toBe(true)

    findButton(panel, 'Create from this draft').click()
    await settle()
    expect(dialogInput().value).toBe('avg_loan_amount')
    expect(dialogTextarea().value).toBe('AVG(loan.amount)')
    expect(bodyText()).toContain('Prefilled from the draft')
  })

  it('a non-conflicted draft keeps its plain confirm (no pill, button enabled)', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=pending')
    await settle()

    expect(rowByText(view, 'avg_salary').text()).not.toContain('Conflict')
    await rowByText(view, 'avg_salary').trigger('click')
    await settle()
    const panel = drawer()
    expect(panel.querySelector('.sem-bad-draft')).toBeNull()
    expect(findButton(panel, 'Confirm').disabled).toBe(false)
    expect(panel.textContent).not.toContain('Create from this draft')
  })

  it('filtered-empty names the state and clears filters in one click', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&q=zzz-no-match')

    expect(bodyText()).toContain('No entries match the current filters')
    findButton(view.element, 'Clear filters').click()
    await settle()
    expect(router.currentRoute.value.query.q).toBeUndefined()
    expect(view.findAll('.dt-row').length).toBeGreaterThan(0)
  })

  it('a failed change-detail GET shows an error line, not a permanent Loading', async () => {
    // 只 toast 会把抽屉永远留在 Loading —— 而「空态」与「取失败」同形。
    mockApi()
    FAIL_CHANGE_DETAIL = new ApiError(500, 'change detail exploded')
    await mountView('/admin/semantic?ds=demo&tab=changes&change=c1')
    await settle()

    const panel = drawer()
    expect(panel.textContent).toContain('Could not load the change detail')
    expect(panel.textContent).toContain('change detail exploded')
    expect(panel.textContent).not.toContain('Loading…')
  })

  it('?change=<id> deep link fetches the change detail on first paint', async () => {
    // Regression: the composable hydrates `change` from the URL before the
    // view's own watcher is set up, so a non-immediate watcher missed the
    // first transition — refresh / shared link opened the drawer empty.
    mockApi()
    await mountView('/admin/semantic?ds=demo&tab=changes&change=c1')
    await settle()

    const paths = (apiGet as any).mock.calls.map((c: unknown[]) => String(c[0]))
    expect(paths).toContain('/v1/admin/semantic/demo/changes/c1')
    // 抽屉离开空载态:正文渲染出详情内容
    const panel = drawer()
    expect(panel.textContent).toContain('为什么退款率上升?')
    expect(panel.textContent).toContain('refund_rate 口径调整')
  })
})
