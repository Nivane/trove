/**
 * GovernanceView (/admin/governance) — the P5 governance center.
 *
 * Exercised against a mock of the *server* contract the page really consumes
 * (GET /v1/admin/todos, /v1/admin/coverage, /v1/admin/drift[/runs],
 * POST /v1/admin/drift/check including its 503-with-full-report shape, the
 * KB asset inventory, catalog search and the per-kind action endpoints) plus
 * a real router, so the assertions pin the page's own behaviour:
 *
 *   · honesty A/B/C (§6) — a skipped check reads 「检测未能完成 · skip_reason」
 *     and never 「没有该状态的漂移」; null renders as '—' and never as 0;
 *     count_exact:false renders '≥ N';
 *   · acceptance ② — a bulk partial failure lists per-item reasons and the
 *     failed row can be retried alone;
 *   · acceptance ④ — the rollback blast radius is the *runtime* file list
 *     (hardcoding a count would pass a fixture that lies);
 *   · acceptance ⑤ — 「去建模」 deep-links to the semantic page of that
 *     source (not the homepage);
 *   · acceptance ⑥ — filters live in the URL, a tab switch prunes the other
 *     tabs' keys, back restores the previous filter state;
 *   · the route is registered, non-admins are bounced, and the gov* i18n
 *     block stays bilingual.
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

import { ApiError, apiFetch, apiGet, apiPost } from '../src/api/http'
import { messages, t } from '../src/i18n'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import GovernanceView from '../src/views/admin/GovernanceView.vue'
import type {
  GovernanceCoverage,
  GovernanceDriftItem,
  GovernanceDriftRun,
  GovernanceTodos,
  SemanticHistoryEntry,
} from '../src/api/types'
import type { OverviewPayload } from '../src/api/overview'

const LANG = 'en' as const
const tr = (key: Parameters<typeof t>[0], params?: number | string) => t(key, LANG, params)

/* ── fixtures: the endpoints' fixed shapes ────────────────────────────── */

function todosBody(): GovernanceTodos {
  return {
    items: [
      {
        kind: 'kb_lesson',
        id: 'lesson-A',
        ds: 'sales',
        title: 'lesson-A',
        summary: 'always filter status = 1',
        severity: null,
        confidence: 0.62,
        created_at: '2026-10-01T09:00:00Z',
        href: '/admin/kb?tab=lessons',
        actionable: { confirm: true, reject: true, batch: false, edit_url: '/admin/kb?tab=lessons' },
        diff: { before: null, after: 'always filter status = 1', fields: [], action: null, error: null },
        source: 'memory',
      },
      {
        kind: 'kb_lesson',
        id: 'lesson-B',
        ds: 'sales',
        title: 'lesson-B',
        summary: 'never join on customer_id alone',
        severity: null,
        confidence: 0.55,
        created_at: '2026-10-01T10:00:00Z',
        href: '/admin/kb?tab=lessons',
        actionable: { confirm: true, reject: true, batch: false, edit_url: '/admin/kb?tab=lessons' },
        diff: null,
        source: 'memory',
      },
    ],
    total: 2,
    counts: { kb_lesson: 2 },
    generated_at: '2026-10-02T10:00:00Z',
    degraded: [],
  }
}

function coverageBody(): GovernanceCoverage {
  return {
    generated_at: '2026-10-02T10:00:00Z',
    window: '30d',
    sources: [
      {
        ds: 'sales',
        model: { enabled: true, datasets: 2, metrics: 3, declared_tables: ['orders'] },
        physical: { tables: 3, source: 'catalog' },
        uncovered_tables: ['refunds', 'payments'],
        asked_unmodeled: [{ table: 'shipments', queries: 4, last_asked_at: '2026-10-02T08:00:00Z' }],
        refused: null,
      },
    ],
    degraded: [],
  }
}

function driftItem(over: Partial<GovernanceDriftItem> = {}): GovernanceDriftItem {
  return {
    id: 7,
    datasource: 'sales',
    level: 'L1',
    kind: 'column_missing',
    subject: 'orders.discount',
    severity: 'warning',
    status: 'open',
    source: 'detector',
    detail: { column: 'discount' },
    affected: { metrics: [], examples: [], rules: [], lessons: [] },
    first_seen_at: '2026-10-01T00:00:00Z',
    last_seen_at: '2026-10-02T00:00:00Z',
    seen_count: 3,
    resolved_at: null,
    resolved_by: null,
    resolve_reason: null,
    ...over,
  }
}

