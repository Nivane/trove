/**
 * OpsView — the P4 quality & cost console (/admin/ops).
 *
 * Exercised against a mock of the *server* contract the page really consumes
 * (GET /v1/admin/quality/overview, GET /v1/admin/usage/overview including its
 * 503-with-full-payload shape) plus a real router, so what the assertions pin
 * is the page's own behaviour:
 *
 *   · the state matrix — loading / first-run empty / block-degraded / page
 *     error & retry, with null rendering as '—' and never as 0;
 *   · the URL protocol — tab / window / the failure list's {q, verdict,
 *     page} all live in the query and survive back/forward;
 *   · ruling ② — the failure list is not deduplicated by qid: a duplicated
 *     qid shows up twice, byte-for-byte as the eval gate sees it;
 *   · ruling ⑤ — the lesson draft is a copy-to-clipboard prefill with zero
 *     new endpoints: the click must not issue any write request.
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

import { apiFetch } from '../src/api/http'
import { t } from '../src/i18n'
import { useUiStore } from '../src/stores/ui'
import OpsView from '../src/views/admin/OpsView.vue'
import type { OpsFailureItem, QualityOverview, UsageOverview } from '../src/api/ops'

const LANG = 'en' as const
const tr = (key: Parameters<typeof t>[0], params?: number | string) => t(key, LANG, params)

/* ── fixtures: the endpoints' fixed shapes ────────────────────────────── */

function failure(over: Partial<OpsFailureItem> = {}): OpsFailureItem {
  return {
    qid: 'q1',
    question: 'How many accounts?',
    verdict: 'MISMATCH',
    path: 'sql_only',
    error: '',
    retries: 0,
    pred_sql: 'SELECT count(*) FROM account',
    gold_sql: 'SELECT count(*) FROM account WHERE status = 1',
    run_id: 'run-1759380000-0001',
    ...over,
  }
}

function quality(): QualityOverview {
  const items = [
    failure(),
    failure({ run_id: 'run-1759380000-0002', retries: 1, error: 'ExecutionError' }),
  ]
  return {
    available: true,
    generated_at: '2026-10-02T10:00:00Z',
    current: {
      path: '.trove/eval/results.jsonl',
      kind: 'eval_bird',
      n: 7,
      n_judged: 7,
      mtime: '2026-10-02T09:59:00Z',
      batch_at: '2026-10-02T09:58:00Z',
      metrics: { ex: 0.7143, compile_hit: 0.8571 },
      coverage: { baseline_qids: 8, covered: 5, ratio: 0.625, duplicate_qids: ['q1'] },
    },
    baseline: {
      path: 'eval/baseline/results.jsonl',
      kind: 'eval_bird',
      n: 8,
      n_judged: 8,
      mtime: '2026-09-30T09:00:00Z',
      batch_at: null,
      metrics: { ex: 0.5, compile_hit: 0.75 },
      coverage: null,
    },
    gate: {
      verdict: 'pass',
      reason: null,
      min_n: 10,
      tolerances: { ex: '0.01' },
      metrics: [
        {
          metric: 'ex',
          baseline: 0.5,
          current: 0.7143,
          delta: 0.2143,
          direction: 'higher',
          tolerance: '0.01',
          ok: true,
          note: '',
        },
        {
          // baseline 侧没测到 → null;页面必须显示 '—',不是 0。
          metric: 'cache_hit_rate',
          baseline: null,
          current: 0.4,
          delta: 0,
          direction: 'higher',
          tolerance: '0.20-r',
          ok: true,
          note: 'baseline unmeasured',
        },
      ],
      unpaired: ['recovery_rate'],
      denominator_notes: [],
    },
    failures: {
      total: 2,
      by_verdict: { MISMATCH: 2 },
      by_path: { sql_only: 2 },
      items,
      truncated: false,
    },
    feedback: {
      up: 3,
      down: 1,
      by_datasource: [{ datasource: 'financial', up: 3, down: 1 }],
      pending_lessons: 2,
      confirmed_lessons: 5,
      pending_examples: 1,
      promotion_enabled: false,
      promotion_threshold: null,
      promotion_net_upvotes_min: 3,
      last_rated_at: '2026-10-01T08:00:00Z',
    },
    not_measured: ['failures.by_error_class', 'feedback.trend'],
    degraded: [],
  }
}

