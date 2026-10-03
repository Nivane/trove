/**
 * KbView — the KB content-operations page (W3-K).
 *
 * Exercised end-to-end against a mock of the *server* contract the page
 * really consumes (/v1/admin/datasources/{ds}/kb, /v1/kb/entries,
 * /v1/kb/examples/pending, per-item confirm/reject) plus a real router, so
 * what the assertions pin is the page's own behaviour: tabs and KPIs as
 * entry points, URL-backed filters, per-item decisions with audit receipts,
 * bulk as N single actions with partial-failure retry, asset adoption
 * health with dispositions, and the cleared-content failure state.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import KbView from '../src/views/admin/KbView.vue'

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

import { apiDelete, apiGet, apiPost, ApiError } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'

interface Lesson {
  pattern?: string
  question?: string
  note?: string
  sql_snippet?: string
  confirmed?: boolean
  upvotes?: number
  downvotes?: number
  confidence?: number
  source?: string
  evidence?: string
  created_at?: string
}

interface PendingExample {
  question: string
  sql: string
  tags?: string[]
  created_at?: string
}

let DESTS: { name: string; type: string; default?: boolean; status: string }[] = []
let LESSONS: Lesson[] = []
let PENDING: PendingExample[] = []
let DETAIL: {
  status: Record<string, unknown>
  terms: unknown[]
  examples: Record<string, unknown>[]
  rules: string[]
  lessons: Lesson[]
} | null = null

/** Set to make the matching confirm-one key fail with 409 (gate refusal). */
let confirmGate: Record<string, string> = {}
/** Set to make the datasource's kb detail endpoint fail. */
let failDs: string | null = null

function resetFixtures() {
  DESTS = [
    { name: 'demo', type: 'sqlite', default: true, status: 'connected' },
    { name: 'sales', type: 'mysql', status: 'connected' },
    { name: 'cold', type: 'mysql', status: 'disconnected' },
  ]
  LESSONS = [
    {
      // vote-sourced lesson: question only, no pattern — the shape that used
      // to 404 because the page keyed on `pattern`.
      question: 'Which region has the highest loan total?',
      note: 'USE 地区 as the dimension',
      sql_snippet: 'SELECT region, SUM(amount) FROM loan GROUP BY region',
      confirmed: false,
      upvotes: 4,
      downvotes: 0,
      confidence: 0.6,
      source: 'user_feedback',
      evidence: '3 corrections in one session',
      created_at: '2026-09-20T08:00:00Z',
    },
    {
      pattern: 'null_guarded_comparison',
      question: 'How many accounts are over the average balance?',
      note: 'Compare against the average of non-null rows',
      confirmed: false,
      upvotes: 1,
      downvotes: 2,
      source: 'auto_failure',
      // no created_at: the column must show "—", not a fabricated date
    },
    {
      pattern: 'explicit_group_by',
      question: 'Never drop the GROUP BY',
      confirmed: true,
      upvotes: 7,
      downvotes: 0,
      confidence: 0.9,
      source: 'manual',
      created_at: '2026-08-01T08:00:00Z',
    },
  ]
  PENDING = [
    {
      question: 'Top 5 districts by average salary',
      sql: 'SELECT district, AVG(salary) FROM account GROUP BY district ORDER BY 2 DESC LIMIT 5',
      tags: ['ranking'],
      created_at: '2026-09-25T09:30:00Z',
    },
    {
      question: 'How many loans were issued last quarter?',
      sql: 'SELECT COUNT(*) FROM loan WHERE date >= "1997-01-01"',
      tags: ['auto'],
    },
  ]
  DETAIL = {
    status: {
      initialized: true,
      files: ['schema_notes.yml', 'semantics.yml', 'examples.yml', 'lessons.yml'],
      items: { metric: 2, entity: 1, table: 1, example: 1, rule: 1, lesson: 3 },
      assets: [
        {
          file: 'schema_notes.yml',
          format: 1,
          generator: 'kb init',
          trove: '0.4.1',
          generated_at: '2026-09-01T10:00:00Z',
          edited: false,
          has_baseline: true,
        },
        {
          file: 'examples.yml',
          format: 1,
          generator: 'kb init',
          trove: '0.4.1',
          generated_at: '2026-09-01T10:01:00Z',
          edited: true,
          has_baseline: true,
        },
        {
          file: 'semantics.yml',
          format: 0,
          generator: 'kb init',
          trove: '0.3.0',
          generated_at: '2026-08-01T10:00:00Z',
          edited: null,
          has_baseline: false,
          needs_migration: true,
        },
        { file: 'lessons.yml', format: 1, generator: 'kb init', generated_at: '', edited: false },
      ],
      refused_assets: { 'demo/lessons.yml': 'mirror refused: unreadable YAML' },
    },
    terms: [],
    examples: [
      {
        question: 'What is the total loan amount?',
        sql: 'SELECT SUM(amount) FROM loan',
        tags: ['aggregate'],
        template: true,
        status: 'certified',
        approved_by: 'admin',
        approved_at: '2026-08-02T10:00:00Z',
        source: 'kb_init',
      },
      {
        question: 'Count of accounts per district',
        sql: 'SELECT district, COUNT(*) FROM account GROUP BY district',
        tags: [],
        status: 'draft',
      },
    ],
    rules: ['Never compare against a bare average — guard NULLs first'],
    lessons: LESSONS,
  }
  confirmGate = {}
  failDs = null
}