function run(over: Partial<GovernanceDriftRun> = {}): GovernanceDriftRun {
  return {
    id: 1,
    datasource: 'sales',
    started_at: '2026-10-02T09:00:00Z',
    finished_at: '2026-10-02T09:00:02Z',
    status: 'ok',
    skip_reason: null,
    detected: 0,
    new_count: 0,
    ...over,
  }
}

function history(): SemanticHistoryEntry[] {
  return [
    {
      sha: 'a1b2c3d4e5f60718',
      author: 'admin',
      date: '2026-10-01T12:00:00Z',
      subject: 'semantics: add net revenue metric',
      trailers: 'Trove-Event: semantic.draft.confirm',
    },
  ]
}

function overview(): OverviewPayload {
  return {
    generated_at: '2026-10-02T10:00:00Z',
    elapsed_ms: 12,
    window: '24h',
    health: null,
    usage: null,
    // count_exact:false on the total → the KPI must render '≥ 5', not '5'.
    todos: {
      total: 5,
      count_exact: false,
      items: [
        todo('kb_lesson', 3, true),
        todo('kb_example', null, false),
        todo('semantic_draft', 2, false),
        todo('skill_draft', 0, true),
        todo('memory_preference', 0, true),
        todo('drift', 0, true),
      ],
    },
    datasources: null,
    wizard: null,
    recent_events: null,
    degraded: [],
  } as unknown as OverviewPayload
}

function todo(kind: string, count: number | null, exact: boolean) {
  return {
    kind,
    count,
    count_exact: exact,
    available: count !== null,
    samples: [],
    href: null,
    note: '',
  }
}

/* ── api double: dispatch by URL ──────────────────────────────────────── */

interface Resp {
  status: number
  body: unknown
}

let todosRoute: Resp
let coverageRoute: Resp
let driftLists: Record<string, GovernanceDriftItem[]>
let driftRuns: Record<string, GovernanceDriftRun[]>
let assetsRoute: Resp
let historyRoute: Resp
let checkRoute: Resp
/** id → 'ok' | error message (per-item action endpoints). */
let lessonResults: Record<string, string>
let searchRoute: Resp
let catalogRoute: Resp
let lineageRoute: Resp
let rollbackCalls: string[]

function response(r: Resp) {
  return {
    ok: r.status >= 200 && r.status < 300,
    status: r.status,
    statusText: String(r.status),
    json: async () => r.body,
    text: async () => JSON.stringify(r.body),
  }
}

function qs(url: string): URLSearchParams {
  return new URL(url, 'http://x').searchParams
}

async function apiGetImpl(url: string): Promise<unknown> {
  const u = String(url)
  if (u.includes('/v1/admin/overview')) return overview()
  if (u.includes('/v1/admin/datasources')) {
    return {
      datasources: [
        { name: 'sales', type: 'sqlite', status: 'connected', kb_initialized: true },
        { name: 'risk', type: 'sqlite', status: 'connected', kb_initialized: true },
      ],
    }
  }
  if (u.includes('/v1/admin/todos')) return todosRoute.body
  if (u.includes('/v1/admin/coverage')) return coverageRoute.body
  if (u.includes('/v1/admin/drift/runs')) {
    const ds = qs(u).get('datasource') ?? ''
    return { datasource: ds, runs: driftRuns[ds] ?? [] }
  }
  if (u.includes('/v1/admin/drift')) {
    const ds = qs(u).get('datasource') ?? ''
    const items = driftLists[ds] ?? []
    return { datasource: ds, count: items.length, items }
  }
  if (u.includes('/v1/admin/semantic/') && u.includes('/history')) return historyRoute.body
  if (u.includes('/v1/kb/assets')) return assetsRoute.body
  if (u.includes('/v1/catalog/search')) return searchRoute.body
  // 单表详情带斜杠(/tables/orders),一览没有(/tables?datasource=…)。
  if (u.includes('/v1/catalog/tables/')) return catalogRoute.body
  if (u.includes('/v1/catalog/tables')) return searchRoute.body
  if (u.includes('/v1/lineage/tables/')) return lineageRoute.body
  if (u.includes('/v1/kb/tables/')) throw new ApiError(404, 'no notes for this table')
  throw new ApiError(404, `not mocked: ${u}`)
}