/** 25 failures (q01..q25) — enough to exercise paging. */
function qualityWithMany(): QualityOverview {
  const q = quality()
  const items = Array.from({ length: 25 }, (_, i) =>
    failure({
      qid: `q${String(i + 1).padStart(2, '0')}`,
      question: `Question number ${i + 1}`,
      run_id: `run-1759380000-${String(i + 1).padStart(4, '0')}`,
    }),
  )
  q.failures = { total: 25, by_verdict: { MISMATCH: 25 }, by_path: { sql_only: 25 }, items, truncated: true }
  return q
}

function usage(): UsageOverview {
  return {
    available: true,
    window: { kind: '7d', since: '2026-09-25T00:00:00Z', until: '2026-10-02T00:00:00Z', basis: '' },
    cost: {
      source: 'message_metadata',
      sampled: 12,
      sample_capped: false,
      sample_max: 5000,
      tokens: { prompt: 900, completion: 300, total: 1200 },
      cache_tokens: 150,
      unmeasured: { assistant_messages: 15, without_usage: 3, ratio: 0.2 },
      per_question: { mean_total: 100, median_total: 90 },
      by_model: null,
    },
    budget: {
      source: 'prometheus',
      lifetime: 'process',
      decisions: [{ datasource: 'financial', source: 'budget', verdict: 'allow', count: 12 }],
      degraded: [{ datasource: 'financial', count: 1 }],
      kills: [],
    },
    cache: {
      connector: { hits: 7, lifetime: 'process' },
      answer: null,
      prompt: { cache_hit_rate: 0.31, source: 'scorecard', at: '2026-10-01T00:00:00Z' },
    },
    latency: {
      basis: 'audit_log:query.execute',
      n: 9,
      sample_capped: false,
      p50_ms: 820,
      p95_ms: 1500,
      series: [{ date: '2026-10-01', p50_ms: 800, n: 5 }],
      end_to_end: null,
    },
    not_measured: ['latency.end_to_end'],
    degraded: [],
    generated_at: '2026-10-02T10:00:00Z',
  }
}

/** 没装配任何东西的诚实空态(不是 0)。 */
function usageEmpty(): UsageOverview {
  const u = usage()
  u.available = false
  u.cost = {
    source: 'message_metadata',
    sampled: null,
    sample_capped: false,
    sample_max: 5000,
    tokens: null,
    cache_tokens: null,
    unmeasured: { assistant_messages: null, without_usage: null, ratio: null },
    per_question: null,
    by_model: null,
  }
  u.budget = null
  u.cache = null
  u.latency = {
    basis: 'audit_log:query.execute',
    n: 0,
    sample_capped: false,
    p50_ms: null,
    p95_ms: null,
    series: [],
    end_to_end: null,
  }
  u.degraded = [
    { block: 'cache', source: 'cache.prompt', error: 'FileNotFoundError', at: '2026-10-02T10:00:01Z' },
  ]
  return u
}

/** 整页 503:内部存储探不通,payload 仍然完整。 */
function usageStorageDown(): UsageOverview {
  const u = usageEmpty()
  u.degraded = [
    { block: 'storage', source: 'storage.ping', error: 'OperationalError', at: '2026-10-02T10:00:02Z' },
  ]
  return u
}

/* ── api double: dispatch by URL ──────────────────────────────────────── */

interface Route {
  status: number
  body: unknown
}

let qualityRoute: Route = { status: 200, body: quality() }
let usageRoute: Route = { status: 200, body: usage() }