const ENTRIES = [
  {
    kind: 'metric',
    key: 'total_loan_amount',
    name: 'total_loan_amount',
    aliases: ['loan volume'],
    definition: 'Sum of all loan amounts',
    expression: 'SUM(loan.amount)',
    datasets: ['loan'],
  },
  {
    kind: 'entity',
    key: 'district',
    name: 'district',
    dataset: 'account',
    field: 'district',
    role: 'dimension',
    synonyms: ['区域'],
    description: 'The district an account belongs to',
  },
  {
    kind: 'table',
    key: 'loan',
    description: 'One row per loan',
    columns: {},
    row_count: 682,
  },
]

/** A real server serialises fresh JSON on every read; a shared object
 *  reference would make the page's ref() see no change at all. */
function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

function mockApi() {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path === '/v1/admin/datasources') return { datasources: DESTS }
    const kb = path.match(/^\/v1\/admin\/datasources\/([^/]+)\/kb$/)
    if (kb) {
      if (failDs === kb[1]) throw new ApiError(500, 'kb detail exploded')
      return clone({ kb: DETAIL })
    }
    if (path.startsWith('/v1/kb/entries?')) return clone({ entries: ENTRIES })
    if (path.startsWith('/v1/kb/examples/pending?')) return clone({ examples: PENDING })
    if (path.endsWith('/kb/init/status')) return { status: 'done', stage: 'done', progress: 100 }
    if (path.endsWith('/kb/reload/status')) return { status: 'done' }
    return {}
  })

  ;(apiPost as any).mockImplementation(async (path: string, body: Record<string, unknown> = {}) => {
    if (path === '/v1/kb/lessons/confirm-one') {
      const key = String(body.key)
      const gate = confirmGate[key]
      if (gate) throw new ApiError(409, `certification gate refused: ${gate}`)
      const lesson = LESSONS.find(
        (l) => !l.confirmed && (l.pattern === key || l.question === key),
      )
      if (!lesson) throw new ApiError(404, `lesson not found: ${key}`)
      if (body.note !== undefined) lesson.note = String(body.note)
      lesson.confirmed = true
      return { status: 'confirmed', key, audit: 'kb.lesson.confirm' }
    }
    if (path === '/v1/kb/lessons/reject-one') {
      const key = String(body.key)
      const before = LESSONS.length
      LESSONS = LESSONS.filter((l) => l.pattern !== key && l.question !== key)
      if (LESSONS.length === before) throw new ApiError(404, `lesson not found: ${key}`)
      if (DETAIL) DETAIL.lessons = LESSONS
      return { status: 'rejected', key, audit: 'kb.lesson.reject' }
    }
    if (path === '/v1/kb/examples/confirm-one') {
      const q = String(body.question)
      const row = PENDING.find((p) => p.question === q)
      if (!row) throw new ApiError(404, `pending example not found: ${q}`)
      PENDING = PENDING.filter((p) => p.question !== q)
      return { status: 'confirmed', question: q, audit: 'kb.example.confirm' }
    }
    if (path === '/v1/kb/examples/reject-one') {
      const q = String(body.question)
      PENDING = PENDING.filter((p) => p.question !== q)
      return { status: 'rejected', question: q, audit: 'kb.example.reject' }
    }
    return {}
  })

  ;(apiDelete as any).mockResolvedValue({ kb: DETAIL })
}

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountView(url = '/admin/kb?ds=demo') {
  wrapper?.unmount()
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/kb', component: { render: () => null } },
      { path: '/admin/datasources', component: { render: () => null } },
    ],
  })
  await router.push(url)
  await router.isReady()
  wrapper = mount(KbView, {
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

function findButton(root: ParentNode, text: string): HTMLElement | null {
  return (
    Array.from(root.querySelectorAll<HTMLElement>('button')).find((b) =>
      (b.textContent ?? '').trim().includes(text),
    ) ?? null
  )
}

function tabBtn(view: VueWrapper, label: string) {
  const tab = view.findAll('.kb-tab').find((el) => el.text().includes(label))
  if (!tab) throw new Error(`tab not found: ${label}`)
  return tab
}

function card(view: VueWrapper, title: string) {
  const found = view.findAll('.diff-card').find((el) => el.text().includes(title))
  if (!found) throw new Error(`card not found: ${title}`)
  return found
}

function kpiTile(view: VueWrapper, label: string) {
  const tile = view.findAll('.kpi-tile').find((el) => el.text().includes(label))
  if (!tile) throw new Error(`KPI tile not found: ${label}`)
  return tile
}

function confirmDialog(): HTMLElement {
  const panel = document.body.querySelector<HTMLElement>('.confirm-panel')
  if (!panel) throw new Error('confirm dialog not open')
  return panel
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
  ;(apiDelete as any).mockResolvedValue({})
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('KbView', () => {
  it('renders tabs with counts and KPIs from the three real endpoints', async () => {
    mockApi()
    const view = await mountView()
    const tabs = view.findAll('.kb-tab').map((t) => t.text())
    expect(tabs).toHaveLength(6)
    const count = (label: string) => tabBtn(view, label).find('.tab-badge').text()
    expect(count('Pending')).toBe('4') // 2 pending lessons + 2 example drafts
    expect(count('Assets')).toBe('4')
    expect(count('Terms & metrics')).toBe('3')
    expect(count('Examples')).toBe('4') // 2 pending drafts + 2 mirror rows
    expect(count('Lessons')).toBe('3')
    expect(count('Rules')).toBe('1')

    // KPI: adoption health is adopted·refused, sub names the refused count
    const health = kpiTile(view, 'Asset health')
    expect(health.text()).toContain('3·1')
    expect(health.text()).toContain('1 not adopted')
    expect(kpiTile(view, 'Pending review').text()).toContain('4')
    expect(kpiTile(view, 'Lessons').text()).toContain('pending 2')
    expect(kpiTile(view, 'Examples').text()).toContain('pending 2')

    const paths = (apiGet as any).mock.calls.map((c: unknown[]) => String(c[0]))
    expect(paths).toContain('/v1/admin/datasources/demo/kb')
    expect(paths).toContain('/v1/kb/entries?datasource=demo')
    expect(paths).toContain('/v1/kb/examples/pending?datasource=demo')
  })

  it('is shareable: tab and filters live in the URL and are read back', async () => {
    mockApi()
    const view = await mountView('/admin/kb?ds=demo&tab=lessons&status=pending')
    expect(router.currentRoute.value.query.tab).toBe('lessons')
    await flushPromises()
    // the lessons table honours the status filter from the URL
    expect(view.findAll('.dt-row')).toHaveLength(2)
    await tabBtn(view, 'Rules').trigger('click')
    await settle()
    expect(router.currentRoute.value.query.tab).toBe('rules')
    expect(view.text()).toContain('hand-authored with no write path')
    expect(view.text()).toContain('guard NULLs first')
  })

  it('treats KPI tiles as entry points (assets KPI jumps to the problem rows)', async () => {
    mockApi()
    const view = await mountView()
    await kpiTile(view, 'Asset health').trigger('click')
    await settle()
    expect(router.currentRoute.value.query.tab).toBe('assets')
    expect(router.currentRoute.value.query.prob).toBe('1')
    expect(kpiTile(view, 'Asset health').classes()).toContain('is-active')
    // only the refused asset is listed
    expect(view.findAll('.dt-row')).toHaveLength(1)
    await kpiTile(view, 'Pending review').trigger('click')
    await settle()
    // 'pending' is the default tab: the key drops back out of the URL
    expect('tab' in router.currentRoute.value.query).toBe(false)
    expect('prob' in router.currentRoute.value.query).toBe(false)
    expect(view.findAll('.diff-card')).toHaveLength(4)
  })

  it('pending cards carry kind, source, votes, confidence, time and evidence', async () => {
    mockApi()
    const view = await mountView()
    const vote = card(view, 'Which region has the highest loan total?')
    const text = vote.text()
    expect(text).toContain('Lesson')
    expect(text).toContain('User votes')
    expect(text).toContain('▲ 4 · ▼ 0')
    expect(text).toContain('60%')
    expect(text).toContain('2026/09/20')
    expect(text).toContain('3 corrections in one session')
    // auto-captured draft without a timestamp shows an em dash, not a date
    const auto = card(view, 'How many loans were issued last quarter?')
    expect(auto.text()).toContain('Example')
    expect(auto.text()).toContain('Auto-captured')
    expect(auto.text()).toContain('—')
  })

  it('confirms one lesson by question key and shows the audit receipt', async () => {
    mockApi()
    const view = await mountView()
    const vote = card(view, 'Which region has the highest loan total?')
    await findButton(vote.element, 'Confirm')!.click()
    await settle()
    const posts = calls('/v1/kb/lessons/confirm-one')
    expect(posts).toHaveLength(1)
    // the vote-sourced lesson has no pattern: the question is the key
    expect(posts[0]).toEqual({
      datasource: 'demo',
      key: 'Which region has the highest loan total?',
    })
    expect(bodyText()).toContain('Confirmed · audited as kb.lesson.confirm')
    // reload dropped the confirmed card
    expect(
      view.findAll('.diff-card').filter((c) => c.text().includes('highest loan total')),
    ).toHaveLength(0)
  })

  it('edit-then-confirm sends the edited note; an untouched note is not sent', async () => {
    mockApi()
    const view = await mountView()
    const open = async (title: string) => {
      const c = card(view, title)
      await findButton(c.element, 'Edit then confirm')!.click()
      await settle()
      return document.body.querySelector('.admin-dialog textarea') as HTMLTextAreaElement
    }

    const area = await open('How many accounts are over the average balance?')
    expect(area).not.toBeNull()
    await wrapper!.find('.admin-dialog textarea').setValue('guard NULLs before comparing')
    await findButton(
      document.body.querySelector('.admin-dialog')!,
      'Save & confirm',
    )!.click()
    await settle()
    const posts = calls('/v1/kb/lessons/confirm-one')
    expect(posts[0].key).toBe('null_guarded_comparison')
    expect(posts[0].note).toBe('guard NULLs before comparing')

    // unchanged note: the body carries no `note` key at all
    resetFixtures()
    const view2 = await mountView()
    const c = card(view2, 'How many accounts are over the average balance?')
    await findButton(c.element, 'Edit then confirm')!.click()
    await settle()
    await findButton(document.body.querySelector('.admin-dialog')!, 'Save & confirm')!.click()
    await settle()
    const posts2 = calls('/v1/kb/lessons/confirm-one')
    expect(posts2[posts2.length - 1]).toEqual({
      datasource: 'demo',
      key: 'null_guarded_comparison',
    })
    expect(posts2).toHaveLength(2)
  })

  it('rejecting asks for confirmation with the blast radius, then posts per item', async () => {
    mockApi()
    const view = await mountView()
    const c = card(view, 'Which region has the highest loan total?')
    await findButton(c.element, 'Reject')!.click()
    await settle()
    const dialog = confirmDialog()
    expect(dialog.textContent).toContain('Reject 1 pending item(s)?')
    expect(dialog.textContent).toContain('Deletes this lesson')
    // cancel: nothing posted
    await findButton(dialog, 'Cancel')!.click()
    await settle()
    expect(calls('/v1/kb/lessons/reject-one')).toHaveLength(0)

    await findButton(card(view, 'Which region has the highest loan total?').element, 'Reject')!.click()
    await settle()
    await findButton(confirmDialog(), 'Reject')!.click()
    await settle()
    expect(calls('/v1/kb/lessons/reject-one')[0]).toEqual({
      datasource: 'demo',
      key: 'Which region has the highest loan total?',
    })
    expect(bodyText()).toContain('Rejected · audited as kb.lesson.reject')
  })

  it('examples tab shows pending badges, per-row decisions and certified provenance', async () => {
    mockApi()
    const view = await mountView('/admin/kb?ds=demo&tab=examples')
    await flushPromises()
    expect(view.findAll('.dt-row')).toHaveLength(4)
    expect(view.text()).toContain('Pending')
    expect(view.text()).toContain('Certified')
    expect(view.text()).toContain('approved by admin')

    const row = view
      .findAll('.dt-row')
      .find((r) => r.text().includes('Top 5 districts by average salary'))!
    await row.find('.mini-btn.primary').trigger('click')
    await settle()
    const posts = calls('/v1/kb/examples/confirm-one')
    expect(posts).toHaveLength(1)
    expect(posts[0].question).toBe('Top 5 districts by average salary')
    expect(posts[0].sql).toContain('GROUP BY district')
    expect(bodyText()).toContain('Confirmed · audited as kb.example.confirm')

    // the pending segment now has one row left
    await view.findAll('.seg-btn').find((b) => b.text().includes('Pending'))!.trigger('click')
    await settle()
    expect(view.findAll('.dt-row')).toHaveLength(1)
  })

  it('bulk confirm runs per item and reports a partial failure with retry', async () => {
    mockApi()
    confirmGate = { null_guarded_comparison: 'bad SQL slips into the fast path' }
    const view = await mountView()
    await flushPromises()
    const lessonCards = view
      .findAll('.diff-card')
      .filter((c) => c.find('.dc-kind').text() === 'Lesson')
    expect(lessonCards).toHaveLength(2)
    for (const c of lessonCards) await c.find('.dc-check').trigger('change')
    expect(view.find('.bulk-bar').text()).toContain('2 selected')

    await findButton(view.find('.bulk-bar').element, 'Confirm selected')!.click()
    await settle(120)
    expect(calls('/v1/kb/lessons/confirm-one')).toHaveLength(2)
    // one succeeded, one was refused by the gate: partial panel + retry
    const partial = view.find('.partial-panel')
    expect(partial.exists()).toBe(true)
    expect(partial.text()).toContain('Partial failure')
    expect(partial.text()).toContain('over the average balance')
    expect(partial.text()).toContain('bad SQL slips into the fast path')
    expect(bodyText()).not.toContain('All succeeded')

    // the retry is the same single-item call
    delete confirmGate.null_guarded_comparison
    await findButton(partial.element, 'Retry')!.click()
    await settle(120)
    expect(calls('/v1/kb/lessons/confirm-one')).toHaveLength(3)
    expect(view.find('.partial-panel').exists()).toBe(false)
    expect(view.find('.handled-sec').text()).toContain('kb.lesson.confirm')
  })

  it('bulk confirm reports all-success only when every item succeeded', async () => {
    mockApi()
    const view = await mountView()
    await flushPromises()
    for (const check of view.findAll('.dc-check')) await check.trigger('change')
    await findButton(view.find('.bulk-bar').element, 'Confirm selected')!.click()
    await settle(120)
    expect(calls('/v1/kb/lessons/confirm-one').length + calls('/v1/kb/examples/confirm-one').length).toBe(4)
    expect(view.find('.partial-panel').exists()).toBe(false)
    expect(bodyText()).toContain('All succeeded')
  })

  it('flags a refused asset with its reason and offers three dispositions', async () => {
    mockApi()
    const view = await mountView('/admin/kb?ds=demo&tab=assets')
    await flushPromises()
    const rows = view.findAll('.dt-row')
    expect(rows).toHaveLength(4)
    const bad = rows.find((r) => r.text().includes('lessons.yml'))!
    expect(bad.find('.adoption-badge.is-refused').exists()).toBe(true)
    expect(bad.text()).toContain('Not adopted')

    await bad.trigger('click')
    await settle()
    const drawer = document.body.querySelector('.drawer-panel')!
    expect(drawer.textContent).toContain('mirror refused: unreadable YAML')
    expect(drawer.textContent).toContain('Trove version')
    expect(drawer.textContent).toContain('Dispositions')

    // merge is a blast-radius action, not a silent click
    await findButton(drawer, 'Re-initialize')!.click()
    await settle()
    expect(document.body.querySelector('.drawer-panel')).toBeNull()
    const dialog = confirmDialog()
    expect(dialog.textContent).toContain('Re-initialize (merge)')
    expect(dialog.textContent).toContain('billed LLM')
    await findButton(dialog, 'Cancel')!.click()
    await settle()
    expect(calls('/v1/admin/datasources/demo/kb/init')).toHaveLength(0)
  })

  it('overwrite and delete KB go through their own blast-radius dialogs', async () => {
    mockApi()
    const view = await mountView()
    const more = view.find('[aria-label="More actions"]')
    await more.trigger('click')
    await findButton(view.find('.more-menu').element, 'Overwrite rebuild')!.click()
    await settle()
    let dialog = confirmDialog()
    expect(dialog.textContent).toContain('Overwrite the knowledge base (demo)')
    expect(dialog.textContent).toContain('unmerged manual edits are lost')
    expect(dialog.getAttribute('role')).toBe('alertdialog')
    expect(document.body.querySelector('.confirm-btn.is-danger')).not.toBeNull()

    await findButton(dialog, 'Overwrite rebuild')!.click()
    await settle(120)
    expect(calls('/v1/admin/datasources/demo/kb/init')[0]).toEqual({
      overwrite: true,
      force: true,
    })

    await more.trigger('click')
    await findButton(view.find('.more-menu').element, 'Delete KB')!.click()
    await settle()
    dialog = confirmDialog()
    expect(dialog.textContent).toContain('Deletes every YAML asset file under .trove/kb/demo/')
    await findButton(dialog, 'Delete KB')!.click()
    await settle(120)
    expect(apiDelete).toHaveBeenCalledWith('/v1/admin/datasources/demo/kb')
  })

  it('a failed datasource switch clears the content and offers a retry', async () => {
    mockApi()
    const view = await mountView()
    expect(view.findAll('.diff-card').length).toBeGreaterThan(0)

    failDs = 'sales'
    await router.push('/admin/kb?ds=sales')
    await settle(120)
    expect(view.findAll('.diff-card')).toHaveLength(0)
    expect(view.findAll('.dt-row')).toHaveLength(0)
    const panel = view.find('.state-panel.is-error')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('Could not load the knowledge base')
    expect(panel.text()).toContain('will not show the previous datasource')

    failDs = null
    await findButton(panel.element, 'Retry')!.click()
    await settle(120)
    expect(view.find('.state-panel.is-error').exists()).toBe(false)
    expect(view.findAll('.diff-card').length).toBeGreaterThan(0)
  })

  it('an uninitialized datasource shows the empty state and the init CTA', async () => {
    mockApi()
    DETAIL!.status = { initialized: false, assets: [], refused_assets: {}, items: {}, files: [] }
    const view = await mountView()
    const panel = view.find('.state-panel')
    expect(panel.text()).toContain('This datasource has no knowledge base yet')
    await findButton(panel.element, 'Init KB')!.click()
    await settle()
    const dialog = confirmDialog()
    expect(dialog.textContent).toContain('calls the billed LLM')
    await findButton(dialog, 'Init KB')!.click()
    await flushPromises()
    expect(calls('/v1/admin/datasources/demo/kb/init')[0]).toEqual({
      overwrite: false,
      force: false,
    })
  })

  it('terms and metrics list kinds with a client-side search in the URL', async () => {
    mockApi()
    const view = await mountView('/admin/kb?ds=demo&tab=entries')
    await flushPromises()
    const rows = view.findAll('.dt-row')
    expect(rows).toHaveLength(3)
    expect(view.text()).toContain('Metric')
    expect(view.text()).toContain('Dimension')
    expect(view.text()).toContain('Table')
    // the table entry has no `name` field: the mirror key is the name
    expect(rows.find((r) => r.text().includes('Table'))!.text()).toContain('loan')

    await view.find('.toolbar-search input').setValue('district')
    await settle()
    expect(router.currentRoute.value.query.q).toBe('district')
    expect(view.findAll('.dt-row')).toHaveLength(1)

    await view.find('.toolbar-search input').setValue('nothing-matches')
    await settle()
    expect(view.text()).toContain('No entries match')
    await findButton(view.find('.dt-empty-cell').element, 'Clear filters')!.click()
    await settle()
    expect(view.findAll('.dt-row')).toHaveLength(3)
    expect('q' in router.currentRoute.value.query).toBe(false)
  })

  it('the queue-empty state distinguishes "no filters" from "filters match nothing"', async () => {
    mockApi()
    const view = await mountView('/admin/kb?ds=demo&kind=example&src=vote')
    await settle()
    expect(view.text()).toContain('No pending entries under the current filters')
    await findButton(view.element, 'Clear filters')!.click()
    await settle()
    expect(router.currentRoute.value.query.kind).toBeUndefined()
    expect(router.currentRoute.value.query.src).toBeUndefined()
    expect(view.findAll('.diff-card')).toHaveLength(4)

    // nothing pending at all → the "queue is clear" copy, no filter button
    resetFixtures()
    DETAIL!.lessons = DETAIL!.lessons.map((l) => ({ ...l, confirmed: true }))
    PENDING = []
    const view2 = await mountView()
    expect(view2.text()).toContain('Queue is clear')
    expect(findButton(view2.element, 'Clear filters')).toBeNull()
  })

  it('lessons tab shows pending/confirmed segments and the audit-safe time column', async () => {
    mockApi()
    const view = await mountView('/admin/kb?ds=demo&tab=lessons')
    await flushPromises()
    expect(view.findAll('.dt-row')).toHaveLength(3)
    const pendingSeg = view.findAll('.seg-btn').find((b) => b.text().includes('Pending'))!
    expect(pendingSeg.text()).toContain('2')
    await pendingSeg.trigger('click')
    await settle()
    expect(view.findAll('.dt-row')).toHaveLength(2)
    expect(view.text()).toContain('2026/09/20')
    // the lesson with no timestamp shows an em dash instead of a fake date
    const rows = view.findAll('.dt-row')
    const noTime = rows.find((r) => r.text().includes('over the average balance'))!
    expect(noTime.find('.cell-time').text()).toBe('—')

    await view.findAll('.seg-btn').find((b) => b.text().includes('Confirmed'))!.trigger('click')
    await settle()
    expect(view.findAll('.dt-row')).toHaveLength(1)
    expect(view.text()).toContain('Never drop the GROUP BY')
  })

  it('closes the ⋯ menu on an outside click (no stale popover)', async () => {
    mockApi()
    const view = await mountView()
    await view.find('[aria-label="More actions"]').trigger('click')
    await flushPromises()
    expect(view.find('.more-menu').exists()).toBe(true)
    document.body.click()
    await flushPromises()
    expect(view.find('.more-menu').exists()).toBe(false)
    await view.find('[aria-label="More actions"]').trigger('click')
    await flushPromises()
    expect(view.find('.more-menu').exists()).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(view.find('.more-menu').exists()).toBe(false)
  })

  it('picks the default datasource from the URL only when the link omits one', async () => {
    mockApi()
    const view = await mountView('/admin/kb')
    await flushPromises()
    expect(router.currentRoute.value.query.ds).toBe('demo')
    await settle()
    expect(view.findAll('.diff-card')).toHaveLength(4)
  })
})

describe('KbView first paint (F2)', () => {
  it('shows KPI skeletons and a loading panel while /datasources is in flight', async () => {
    mockApi()
    // hold the first leg of the chain open: datasources → KB 三连
    const realImpl = (apiGet as any).getMockImplementation()
    let release!: () => void
    ;(apiGet as any).mockImplementation((path: string) => {
      if (path === '/v1/admin/datasources') {
        return new Promise((resolve) => {
          release = () => resolve({ datasources: clone(DESTS) })
        })
      }
      return realImpl(path)
    })
    const view = await mountView('/admin/kb?ds=demo')

    // mid-flight: same-shape placeholders, no fabricated zeros, no blank body
    expect(view.findAll('.kpi-skel')).toHaveLength(5)
    expect(view.findAll('.kpi-tile')).toHaveLength(0)
    expect(view.find('.kpi-row').text().trim()).toBe('')
    expect(view.find('.state-panel.is-loading').exists()).toBe(true)
    expect(view.find('.state-panel.is-error').exists()).toBe(false)

    release()
    await settle()
    expect(view.findAll('.kpi-skel')).toHaveLength(0)
    expect(view.findAll('.kpi-tile')).toHaveLength(5)
    // the pending tile counts 2 unconfirmed lessons + 2 example drafts
    expect(view.find('.kpi-tile .kpi-value').text()).toBe('4')
    expect(view.find('.state-panel.is-loading').exists()).toBe(false)
  })

  it('drops the KPI row entirely when no datasource is connected', async () => {
    DESTS = []
    mockApi()
    const view = await mountView('/admin/kb')
    await settle()
    // neither zeros nor an eternal shimmer: there is nothing to count
    expect(view.find('.kpi-row').exists()).toBe(false)
    const empty = view.find('.state-panel.is-empty')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('No datasources connected yet')
  })
})