async function apiPostImpl(url: string, body: Record<string, unknown>): Promise<unknown> {
  const u = String(url)
  if (u.includes('/v1/admin/semantic/') && u.includes('/rollback')) {
    rollbackCalls.push(String(body.sha))
    return { rolled_back: true, datasource: 'sales', sha: body.sha }
  }
  if (u.includes('/v1/kb/lessons/confirm-one')) {
    const key = String(body.key)
    const verdict = lessonResults[key]
    if (verdict && verdict !== 'ok') throw new ApiError(422, verdict)
    return { ok: true }
  }
  if (u.includes('/v1/kb/lessons/reject-one')) return { ok: true }
  return { ok: true }
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = LANG
  vi.clearAllMocks()
  todosRoute = { status: 200, body: todosBody() }
  coverageRoute = { status: 200, body: coverageBody() }
  driftLists = { sales: [] }
  driftRuns = { sales: [run()] }
  assetsRoute = {
    status: 200,
    body: {
      datasource: 'sales',
      assets: [
        { file: 'semantics.yml' },
        { file: 'schema_notes.yml' },
        { file: 'decisions.yml' },
      ],
      refused: {},
    },
  }
  historyRoute = { status: 200, body: { datasource: 'sales', history: history() } }
  checkRoute = {
    status: 200,
    body: {
      datasource: 'sales',
      status: 'ok',
      skip_reason: null,
      generated_at: '2026-10-02T10:05:00Z',
      detected: 0,
      new_count: 0,
      levels_verified: ['L1', 'L2'],
      items: [],
    },
  }
  lessonResults = { 'lesson-A': 'ok', 'lesson-B': 'ok' }
  searchRoute = {
    status: 200,
    body: { tables: [{ name: 'orders', schema: 'main', columns: 4, row_count: 120 }] },
  }
  catalogRoute = {
    status: 200,
    body: {
      name: 'orders',
      schema: 'main',
      row_count: 120,
      columns: [{ name: 'order_id', type: 'INTEGER', nullable: false, primary_key: true }],
    },
  }
  lineageRoute = {
    status: 200,
    body: {
      table: 'orders',
      datasource: 'sales',
      upstream: [],
      downstream: [],
      columns: [],
      definitions: [],
      query_log: { count: 0, last_at: null },
    },
  }
  rollbackCalls = []
  ;(apiGet as unknown as ReturnType<typeof vi.fn>).mockImplementation(apiGetImpl)
  ;(apiPost as unknown as ReturnType<typeof vi.fn>).mockImplementation(apiPostImpl)
  // fetchOverview 与漂移检测走 apiFetch(不是 apiGet)。
  ;(apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
    const u = String(url)
    if (u.includes('/v1/admin/overview')) return response({ status: 200, body: overview() })
    return response(checkRoute)
  })
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

