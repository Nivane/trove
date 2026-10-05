import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import AuditView from '../src/views/admin/AuditView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiPut: vi.fn(),
}))

import { apiGet } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

function makeEntries(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    ts: `2026-08-${String((i % 28) + 1).padStart(2, '0')}T0${(i % 9) + 1}:00:00Z`,
    username: `user${i}`,
    action: 'admin.user.create',
    method: 'POST',
    path: '/v1/admin/users',
    status: 201,
  }))
}

const calls: string[] = []
let currentTotal = 45

/** 拒绝报表响应(A5):新测试按需覆写;默认空窗口。 */
let refusalData: Record<string, unknown> = {
  available: true,
  total: 0,
  capped: false,
  coverage_note: '覆盖范围:经对话链路的拒绝轮都进本表。不含:直接编译调用。',
  coverage_note_en:
    'Covers refusal rounds on the conversation path. Not covered: direct compile calls.',
  by_reason: [],
  by_miss: [],
  top_questions: [],
  relationship_missing: { reasons: [], count: 0, top_questions: [] },
}

/** 只取审计列表请求(排除 /audit/refusal-report —— 它同前缀)。 */
function auditCalls(): string[] {
  return calls.filter((c) => c.includes('/v1/admin/audit?'))
}

let wrapper: VueWrapper | null = null
let router: Router

