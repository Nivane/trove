/**
 * SemanticView — the semantic workbench (P2).
 *
 * Exercised end-to-end against a mock of the *server* contract the page
 * really consumes (GET /v1/admin/semantic/{ds}, /history, /admin/audit,
 * POST /validate · /preview · /drafts · /drafts/batch · /drift/check) plus
 * a real router, so the assertions pin the page's own behaviour:
 *
 *  · KPIs as entry points, state in the URL (tab/q/sort/prob read back);
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
let DRIFT_CHECK_ERROR: ApiError | null = null
let VALIDATE: (body: Record<string, any>) => Record<string, unknown>
let PREVIEW: (body: Record<string, any>) => Record<string, unknown>
let BATCH: (body: Record<string, any>) => Record<string, unknown>

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
    expect(router.currentRoute.value.query.sort).toBe('name:asc')

    await nameHeader.trigger('click')
    await settle()
    expect(router.currentRoute.value.query.sort).toBe('name:desc')
    expect(view.findAll('.dt-row')[0].text()).toContain('total_loan_amount')

    // last-changed sort is audit-derived: the confirmed asset floats up, "—" sinks
    const changedHeader = view
      .findAll('.dt-sort')
      .find((b) => b.text().includes('Last changed'))!
    await changedHeader.trigger('click')
    await settle()
    expect(router.currentRoute.value.query.sort).toBe('changed:desc')
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

  it('filtered-empty names the state and clears filters in one click', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&q=zzz-no-match')

    expect(bodyText()).toContain('No entries match the current filters')
    findButton(view.element, 'Clear filters').click()
    await settle()
    expect(router.currentRoute.value.query.q).toBeUndefined()
    expect(view.findAll('.dt-row').length).toBeGreaterThan(0)
  })
})