async function mountView(url = '/admin/governance') {
  wrapper?.unmount()
  const routes: RouteRecordRaw[] = [
    { path: '/', name: 'chat', component: { render: () => null } },
    { path: '/admin', component: { render: () => null } },
    { path: '/admin/governance', name: 'admin-governance', component: { render: () => null } },
    { path: '/admin/semantic', name: 'admin-semantic', component: { render: () => null } },
    { path: '/admin/datasources', component: { render: () => null } },
    { path: '/admin/kb', component: { render: () => null } },
  ]
  router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(url)
  await router.isReady()
  wrapper = mount(GovernanceView, {
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

/** 确认弹窗的确认键(Teleport 到 body;表行里也有同名按钮,必须按类取)。 */
function dialogConfirm(): HTMLElement {
  const btn = document.body.querySelector('.confirm-btn.is-solid')
  if (!btn) throw new Error('dialog confirm button not found')
  return btn as HTMLElement
}

/** 回滚弹窗的确认键 —— 版本表里还有一枚「Roll back to this」,不能按文案找。 */
function rollbackConfirm(): HTMLElement {
  const btn = document.body.querySelector('.rollback-dialog .confirm-btn.is-solid')
  if (!btn) throw new Error('rollback confirm button not found')
  return btn as HTMLElement
}

/* ── ① 收件箱 ─────────────────────────────────────────────────────────── */

describe('inbox', () => {
  it('confirms a lesson inline (one click, no page jump) and reloads the queue', async () => {
    const view = await mountView('/admin/governance?tab=inbox&ds=sales')
    const before = (apiGet as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
      String(c[0]).includes('/v1/admin/todos'),
    ).length

    const row = view.find('.inbox-table tbody tr')
    expect(row.text()).toContain('lesson-A')
    await findButton(row.element, tr('govInboxConfirm')).click()
    await settle()

    const confirmCalls = (apiPost as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
      String(c[0]).includes('/v1/kb/lessons/confirm-one'),
    )
    expect(confirmCalls.length).toBe(1)
    expect((confirmCalls[0][1] as { key: string }).key).toBe('lesson-A')
    // 列表重取(队列随动作刷新),且没有离开本页。
    const after = (apiGet as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
      String(c[0]).includes('/v1/admin/todos'),
    ).length
    expect(after).toBeGreaterThan(before)
    expect(router.currentRoute.value.path).toBe('/admin/governance')
  })

  it('lists per-item bulk failures with reasons, and retries a single row alone', async () => {
    lessonResults['lesson-B'] = 'stale lesson revision'
    const view = await mountView('/admin/governance?tab=inbox&ds=sales')

    // 选中两行(第一枚 dt-check 是全选)。
    const checks = view.findAll('.inbox-table .dt-check')
    expect(checks.length).toBeGreaterThanOrEqual(3)
    await checks[1].trigger('click')
    await checks[2].trigger('click')
    await settle()

    await findButton(view.element, tr('govBulkConfirmKind', 2)).click()
    await settle()

    // 成败都在结果表里:失败的带原因,并留在结果里可单条重试。
    const resultRows = Array.from(document.body.querySelectorAll('.bb-table tbody tr'))
    expect(resultRows.length).toBe(2)
    const badRow = resultRows.find(
      (r) => (r.querySelector('.bb-status')?.textContent ?? '').includes(tr('govBulkFailed')),
    )
    expect(badRow).toBeTruthy()
    expect(badRow!.textContent).toContain('stale lesson revision')
    expect(badRow!.textContent).toContain('lesson-B')

    // 单条重试:只重发这一条,而且这次成功。
    lessonResults['lesson-B'] = 'ok'
    const postsBefore = (apiPost as unknown as ReturnType<typeof vi.fn>).mock.calls.length
    await findButton(badRow!, tr('retry')).click()
    await settle()
    const posts = (apiPost as unknown as ReturnType<typeof vi.fn>).mock.calls.slice(postsBefore)
    expect(posts.length).toBe(1)
    expect(String(posts[0][0])).toContain('/v1/kb/lessons/confirm-one')
    expect((posts[0][1] as { key: string }).key).toBe('lesson-B')

    const retried = Array.from(document.body.querySelectorAll('.bb-table tbody tr')).find((r) =>
      r.textContent?.includes('lesson-B'),
    )
    expect(retried!.textContent).toContain(tr('govBulkOk'))
  })
})

/* ── ② 覆盖与体检 ─────────────────────────────────────────────────────── */

describe('coverage', () => {
  it('deep-links an asked-but-unmodeled table to the semantic page of its source', async () => {
    const view = await mountView('/admin/governance?tab=coverage&ds=sales')
    const asked = view.find('.ct-asked')
    expect(asked.exists()).toBe(true)
    expect(asked.text()).toContain('shipments')

    const link = Array.from(asked.element.querySelectorAll('a')).find((a) =>
      (a.textContent ?? '').includes(tr('govCovGoModel')),
    )
    expect(link).toBeTruthy()
    ;(link as HTMLElement).click()
    await settle()
    expect(router.currentRoute.value.fullPath).toBe('/admin/semantic?ds=sales')
  })

  it('renders sources:null as 未取到 — not as "no datasources"', async () => {
    coverageRoute = {
      status: 200,
      body: {
        generated_at: '2026-10-02T10:00:00Z',
        window: '30d',
        sources: null,
        degraded: [{ ds: null, error: 'RuntimeError', at: '2026-10-02T10:00:00Z' }],
      },
    }
    const view = await mountView('/admin/governance?tab=coverage')
    expect(bodyText()).toContain(tr('govNotFetched'))
    expect(bodyText()).not.toContain(tr('govCovEmpty'))
    expect(bodyText()).toContain('RuntimeError')
    void view
  })
})

/* ── ③ 漂移与版本(诚实 A:最重要的一条)────────────────────────────── */

describe('drift', () => {
  it('renders a skipped run as 检测未能完成 · skip_reason, never as an empty list', async () => {
    driftRuns.sales = [
      run({ status: 'skipped', skip_reason: 'schema unreadable', detected: 0, new_count: 0 }),
    ]
    driftLists.sales = []
    await mountView('/admin/governance?tab=drift&ds=sales')

    expect(bodyText()).toContain(tr('govDriftSkipped'))
    expect(bodyText()).toContain('schema unreadable')
    expect(bodyText()).toContain(tr('govDriftStaleNote'))
    // 未检测/未完成 ≠ 0 条漂移。
    expect(bodyText()).not.toContain(tr('govDriftEmpty'))
  })

  it('resolves a 503 check response as data and shows the fresh skip_reason', async () => {
    driftRuns.sales = []
    checkRoute = {
      status: 503,
      body: {
        detail: {
          datasource: 'sales',
          status: 'skipped',
          skip_reason: 'connector timeout',
          generated_at: '2026-10-02T10:05:00Z',
          detected: 0,
          new_count: 0,
          levels_verified: [],
          items: [],
        },
      },
    }
    await mountView('/admin/governance?tab=drift&ds=sales')
    expect(bodyText()).toContain(tr('govDriftUnchecked'))

    await findButton(document.body, tr('govDriftCheck')).click()
    await settle()

    const calls = (apiFetch as unknown as ReturnType<typeof vi.fn>).mock.calls
    expect(calls.some((c) => String(c[0]).includes('/v1/admin/drift/check'))).toBe(true)
    expect(bodyText()).toContain('connector timeout')
    expect(bodyText()).toContain(tr('govDriftStaleNote'))
    expect(bodyText()).not.toContain(tr('govDriftEmpty'))
  })

  it('records a resolve verdict with its reason (audit path, §6-3)', async () => {
    driftLists.sales = [driftItem()]
    await mountView('/admin/governance?tab=drift&ds=sales')

    await findButton(document.body, tr('govDriftResolve')).click()
    await settle()
    // 无理由不放行(点弹窗的确认键,不是表行的 Resolve)。
    await dialogConfirm().click()
    await settle()
    let calls = (apiPost as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
      String(c[0]).includes('/resolve'),
    )
    expect(calls.length).toBe(0)
    expect(bodyText()).toContain(tr('govDriftReasonRequired'))

    const input = document.body.querySelector('.gov-reason-input') as HTMLInputElement
    expect(input).toBeTruthy()
    input.value = 'upstream fixed it'
    input.dispatchEvent(new Event('input'))
    await settle()
    await dialogConfirm().click()
    await settle()

    calls = (apiPost as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
      String(c[0]).includes('/resolve'),
    )
    expect(calls.length).toBe(1)
    expect((calls[0][1] as { reason: string }).reason).toBe('upstream fixed it')
    expect(bodyText()).toContain(tr('govActionDone'))
  })

  it('rolls back only after the runtime file list is in place and the name is typed', async () => {
    driftLists.sales = [driftItem()]
    const view = await mountView('/admin/governance?tab=drift&ds=sales')

    await findButton(view.element, tr('govVerRollback')).click()
    await settle()

    // 爆炸半径 = 运行时的 /v1/kb/assets 清单(不是写死的数字/文件名)。
    expect(bodyText()).toContain(tr('govRollFilesLine', 3))
    expect(bodyText()).toContain('decisions.yml')

    // 名字没打对:点不动(handler 层否决,不只是变灰)。
    const input = document.body.querySelector('.rd-input') as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.placeholder).toBe(tr('govRollTypeName', 'sales'))
    rollbackConfirm().click()
    await settle()
    expect(rollbackCalls.length).toBe(0)

    input.value = 'sales'
    input.dispatchEvent(new Event('input'))
    await settle()
    rollbackConfirm().click()
    await settle()

    expect(rollbackCalls).toEqual(['a1b2c3d4e5f60718'])
    expect(bodyText()).toContain(tr('govRollDone'))
  })
})

/* ── ④ 血缘与数据地图 ─────────────────────────────────────────────────── */

describe('lineage', () => {
  it('opens the table drawer from the URL (?table=…) and closing clears the key', async () => {
    const view = await mountView('/admin/governance?tab=lineage&ds=sales')
    await view.find('.lin-row').trigger('click')
    await settle()

    expect(router.currentRoute.value.query.table).toBe('orders')
    expect(bodyText()).toContain('order_id')
    // 冷启动是真实空态:没记录 ≠ 无依赖。
    expect(bodyText()).toContain(tr('govLinHistoryEmpty'))

    const closeBtn = document.body.querySelector('.drawer-close') as HTMLElement
    expect(closeBtn).toBeTruthy()
    expect(closeBtn.getAttribute('aria-label')).toBe(tr('close'))
    closeBtn.click()
    await settle()
    expect(router.currentRoute.value.query.table).toBeUndefined()
  })
})

/* ── KPI 行与 URL 协议 ────────────────────────────────────────────────── */

describe('kpi row', () => {
  it('renders null as — and count_exact:false as ≥ N', async () => {
    const view = await mountView('/admin/governance')
    const tiles = view.findAll('.kpi-tile')
    const byLabel = (label: string) =>
      tiles.find((tile) => tile.text().includes(label))?.text() ?? ''

    expect(byLabel(tr('govKindKbLesson'))).toContain('3')
    expect(byLabel(tr('govKindKbExample'))).toContain('—')
    expect(byLabel(tr('govKindSemanticDraft'))).toContain(tr('govAtLeast', 2))
    expect(byLabel(tr('govKindSkillDraft'))).toContain('0')
    expect(byLabel(tr('govKpiTotal'))).toContain(tr('govAtLeast', 5))
  })
})

describe('inbox total honesty (§6-B)', () => {
  const degradedDrift = {
    kind: 'drift',
    ds: 'sales',
    error: 'capped',
    at: '2026-10-02T10:00:00Z',
  }

  it('marks the total as ≥ N when a degraded leg intersects the slice', async () => {
    todosRoute = { status: 200, body: { ...todosBody(), degraded: [degradedDrift] } }
    const view = await mountView('/admin/governance?tab=inbox')
    expect(view.find('.it-total').text()).toContain(tr('govInboxTotalAtLeast', 2))
  })

  it('keeps the total exact when the degraded leg is outside the requested slice', async () => {
    todosRoute = { status: 200, body: { ...todosBody(), degraded: [degradedDrift] } }

    // kind 筛选把 drift 排除在请求类别外;ds 筛选把别的源排除在切片外。
    let view = await mountView('/admin/governance?tab=inbox&kind=kb_lesson')
    let total = view.find('.it-total').text()
    expect(total).toContain(tr('govInboxTotal', 2))
    expect(total).not.toContain('≥')

    view = await mountView('/admin/governance?tab=inbox&ds=risk')
    total = view.find('.it-total').text()
    expect(total).toContain(tr('govInboxTotal', 2))
    expect(total).not.toContain('≥')
  })
})

describe('url protocol', () => {
  it('hydrates from the query, prunes other tabs on switch, and back restores', async () => {
    const view = await mountView(
      '/admin/governance?tab=inbox&ds=sales&kind=kb_lesson&q=loan&page=2',
    )

    const q = view.find('.filter-input input').element as HTMLInputElement
    expect(q.value).toBe('loan')
    expect(bodyText()).toContain(tr('govInboxPageOf', 2))
    // 服务端分页:offset = (page-1)×50。
    const todoCalls = (apiGet as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) =>
      String(c[0]).includes('/v1/admin/todos'),
    )
    expect(String(todoCalls[todoCalls.length - 1][0])).toContain('offset=50')
    expect(String(todoCalls[todoCalls.length - 1][0])).toContain('kind=kb_lesson')

    await findButton(view.element, tr('govTabCoverage')).click()
    await settle()
    // 局部键被清掉,全局键(ds)保留。
    expect(router.currentRoute.value.query).toEqual({ tab: 'coverage', ds: 'sales' })

    await router.go(-1)
    await settle()
    expect(router.currentRoute.value.query.kind).toBe('kb_lesson')
    expect(router.currentRoute.value.query.page).toBe('2')
  })
})

/* ── 路由与 i18n ──────────────────────────────────────────────────────── */

describe('routing and copy', () => {
  it('registers /admin/governance and keeps non-admins out', async () => {
    const { router: appRouter } = await import('../src/router')
    expect(appRouter.resolve('/admin/governance').name).toBe('admin-governance')

    const auth = useAuthStore()
    auth.token = 'token'
    auth.user = { id: 1, username: 'someone', role: 'user' }
    await appRouter.push('/admin/governance')
    expect(appRouter.currentRoute.value.name).toBe('chat')
  })

  it('keeps every gov* key bilingual', () => {
    const zh = Object.keys(messages.zh).filter((k) => k.startsWith('gov'))
    const en = Object.keys(messages.en).filter((k) => k.startsWith('gov'))
    expect(zh.length).toBeGreaterThan(100)
    expect(new Set(en)).toEqual(new Set(zh))
  })
})