async function mountView(query = '') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/audit', component: AuditView },
    ],
  })
  await router.push(`/admin/audit${query}`)
  await router.isReady()
  wrapper = mount(AuditView, {
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  currentTotal = 45
  refusalData = {
    available: true,
    total: 0,
    capped: false,
    coverage_note: '覆盖范围:经对话链路的拒绝轮都进本表。不含:直接编译调用。',
    coverage_note_en:
      'Covers refusal rounds on the conversation path. Not covered: direct compile calls.',
    by_reason: [],
    by_miss: [],
    top_questions: [],
    relationship_missing: { reasons: [], count: 0, top_questions: [] },
  }
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  calls.length = 0
  document.body.innerHTML = ''
  ;(apiGet as any).mockImplementation(async (path: string) => {
    calls.push(path)
    if (path.startsWith('/v1/admin/audit/refusal-report')) {
      return JSON.parse(JSON.stringify(refusalData))
    }
    const url = new URL(path, 'http://localhost')
    const limit = Number(url.searchParams.get('limit') ?? 20)
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const all = makeEntries(currentTotal)
    return { audit: all.slice(offset, offset + limit), total: all.length }
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('AuditView pagination', () => {
  it('loads page 2 with the right offset when a pager number is clicked', async () => {
    const view = await mountView()
    expect(calls[0]).toContain('offset=0')

    // click the "2" pager button
    const page2 = view
      .findAll('.el-pager li.number')
      .find((li) => li.text() === '2')
    expect(page2).toBeTruthy()
    await page2!.trigger('click')
    await flushPromises()

    const last = calls[calls.length - 1]
    expect(last).toContain('offset=20')
    const rows = view.findAll('.el-table__body tbody tr')
    expect(rows.length).toBe(20)
  })

  it('keeps the selected page across a refresh', async () => {
    const view = await mountView()
    const page2 = view
      .findAll('.el-pager li.number')
      .find((li) => li.text() === '2')
    await page2!.trigger('click')
    await flushPromises()
    expect(auditCalls().at(-1)).toContain('offset=20')

    // clicking refresh re-reads the same page, not a reset to page 1
    // (refresh also pulls the refusal report —— 校验列表腿,不看最后一条)
    await view.find('.refresh-btn').trigger('click')
    await flushPromises()
    expect(auditCalls().at(-1)).toContain('offset=20')
  })

  it('walks pages forward one by one', async () => {
    const view = await mountView()
    for (const expected of ['offset=20', 'offset=40']) {
      await view.find('.el-pagination .btn-next').trigger('click')
      await flushPromises()
      expect(calls[calls.length - 1]).toContain(expected)
    }
  })

  it('clamps back to page 1 when the result set shrinks after a refresh', async () => {
    const view = await mountView()
    const page3 = view
      .findAll('.el-pager li.number')
      .find((li) => li.text() === '3')
    await page3!.trigger('click')
    await flushPromises()
    expect(calls[calls.length - 1]).toContain('offset=40')

    // the dataset shrinks to a single page → the pager must not stay on page 3
    currentTotal = 12
    await view.find('.refresh-btn').trigger('click')
    await flushPromises()
    expect(calls[calls.length - 1]).toContain('offset=0')
  })
})

describe('AuditView page header (P6 §2.3)', () => {
  it('renders PageHeader with the root crumb, aria-current and document.title', async () => {
    const view = await mountView()
    expect(view.find('h1').text()).toBe('Audit log')
    const crumbs = view.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Audit log'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(document.title).toBe('Audit log')
  })

  it('states each stat card caliber (§7: filtered total vs current page)', async () => {
    const view = await mountView()
    // 只断言审计表的三张卡(第一个 stat-grid);拒绝报表卡有自己的一组
    const subs = view
      .findAll('.stat-grid')[0]
      .findAll('.stat-sub')
      .map((s) => s.text())
    // total is the server-side count under the filters; ok/err only split
    // the visible page, so the two calibers must not look identical.
    expect(subs).toEqual(['matching filters', 'this page', 'this page'])
  })
})

describe('AuditView URL state (§4.3)', () => {
  it('honours a deep link: user_id / action / page all reach the API', async () => {
    await mountView('?user_id=3&action=auth.login&page=2')
    const first = calls[0]
    expect(first).toContain('user_id=3')
    expect(first).toContain('action=auth.login')
    expect(first).toContain('offset=20')
  })

  it('writes the filters and the page back into the URL', async () => {
    const view = await mountView()
    const inputs = view.findAll('.audit-filter-input input')
    expect(inputs.length).toBe(2)
    await inputs[0].setValue('7')
    await inputs[1].setValue('admin.user.create')
    await flushPromises()
    expect(router.currentRoute.value.query.user_id).toBe('7')
    expect(router.currentRoute.value.query.action).toBe('admin.user.create')
    expect(calls[calls.length - 1]).toContain('user_id=7')
    expect(calls[calls.length - 1]).toContain('action=admin.user.create')

    const page2 = view.findAll('.el-pager li.number').find((li) => li.text() === '2')
    await page2!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.page).toBe('2')
  })

  it('drops the keys back out of the URL when a filter is cleared', async () => {
    await mountView('?action=auth.login')
    expect(router.currentRoute.value.query.action).toBe('auth.login')
    const actionInput = wrapper!.findAll('.audit-filter-input input')[1]
    await actionInput.setValue('')
    await flushPromises()
    expect(router.currentRoute.value.query.action).toBeUndefined()
  })
})

describe('AuditView refusal report (A5)', () => {
  it('renders counts, question ranking and the coverage note', async () => {
    refusalData = {
      available: true,
      total: 9,
      capped: false,
      coverage_note: '覆盖范围:……不含:直接编译调用。',
      coverage_note_en: 'Covers the conversation path. Not covered: direct compile calls.',
      by_reason: [
        { reason: 'uncovered', count: 6 },
        { reason: 'no_model', count: 3 },
      ],
      by_miss: [{ reason: 'unknown_cardinality', count: 2 }],
      top_questions: [
        { question: '各分行存款余额', count: 4, reasons: ['uncovered'] },
        { question: '哪个地区贷款金额最高?', count: 2, reasons: ['no_model'] },
      ],
      relationship_missing: {
        reasons: ['unknown_cardinality', 'fan_out'],
        count: 2,
        top_questions: [{ question: '各分行存款余额', count: 2 }],
      },
    }
    const view = await mountView()
    const card = view.find('.refusal-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('Refusals')
    expect(card.text()).toContain('9')
    expect(card.text()).toContain('各分行存款余额')
    expect(card.text()).toContain('uncovered · 6')
    expect(card.text()).toContain('unknown_cardinality · 2')
    // 覆盖面自述按语言取对应字段(en)
    expect(card.text()).toContain('Not covered: direct compile calls.')
    // 请求带上窗口参数
    expect(calls.some((c) => c.includes('refusal-report?days=30'))).toBe(true)
  })

  it('空数据显式「无数据」而不是空白', async () => {
    const view = await mountView()
    const card = view.find('.refusal-card')
    expect(card.findAll('.refusal-col p.cell-muted').length).toBe(2)
    expect(card.text()).toContain('No data in window')
  })

  it('degraded: 读不到审计库 → 显式不可用,绝不铺零', async () => {
    refusalData = {
      available: false,
      total: null,
      note: 'audit read failed: RuntimeError',
      coverage_note: '覆盖范围……',
      coverage_note_en: 'covers…',
      relationship_missing: { count: null },
    }
    const view = await mountView()
    const card = view.find('.refusal-card')
    expect(card.find('.refusal-degraded').exists()).toBe(true)
    expect(card.text()).toContain('report unavailable')
    expect(card.text()).toContain('audit read failed: RuntimeError')
    // 一张数字卡都不渲染(0 是编出来的结论)
    expect(card.find('.refusal-stats').exists()).toBe(false)
  })
})