function response(r: Route) {
  return {
    ok: r.status >= 200 && r.status < 300,
    status: r.status,
    statusText: String(r.status),
    json: async () => r.body,
    text: async () => JSON.stringify(r.body),
  }
}

function installApi() {
  ;(apiFetch as any).mockImplementation(async (url: string) => {
    const u = String(url)
    if (u.includes('/v1/admin/quality/overview')) return response(qualityRoute)
    if (u.includes('/v1/admin/usage/overview')) return response(usageRoute)
    return response({ status: 404, body: { detail: 'not mocked: ' + u } })
  })
}

function usageWindows(): string[] {
  return (apiFetch as any).mock.calls
    .map((c: unknown[]) => String(c[0]))
    .filter((u: string) => u.includes('/v1/admin/usage/overview'))
    .map((u: string) => new URL(u, 'http://x').searchParams.get('window') ?? '')
}

function writeCalls(): unknown[] {
  return (apiFetch as any).mock.calls.filter((c: unknown[]) => {
    const opts = (c[1] ?? {}) as { method?: string }
    return opts.method !== undefined && opts.method !== 'GET'
  })
}

/* ── mounting ─────────────────────────────────────────────────────────── */

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null
let writeText: ReturnType<typeof vi.fn>

async function mountOps(url = '/admin/ops') {
  wrapper?.unmount()
  const routes: RouteRecordRaw[] = [
    { path: '/admin', component: { render: () => null } },
    { path: '/admin/ops', name: 'admin-ops', component: { render: () => null } },
    { path: '/admin/usage', component: { render: () => null } },
  ]
  router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(url)
  await router.isReady()
  wrapper = mount(OpsView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await settle()
  return wrapper
}

async function settle(ms = 25) {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, ms))
  await flushPromises()
}

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

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = LANG
  vi.clearAllMocks()
  qualityRoute = { status: 200, body: quality() }
  usageRoute = { status: 200, body: usage() }
  installApi()
  writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

/* ── quality tab ──────────────────────────────────────────────────────── */

