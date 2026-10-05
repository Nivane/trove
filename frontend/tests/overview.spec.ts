/**
 * OverviewView — the P3 dashboard (/admin).
 *
 * Exercised against a mock of the *server* contract the page really consumes
 * (GET /v1/admin/overview, including its 503-with-full-payload shape) plus a
 * real router, so what the assertions pin is the page's own behaviour:
 *
 *   · normal render — banner first, six KPIs that are entry points, the
 *     datasource table, the eleven-kind todo queue, wizard and events;
 *   · degraded render — a block the aggregate could not fetch keeps its own
 *     error face, an inexact count renders as ≥ N, and null never becomes 0;
 *   · URL protocol — win lives in the query (invalid values self-heal),
 *     #todos anchors navigate, /admin/overview redirects to /admin with
 *     query and hash preserved (P6 cross-doc decision).
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

import { apiFetch, ApiError } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import OverviewView from '../src/views/admin/OverviewView.vue'
import type { OverviewPayload } from '../src/api/overview'

/* ── fixtures: the endpoint's fixed shape ─────────────────────────────── */

function healthy(): OverviewPayload {
  return {
    generated_at: '2026-10-02T10:00:00Z',
    elapsed_ms: 240,
    window: '24h',
    health: {
      status: 'ok',
      storage: { ok: true },
      llm: { mock: false, target: 'gpt-4o', providers: 2 },
      datasources: {
        demo: { ok: true, readonly: { verified: true, basis: 'grants' } },
        wh: { ok: true, readonly: { verified: null, basis: 'not_probed' } },
      },
    },
    usage: {
      available: true,
      questions: 120,
      ok: 111,
      success_rate: 0.925,
      failures: 9,
      failures_by_class: [
        { class: 'Timeout', domain: 'db', count: 6 },
        { class: 'RuntimeError', domain: '', count: 3 },
      ],
      failures_source: 'audit_text',
      failures_approximate: true,
      sample_size: 500,
      sample_capped: false,
      count_exact: true,
    },
    todos: {
      total: 319,
      count_exact: false,
      items: [
        {
          kind: 'kb_lesson',
          count: 21,
          count_exact: true,
          available: true,
          samples: ['never drop GROUP BY'],
          href: '/admin/kb?tab=lessons',
          note: '',
        },
        {
          kind: 'kb_example',
          count: 280,
          count_exact: false,
          available: true,
          samples: [],
          href: '/admin/kb?tab=examples',
          note: 'capped',
        },
        {
          kind: 'semantic_draft',
          count: 3,
          count_exact: true,
          available: true,
          samples: ['loan'],
          href: '/admin/semantic?tab=pending',
          note: '',
        },
        {
          kind: 'skill_draft',
          count: 0,
          count_exact: true,
          available: true,
          samples: [],
          href: '/admin/skills',
          note: '',
        },
        {
          kind: 'memory_preference',
          count: 4,
          count_exact: true,
          available: true,
          samples: [],
          href: null,
          note: '',
        },
        {
          kind: 'drift',
          count: 5,
          count_exact: false,
          available: true,
          samples: [],
          href: '/admin/datasources',
          note: 'capped',
        },
        {
          kind: 'action_template',
          count: 0,
          count_exact: true,
          available: true,
          samples: [],
          href: '/admin/actions?tab=templates',
          note: '',
        },
        {
          kind: 'action_proposal',
          count: 0,
          count_exact: true,
          available: true,
          samples: [],
          href: '/admin/actions?tab=proposals&status=open',
          note: '',
        },
        {
          kind: 'job_failed',
          count: 2,
          count_exact: true,
          available: true,
          samples: ['nightly'],
          href: '/admin/jobs?status=error',
          note: '',
        },
        {
          kind: 'user_nogrant',
          count: 3,
          count_exact: true,
          available: true,
          samples: [],
          href: '/admin/users?status=nogrant',
          note: '',
        },
        {
          kind: 'datasource_uninitialized',
          count: 1,
          count_exact: true,
          available: true,
          samples: ['wh'],
          href: '/admin/datasources',
          note: '',
        },
      ],
    },
    datasources: [
      {
        name: 'demo',
        status: 'connected',
        kb_initialized: true,
        kb_items: { lessons: 12, examples: 40 },
        refused: 0,
        drift_open: 0,
        drift_count_exact: true,
        readonly: { verified: true, basis: 'grants' },
      },
      {
        name: 'wh',
        status: 'connected',
        kb_initialized: true,
        kb_items: { examples: 5 },
        refused: 1,
        drift_open: 5,
        drift_count_exact: false,
        readonly: { verified: null, basis: 'not_probed' },
      },
    ],
    wizard: { registered: 2, kb_initialized: 2, users_without_grant: 3 },
    recent_events: [
      {
        ts: '2026-10-02T09:59:00Z',
        action: 'query.execute',
        username: 'admin',
        status: 200,
        href: '/admin/audit?action=query.execute',
      },
    ],
    degraded: [],
  }
}

/** A block-level degradation: usage and todos unreadable, one source down. */
function degraded(): OverviewPayload {
  const p = healthy()
  p.health = {
    status: 'degraded',
    storage: { ok: true },
    llm: { mock: true, target: '', providers: 0 },
    datasources: {
      demo: { ok: true, readonly: { verified: true, basis: 'grants' } },
      wh: {
        ok: false,
        error: 'OperationalError',
        readonly: { verified: null, basis: 'probe_failed' },
      },
    },
  }
  p.usage = null
  p.todos = null
  p.datasources![0].drift_open = null
  p.datasources![0].drift_count_exact = false
  p.degraded = [
    {
      block: 'usage',
      source: 'usage.questions',
      error: 'RuntimeError',
      at: '2026-10-02T10:00:01Z',
    },
    {
      block: 'todos',
      source: 'todos.kb_lesson',
      error: 'Timeout',
      at: '2026-10-02T10:00:02Z',
    },
    {
      block: 'datasources',
      source: 'drift:wh',
      error: 'OperationalError',
      at: '2026-10-02T10:00:03Z',
    },
  ]
  return p
}

/** The storage-level failure: HTTP 503, full payload, health unavailable. */
function unavailable(): OverviewPayload {
  const p = healthy()
  p.health = {
    status: 'unavailable',
    storage: { ok: false, error: 'RuntimeError' },
    llm: { mock: true, target: '', providers: 0 },
    datasources: null,
  }
  return p
}

/* ── api double: apiFetch returns a duck-typed response ───────────────── */

let respond: () => unknown = () => healthy()

function setPayload(p: unknown) {
  respond = () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => p,
  })
}

function setHttp(status: number, body: unknown) {
  respond = () => ({
    ok: false,
    status,
    statusText: String(status),
    json: async () => body,
  })
}

function fetchedWindows(): string[] {
  return (apiFetch as any).mock.calls.map(
    (c: unknown[]) => new URL(String(c[0]), 'http://x').searchParams.get('window') ?? '',
  )
}

/* ── mounting ─────────────────────────────────────────────────────────── */

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountView(url = '/admin', opts: { ops?: boolean } = {}) {
  wrapper?.unmount()
  const routes: RouteRecordRaw[] = [
    { path: '/admin', component: { render: () => null } },
    { path: '/admin/datasources', component: { render: () => null } },
    { path: '/admin/kb', component: { render: () => null } },
    { path: '/admin/jobs', component: { render: () => null } },
    { path: '/admin/audit', component: { render: () => null } },
    { path: '/admin/users', component: { render: () => null } },
    { path: '/admin/semantic', component: { render: () => null } },
    { path: '/admin/skills', component: { render: () => null } },
  ]
  if (opts.ops) {
    routes.push({ path: '/admin/ops', name: 'admin-ops', component: { render: () => null } })
  }
  router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(url)
  await router.isReady()
  wrapper = mount(OverviewView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await settle()
  return wrapper
}

async function settle(ms = 30) {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, ms))
  await flushPromises()
}

function kpiTile(view: VueWrapper, label: string) {
  const tile = view.findAll('.kpi-tile').find((el) => el.text().includes(label))
  if (!tile) throw new Error(`KPI tile not found: ${label}`)
  return tile
}

function moduleError(view: VueWrapper, title: string) {
  const card = view.findAll('.module-error').find((el) => el.text().includes(title))
  if (!card) throw new Error(`module error card not found: ${title}`)
  return card
}