describe('OpsView quality tab', () => {
  it('renders the gate verdict with the coverage chip beside it', async () => {
    const view = await mountOps()
    const gateHead = view.find('.gate-card .card-head')
    expect(gateHead.exists()).toBe(true)
    const text = gateHead.text()
    expect(text).toContain(tr('opsGatePass'))
    expect(text).toContain('5/8')
    expect(text).toContain('62.5%')
    // 覆盖率徽章与判定徽章同框 —— 样本缩水必须和判定一起被看见。
    expect(gateHead.find('.gate-badge').exists()).toBe(true)
    expect(gateHead.find('.cov-chip').exists()).toBe(true)
    // 重复 qid 显式出现(裁决②:失败清单不去重)。
    expect(gateHead.text()).toContain(tr('opsQualityDupQids', 1))
    await view.unmount()
  })

  it('renders null as — and never as 0, and shows both duplicate-qid rows', async () => {
    const view = await mountOps()
    // baseline 为 null 的指标:baseline 单元格是 '—',current 照常有值
    const rows = view.findAll('.metrics-table tbody tr')
    const cacheRow = rows.find((r) => r.text().includes('cache_hit_rate'))
    expect(cacheRow).toBeTruthy()
    const cells = cacheRow!.findAll('td')
    expect(cells[1].text()).toBe('—')
    expect(cells[2].text()).toBe('0.4000')

    // 两条 qid=q1 都渲染(不去重)
    const failRows = view.findAll('.failures-table .dt-row')
    const q1Rows = failRows.filter((r) => r.text().includes('q1'))
    expect(q1Rows.length).toBe(2)
    await view.unmount()
  })

  it('reads filters from the URL and writes them back', async () => {
    qualityRoute = { status: 200, body: qualityWithMany() }
    const view = await mountOps('/admin/ops?verdict=MISMATCH&q=number+7')
    expect(view.findAll('.failures-table .dt-row').length).toBe(1)
    expect(bodyText()).toContain('Question number 7')

    // 清除筛选 → 键从 URL 消失,全部行回来
    findButton(view.element as HTMLElement, tr('opsClearFilters')).click()
    await settle()
    expect(router.currentRoute.value.query.verdict).toBeUndefined()
    expect(router.currentRoute.value.query.q).toBeUndefined()
    expect(view.findAll('.failures-table .dt-row').length).toBe(20)
    await view.unmount()
  })

  it('pages the filtered list with the page key in the URL', async () => {
    qualityRoute = { status: 200, body: qualityWithMany() }
    const view = await mountOps()
    expect(view.findAll('.failures-table .dt-row').length).toBe(20)
    expect(bodyText()).not.toContain('Question number 25')

    const next = view.findAll('.pager-btn').at(-1)!
    await next.trigger('click')
    await settle()
    expect(router.currentRoute.value.query.page).toBe('2')
    expect(bodyText()).toContain('Question number 25')
    await view.unmount()
  })

  it('opens the failure drawer and copies a lesson draft without any write call', async () => {
    const view = await mountOps()
    const row = view.findAll('.failures-table .dt-row')[0]
    await row.trigger('click')
    await settle()

    const drawer = document.body.querySelector('.drawer-panel')
    expect(drawer).not.toBeNull()
    expect(drawer!.textContent).toContain('How many accounts?')
    expect(drawer!.textContent).toContain('SELECT count(*) FROM account WHERE status = 1')

    findButton(drawer!, tr('opsFailureDraft')).click()
    await settle()
    expect(writeText).toHaveBeenCalledTimes(1)
    const draft = writeText.mock.calls[0][0] as string
    expect(draft).toContain('lessons:')
    expect(draft).toContain('confirmed: false')
    // 裁决⑤:纯预填,零新端点 —— 全程没有一个写请求。
    expect(writeCalls()).toEqual([])
    await view.unmount()
  })

  it('surfaces a degraded leg inline with its error type name', async () => {
    const q = quality()
    q.gate.verdict = 'not_concluded'
    q.gate.reason = 'min_n=10 not met (7 judged)'
    q.degraded = [
      { block: 'gate', source: 'gate.compare', error: 'ValueError', at: '2026-10-02T10:00:03Z' },
    ]
    qualityRoute = { status: 200, body: q }
    const view = await mountOps()
    const notice = view.find('.degraded-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('gate.compare')
    expect(notice.text()).toContain('ValueError')
    // 判不了是合法结论,不是通过。
    expect(view.find('.gate-badge').text()).toContain(tr('opsGateNotConcluded'))
    await view.unmount()
  })

  it('shows the first-run empty state when no artifact exists', async () => {
    const q = quality()
    q.available = false
    q.current = null
    q.baseline = null
    q.failures = null
    q.feedback = null
    qualityRoute = { status: 200, body: q }
    const view = await mountOps()
    expect(bodyText()).toContain(tr('opsEmptyFirstRun'))
    await view.unmount()
  })

  it('renders a page error with retry on fetch failure', async () => {
    qualityRoute = { status: 500, body: { detail: 'boom' } }
    const view = await mountOps()
    expect(bodyText()).toContain('boom')
    expect(bodyText()).toContain(tr('opsErrorTitle'))

    qualityRoute = { status: 200, body: quality() }
    findButton(view.element as HTMLElement, tr('opsRetry')).click()
    await settle()
    expect(view.find('.gate-card').exists()).toBe(true)
    await view.unmount()
  })

  it('lists the not-measured keys the endpoint declares', async () => {
    const view = await mountOps()
    const strip = view.find('.nm-strip')
    expect(strip.exists()).toBe(true)
    expect(strip.text()).toContain('failures.by_error_class')
    await view.unmount()
  })
})

/* ── usage tab ────────────────────────────────────────────────────────── */

describe('OpsView usage tab', () => {
  it('switches tabs through the URL and only then fetches usage', async () => {
    const view = await mountOps()
    expect(usageWindows()).toEqual([])

    findButton(view.element as HTMLElement, tr('opsTabUsage')).click()
    await settle()
    expect(router.currentRoute.value.query.tab).toBe('usage')
    expect(usageWindows()).toEqual(['7d'])
    expect(bodyText()).toContain('1,200') // tokens total

    // 窗口切到 30d → URL + 重新取数
    findButton(view.element as HTMLElement, '30d').click()
    await settle()
    expect(router.currentRoute.value.query.window).toBe('30d')
    expect(usageWindows()).toEqual(['7d', '30d'])
    await view.unmount()
  })

  it('follows /admin/usage?tab=usage deep links', async () => {
    const view = await mountOps('/admin/ops?tab=usage&window=90d')
    expect(usageWindows()).toEqual(['90d'])
    expect(bodyText()).toContain(tr('opsPerfP50'))
    await view.unmount()
  })

  it('renders null as — and measured zero as 0', async () => {
    usageRoute = { status: 200, body: usageEmpty() }
    const view = await mountOps('/admin/ops?tab=usage')
    // 存储没装配 → 明说「没有数据源」,不是 0
    expect(bodyText()).toContain(tr('opsCostUnwired'))
    expect(bodyText()).toContain(tr('opsPerfNoData'))
    expect(bodyText()).toContain('—')

    // 采样测到、窗口内却一条都没标注 → 求和留空 + 明说原因(仍是 null,不是假和)
    const u = usage()
    u.cost!.sampled = 0
    u.cost!.tokens = null
    u.cost!.cache_tokens = null
    u.cost!.unmeasured = { assistant_messages: 0, without_usage: 0, ratio: 0 }
    u.cost!.per_question = null
    usageRoute = { status: 200, body: u }
    const view2 = await mountOps('/admin/ops?tab=usage')
    expect(bodyText()).toContain(tr('opsCostNoMeasurement'))
    // 测到且为零的计数器就是 0(未计量 0 条)—— 与「没测到」区分开
    const unmeasuredTile = view2
      .findAll('.kpi-tile')
      .find((el) => el.text().includes(tr('opsCostUnmeasured')))
    expect(unmeasuredTile).toBeTruthy()
    expect(unmeasuredTile!.find('.kpi-value').text()).toBe('0')
    await view.unmount()
    await view2.unmount()
  })

  it('warns when the cost sample is capped and nulls the sums', async () => {
    const u = usage()
    u.cost!.sample_capped = true
    u.cost!.tokens = null
    u.cost!.cache_tokens = null
    u.cost!.unmeasured = { assistant_messages: 5000, without_usage: 900, ratio: 0.18 }
    usageRoute = { status: 200, body: u }
    const view = await mountOps('/admin/ops?tab=usage')
    expect(bodyText()).toContain(tr('opsCostCapped', 5000))
    await view.unmount()
  })

  it('renders the 503-with-payload as a page-level degraded notice, not a page error', async () => {
    usageRoute = { status: 503, body: usageStorageDown() }
    const view = await mountOps('/admin/ops?tab=usage')
    const notice = view.find('.degraded-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('storage.ping')
    expect(notice.text()).toContain('OperationalError')
    // 页面本体还在:这是降级不是整页错误。
    expect(bodyText()).not.toContain(tr('opsErrorTitle'))
    expect(view.find('.ops-card').exists()).toBe(true)
    await view.unmount()
  })

  it('renders the budget/cache blocks with process-lifetime labels', async () => {
    const view = await mountOps('/admin/ops?tab=usage')
    expect(bodyText()).toContain(tr('opsBudgetDecisions'))
    expect(bodyText()).toContain(tr('opsCachePrompt'))
    // 进程生命周期,不是窗口 —— 标签必须说出来
    expect(bodyText()).toContain(tr('opsLifetimeProcess'))
    await view.unmount()
  })
})