function hrefs(view: VueWrapper): string[] {
  return view.findAll('a').map((a) => a.attributes('href') ?? '')
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  // W5: /admin is reached only as an authenticated admin, and the read-only
  // gating reads this store — pin the real precondition, or the page renders
  // read-only and silently hides the exits these tests are about.
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  setPayload(healthy())
  ;(apiFetch as any).mockImplementation(async () => respond())
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

/* ── tests ────────────────────────────────────────────────────────────── */

describe('OverviewView', () => {
  it('renders banner, six KPIs, datasource table, todos, wizard and events', async () => {
    const view = await mountView('/admin')

    // one round trip, window defaulted and echoed into the request
    expect(apiFetch).toHaveBeenCalledTimes(1)
    expect(fetchedWindows()).toEqual(['24h'])

    // banner first, healthy tone, facts with no invented verdict
    const banner = view.find('.health-banner')
    expect(banner.classes()).toContain('is-ok')
    expect(banner.text()).toContain('All 2 datasources reachable')
    expect(banner.text()).toContain('2/2')
    expect(banner.text()).toContain('1 verified')

    // six KPI tiles, each carrying its own count discipline
    expect(view.findAll('.kpi-tile')).toHaveLength(6)
    expect(kpiTile(view, 'Pending items').text()).toContain('≥ 319')
    expect(kpiTile(view, 'Pending items').text()).toContain('11 sources total')
    expect(kpiTile(view, 'Questions').text()).toContain('120')
    expect(kpiTile(view, 'Questions').text()).toContain('last 24h')
    expect(kpiTile(view, 'Success rate').text()).toContain('92.5%')
    expect(kpiTile(view, 'Failed jobs').text()).toContain('2')
    expect(kpiTile(view, 'Refused assets').text()).toContain('1')
    // drift: one leg inexact → lower bound, never a precise-looking low number
    expect(kpiTile(view, 'Drift').text()).toContain('≥ 5')

    // failure buckets are labelled approximate
    const strip = view.find('.ov-strip')
    expect(strip.text()).toContain('Failure buckets')
    expect(strip.text()).toContain('Timeout')
    expect(strip.text()).toContain('6')
    expect(strip.text()).toContain('approximate')

    // datasource table: both rows, assets and the three-state readonly check
    expect(view.findAll('.dt-row')).toHaveLength(2)
    const demoRow = view.findAll('.dt-row')[0]
    expect(demoRow.text()).toContain('demo')
    expect(demoRow.text()).toContain('12 lessons · 40 examples')
    expect(demoRow.text()).toContain('verified')

    // todo queue: all eleven kinds, ≥ N for capped, an explicit no-page chip
    const tq = view.findAll('.tq-row')
    expect(tq).toHaveLength(11)
    // every server kind must land on a label + icon — a kind missing from
    // TodoQueue's maps renders a blank row (the regression this pins)
    const labels = tq.map((r) => r.find('.tq-label').text())
    expect(labels.every((l) => l.trim().length > 0)).toBe(true)
    expect(labels).toContain('Action templates')
    expect(labels).toContain('Action proposals')
    expect(labels).toContain('Uninitialized datasources')
    expect(view.findAll('.tq-row .tq-icon svg')).toHaveLength(11)
    const examples = tq.find((r) => r.text().includes('KB examples'))!
    expect(examples.text()).toContain('≥ 280')
    expect(examples.text()).toContain('capped')
    const memPref = tq.find((r) => r.text().includes('Memory preference drafts'))!
    expect(memPref.text()).toContain('no page yet')
    expect(view.find('#todos').text()).toContain('≥ 319')

    // wizard step 3 is the one still open, and links to its filter
    expect(hrefs(view)).toContain('/admin/users?status=nogrant')

    // events: audit rows with their own deep links
    expect(view.find('.ov-events').text()).toContain('query.execute')

    // P4's ops page is not registered yet — the cost card's drill-down must
    // fall back to the audit stream rather than being a dead link
    const costCard = view
      .findAll('.ov-card')
      .find((c) => c.text().includes('Cost & cache'))!
    expect(costCard.find('a').attributes('href')).toBe(
      '/admin/audit?action=query.execute',
    )

    // refresh re-fetches in place
    const refresh = view
      .findAll('button')
      .find((b) => b.text().includes('Refresh'))!
    await refresh.trigger('click')
    await settle()
    expect(apiFetch).toHaveBeenCalledTimes(2)
  })

  it('uses the ops console link once that route exists', async () => {
    const view = await mountView('/admin?win=7d', { ops: true })
    const costCard = view
      .findAll('.ov-card')
      .find((c) => c.text().includes('Cost & cache'))!
    expect(costCard.find('a').attributes('href')).toBe(
      '/admin/ops?tab=usage&win=7d',
    )
    // the usage KPI drills into the same page, window included
    await kpiTile(view, 'Questions').trigger('click')
    await settle()
    expect(router.currentRoute.value.fullPath).toBe('/admin/ops?tab=usage&win=7d')
    expect(fetchedWindows()).toEqual(['7d'])
  })

  it('opens a datasource drawer and closes it again', async () => {
    const view = await mountView('/admin')
    await view.findAll('.dt-row')[0].trigger('click')
    await settle()

    const panel = document.body.querySelector<HTMLElement>('.drawer-panel')
    expect(panel).not.toBeNull()
    expect(panel!.textContent).toContain('demo')
    expect(panel!.textContent).toContain('grants')
    expect(panel!.textContent).toContain('Manage on the datasources page')

    await panel!.querySelector<HTMLElement>('.drawer-close')!.click()
    await settle()
    expect(document.body.querySelector('.drawer-panel')).toBeNull()
  })

  describe('degraded blocks', () => {
    it('keeps a per-block error face, ≥ N and — instead of 0', async () => {
      setPayload(degraded())
      const view = await mountView('/admin')

      // banner tells the truth: one source unreachable, error as type name
      const banner = view.find('.health-banner')
      expect(banner.classes()).toContain('is-degraded')
      expect(banner.text()).toContain('1 datasources unreachable')
      expect(banner.text()).toContain('wh')
      expect(banner.text()).toContain('OperationalError')

      // usage and todos: their own cards, their own retry — page stays up
      expect(moduleError(view, 'Usage metrics').text()).toContain('RuntimeError')
      expect(moduleError(view, 'Todo queue').text()).toContain('Timeout')
      expect(view.find('#todos').text()).not.toContain('0 sources')

      // an unfetchable count is an em dash, not a reassuring zero
      expect(kpiTile(view, 'Pending items').text()).toContain('—')
      expect(kpiTile(view, 'Pending items').text()).toContain('block unavailable')
      expect(kpiTile(view, 'Questions').text()).toContain('—')
      expect(kpiTile(view, 'Success rate').text()).toContain('—')

      // datasource rows survive a degraded drift leg; the strip says why
      expect(view.findAll('.dt-row')).toHaveLength(2)
      expect(view.find('.ov-warn-strip').text()).toContain('drift:wh')
      expect(view.findAll('.dt-row')[0].text()).toContain('—')
      // …and the drift KPI drops to a lower bound / dash
      expect(kpiTile(view, 'Drift').text()).not.toContain('5')
    })

    it('degrades the banner when the probe leg itself failed (datasources: null)', async () => {
      const p = healthy()
      p.health = {
        status: 'degraded',
        storage: { ok: true },
        llm: { mock: false, target: 'gpt-4o', providers: 2 },
        datasources: null,
      }
      p.degraded = [
        {
          block: 'health',
          source: 'datasources',
          error: 'Timeout',
          at: '2026-10-02T10:00:01Z',
        },
      ]
      setPayload(p)
      const view = await mountView('/admin')

      // 「没探到」不是「全部可达」:status 是 degraded 时绿色 ok 就是假话
      const banner = view.find('.health-banner')
      expect(banner.classes()).toContain('is-degraded')
      expect(banner.text()).toContain('Datasource probe did not complete')
      expect(banner.text()).not.toContain('All 0')

      // 原因给类型名,不给数字 —— 可达性未知就没有 N/M,更没有 0/0
      expect(banner.text()).toContain('✕ Timeout')
      expect(banner.text()).not.toContain('0/0')

      // health 块本身在 → banner 是它唯一的脸,不冒出整块错误卡
      expect(view.find('.module-error').exists()).toBe(false)
    })
  })

  describe('page-level failures', () => {
    it('renders the unavailable face for a 503 that still carries the payload', async () => {
      setHttp(503, unavailable())
      const view = await mountView('/admin')

      const panel = view.find('.state-panel.is-error')
      expect(panel.exists()).toBe(true)
      expect(panel.text()).toContain('Overview unavailable')
      expect(panel.text()).toContain('storage: RuntimeError')
      // nothing pretends to be healthy
      expect(view.find('.health-banner').exists()).toBe(false)
      expect(view.find('#datasources').exists()).toBe(false)

      // retry is wired
      const retry = view
        .findAll('button')
        .find((b) => b.text().includes('Retry'))!
      await retry.trigger('click')
      await settle()
      expect(apiFetch).toHaveBeenCalledTimes(2)
    })

    it('renders the thrown error message when the body is not a payload', async () => {
      setHttp(500, { detail: 'overview exploded' })
      const view = await mountView('/admin')
      const panel = view.find('.state-panel.is-error')
      expect(panel.text()).toContain('overview exploded')
    })

    it('treats a thrown ApiError like any other failure', async () => {
      ;(apiFetch as any).mockImplementation(async () => {
        throw new ApiError(500, 'network down')
      })
      const view = await mountView('/admin')
      expect(view.find('.state-panel.is-error').text()).toContain('network down')
    })
  })

  it('replaces banner and KPIs with onboarding when no datasource exists', async () => {
    const p = healthy()
    p.datasources = []
    p.wizard = { registered: 0, kb_initialized: null, users_without_grant: null }
    setPayload(p)
    const view = await mountView('/admin')

    expect(view.find('.ov-first-empty').exists()).toBe(true)
    expect(view.find('.ov-first-empty').text()).toContain('No datasources yet')
    expect(view.find('.health-banner').exists()).toBe(false)
    expect(view.find('.kpi-row').exists()).toBe(false)
    expect(hrefs(view)).toContain('/admin/datasources')
  })

  describe('URL protocol', () => {
    it('reads win from the query and writes it back on change', async () => {
      const view = await mountView('/admin?win=7d')
      expect(fetchedWindows()).toEqual(['7d'])
      const active = view.findAll('.seg-btn').find((b) => b.classes().includes('is-active'))!
      expect(active.text()).toBe('7d')

      await view.findAll('.seg-btn').find((b) => b.text() === '30d')!.trigger('click')
      await settle()
      expect(fetchedWindows()).toEqual(['7d', '30d'])
      expect(router.currentRoute.value.query.win).toBe('30d')
    })

    it('self-heals an invalid win back to the default', async () => {
      await mountView('/admin?win=99h')
      expect(fetchedWindows()).toEqual(['24h'])
      expect(router.currentRoute.value.query.win).toBeUndefined()
    })

    it('navigates #todos as an anchor, and the drift KPI as a route', async () => {
      const view = await mountView('/admin')
      await kpiTile(view, 'Pending items').trigger('click')
      await settle()
      expect(router.currentRoute.value.hash).toBe('#todos')

      await kpiTile(view, 'Drift').trigger('click')
      await settle()
      expect(router.currentRoute.value.fullPath).toBe('/admin/datasources')
    })
  })
})

describe('OverviewView W5 只读面（analyst 登录后的落地页）', () => {
  function asAnalyst() {
    useAuthStore().user = { id: 2, username: 'ana', role: 'analyst' }
  }

  it('只读出口收窄：建模页深链不渲染，向导整段隐藏，可读出口照常', async () => {
    asAnalyst()
    const view = await mountView('/admin')

    // datasource 卡头的「数据源 →」指向建模组 → analyst 不渲染
    // （审计/成本卡头同用 ov-card-link，但目标是只读面内，保留）
    expect(hrefs(view)).not.toContain('/admin/datasources')
    expect(hrefs(view)).toContain('/admin/audit')
    // 接入向导整段是管理员动作（注册/KB init/授权）→ 连同它的深链一起消失
    expect(view.text()).not.toContain('Onboarding')
    expect(hrefs(view)).not.toContain('/admin/users?status=nogrant')

    // 只读面内的出口不受影响：失败任务 → /admin/jobs、成本 → ops/审计
    const failed = kpiTile(view, 'Failed jobs')
    expect(failed.attributes('disabled')).toBeUndefined()
    await failed.trigger('click')
    await settle()
    expect(router.currentRoute.value.fullPath).toBe('/admin/jobs?status=error')
  })

  it('KPI 出口分档：目标在只读面外的 tile 置灰并说明，不做死点击', async () => {
    asAnalyst()
    const view = await mountView('/admin')

    // refused → /admin/kb、drift → /admin/datasources 都在建模组 → 置灰
    for (const label of ['Refused assets', 'Drift']) {
      const tile = kpiTile(view, label)
      expect(tile.attributes('disabled'), label).toBeDefined()
      expect(tile.attributes('title'), label).toBe('Admin only')
    }
    // 置灰的 tile 点击不导航（不是「点了没反应」，是根本没有出口）
    await kpiTile(view, 'Drift').trigger('click')
    await settle()
    expect(router.currentRoute.value.fullPath).toBe('/admin')

    // 可达出口保持可点：#todos 锚点与 /admin/jobs 都不在置灰之列
    expect(kpiTile(view, 'Pending items').attributes('disabled')).toBeUndefined()
  })
})

describe('admin overview route', () => {
  it('redirects /admin/overview to /admin preserving query and hash', async () => {
    const { router: realRouter } = await import('../src/router')
    const auth = useAuthStore()
    auth.token = 'test-token'
    auth.user = { id: 1, username: 'admin', role: 'admin' }

    await realRouter.push('/admin/overview?win=7d#todos')
    const route = realRouter.currentRoute.value
    expect(route.path).toBe('/admin')
    expect(route.name).toBe('admin-overview')
    expect(route.query).toEqual({ win: '7d' })
    expect(route.hash).toBe('#todos')
  })
})
