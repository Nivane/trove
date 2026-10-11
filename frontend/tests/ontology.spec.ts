/**
 * OntologyView — 管理台「本体」页（E6/O2，前端方案 §3/§8）。
 *
 * 对着页面真正消费的服务器契约做端到端：GET /v1/admin/datasources 与
 * GET /v1/admin/semantic/{ds}/object-graph，配真 router（?ds= 走 URL）。
 * ECharts 挂模块级 mock（jsdom 无画布）：断言「喂给 setOption 的 option」
 * 而不是像素 —— 图规格本身的判据在 tests/ontology-chart.spec.ts。
 *
 * 钉住的页面行为：
 *  · KPI 全部纯派生自投影（对象/链接/层级/声明覆盖率；层级键缺席 → 0，不是错误）；
 *  · 三态纪律：零数据集 = 引导卡（有 CTA）；有对象零声明 = 图照常 + 弱化引导；
 *    缺 objects/links 键 = 错误卡 + 重试（绝不渲染假空图）；
 *  · 点选 → 侧栏对象/链接详情 + 深链回语义台（页内无第二编辑面）；
 *  · 切数据源 = 换一张图（重新取数 + 选中态清空）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { nextTick } from 'vue'

const { initMock, instances } = vi.hoisted(() => {
  const instances: {
    options: Record<string, unknown>[]
    handlers: Record<string, (p: unknown) => void>
    setOption: ReturnType<typeof vi.fn>
    on: ReturnType<typeof vi.fn>
    dispose: ReturnType<typeof vi.fn>
    resize: ReturnType<typeof vi.fn>
    dispatchAction: ReturnType<typeof vi.fn>
    getDom: () => HTMLElement
  }[] = []
  const initMock = vi.fn((el: HTMLElement) => {
    const inst = {
      options: [] as Record<string, unknown>[],
      handlers: {} as Record<string, (p: unknown) => void>,
      setOption: vi.fn((opt: Record<string, unknown>) => {
        inst.options.push(opt)
      }),
      on: vi.fn((ev: string, cb: (p: unknown) => void) => {
        inst.handlers[ev] = cb
      }),
      dispose: vi.fn(),
      resize: vi.fn(),
      dispatchAction: vi.fn(),
      getDom: () => el,
    }
    instances.push(inst)
    return inst
  })
  return { initMock, instances }
})

vi.mock('echarts', () => ({ init: initMock }))

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

import { apiGet } from '../src/api/http'
import OntologyView from '../src/views/admin/OntologyView.vue'

const DESTS = [
  { name: 'demo', type: 'sqlite', default: true, status: 'connected' },
  { name: 'sales', type: 'mysql', status: 'connected' },
  { name: 'cold', type: 'mysql', status: 'disconnected' },
]

/** 投影 DTO fixture（design §4.5）：3 对象 / 2 链接 / 无 hierarchies 键。 */
const GRAPH = {
  objects: [
    {
      dataset: 'loan',
      display_name: '贷款',
      declared: true,
      natural_keys: ['loan_id'],
      aliases: ['借据'],
      metrics: ['total_loan_amount', 'avg_loan_amount'],
      field_count: 8,
    },
    {
      dataset: 'account',
      display_name: '账户',
      declared: true,
      natural_keys: ['account_id'],
      aliases: [],
      metrics: ['total_salary'],
      field_count: 5,
    },
    { dataset: 'district', declared: false, natural_keys: [], aliases: [], metrics: [], field_count: 4 },
  ],
  links: [
    {
      name: 'loan_to_account',
      from: 'loan',
      to: 'account',
      from_columns: ['account_id'],
      to_columns: ['account_id'],
      cardinality: '1:N',
      fan_out: false,
      display_name: '发放',
    },
    {
      name: 'account_to_district',
      from: 'account',
      to: 'district',
      from_columns: ['district_id'],
      to_columns: ['district_id'],
      cardinality: 'M:N',
      fan_out: true,
    },
  ],
}

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

/** 每个测试自行改的两个开关。 */
let graphBody: unknown
let graphError: Error | null

function installApi() {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path === '/v1/admin/datasources') return { datasources: clone(DESTS) }
    if (/^\/v1\/admin\/semantic\/[^/]+\/object-graph$/.test(path)) {
      if (graphError) throw graphError
      return clone(graphBody)
    }
    return {}
  })
}

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountView(url = '/admin/ontology?ds=demo') {
  wrapper?.unmount()
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/semantic', name: 'admin-semantic', component: { render: () => null } },
      { path: '/admin/ontology', name: 'admin-ontology', component: { render: () => null } },
    ],
  })
  await router.push(url)
  await router.isReady()
  wrapper = mount(OntologyView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

/** 最近一次 init 出来的图表实例（图上屏过才存在）。 */
function chartInst() {
  return instances[instances.length - 1]
}

function lastOption() {
  const inst = chartInst()
  return inst.options[inst.options.length - 1]
}

function graphNodes(): { name: string; label: { formatter: string }; itemStyle: Record<string, unknown> }[] {
  const series = (lastOption().series as { data: never[] }[])[0]
  return series.data
}

function graphLinks(): {
  source: string
  label: { formatter: string }
  lineStyle: { type: string; color: string }
}[] {
  const series = (lastOption().series as { links: never[] }[])[0]
  return series.links
}

function clickChart(payload: unknown) {
  chartInst().handlers.click(payload)
}

function kpiValues(view: VueWrapper): string[] {
  return view.findAll('.kpi-tile .kpi-value').map((el) => el.text())
}

function findButton(root: ParentNode, text: string): HTMLButtonElement {
  const el = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) =>
    (b.textContent ?? '').trim().includes(text),
  )
  if (!el) throw new Error(`button not found: ${text}`)
  return el
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  instances.length = 0
  initMock.mockClear()
  graphBody = clone(GRAPH)
  graphError = null
  installApi()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('OntologyView — 渲染与 KPI', () => {
  it('KPI 纯派生（对象/链接/层级缺席→0/声明覆盖率）；图拿到的 option 就是投影', async () => {
    const view = await mountView()
    expect(kpiValues(view)).toEqual(['3', '2', '0', '67%'])
    // 图渲染：节点/边逐条进 option。
    expect(graphNodes().map((n) => n.name)).toEqual(['loan', 'account', 'district'])
    expect(graphLinks().map((l) => l.source)).toEqual(['loan', 'account'])
    // 声明/未声明样式（token 在 jsdom 走缺省 accent）。
    const loan = graphNodes().find((n) => n.name === 'loan')!
    const district = graphNodes().find((n) => n.name === 'district')!
    expect(loan.itemStyle.borderType).toBe('solid')
    expect(district.itemStyle.borderType).toBe('dashed')
    expect(district.label.formatter).toBe('district')
    // 未声明但存在 ≠ 空态：引导卡不出现，图与侧栏在位。
    expect(view.find('.state-panel').exists()).toBe(false)
    expect(view.find('.onto-panel').exists()).toBe(true)
  })

  it('规模 > 60：出提示条（不聚合，节点一个不少）', async () => {
    graphBody = {
      objects: Array.from({ length: 61 }, (_, i) => ({
        dataset: `t${i}`,
        declared: false,
      })),
      links: [],
    }
    const view = await mountView()
    expect(graphNodes()).toHaveLength(61)
    expect(view.find('.onto-scale-hint').exists()).toBe(true)
    // 60 个及以下不出提示条。
    graphBody = {
      objects: Array.from({ length: 60 }, (_, i) => ({ dataset: `t${i}`, declared: false })),
      links: [],
    }
    const view2 = await mountView()
    expect(view2.find('.onto-scale-hint').exists()).toBe(false)
  })

  it('有对象零声明：图照常渲染 + 弱化引导卡（不是空态）', async () => {
    graphBody = {
      objects: [{ dataset: 'loan', declared: false }, { dataset: 'account', declared: false }],
      links: [],
    }
    const view = await mountView()
    expect(view.find('.onto-guide.is-weak').exists()).toBe(true)
    expect(graphNodes()).toHaveLength(2)
    expect(view.find('.state-panel').exists()).toBe(false)
    expect(kpiValues(view)).toEqual(['2', '0', '0', '0%'])
  })

  it('点选对象：侧栏给声明详情 + 深链回语义台（页内无第二编辑面）', async () => {
    const view = await mountView()
    expect(view.find('.onto-panel').text()).toContain('点击图中对象或链接查看声明详情')
    clickChart({ data: { onto: { kind: 'object', ref: 'loan' } } })
    await nextTick()
    const panel = view.find('.onto-panel')
    expect(panel.text()).toContain('贷款')
    expect(panel.text()).toContain('已声明')
    expect(panel.text()).toContain('loan_id')
    expect(panel.text()).toContain('借据')
    expect(panel.text()).toContain('2') // 度量数
    expect(panel.text()).toContain('8') // 字段数
    const link = panel.find('a')
    expect(link.attributes('href')).toBe('/admin/semantic?ds=demo&tab=datasets&q=loan')
  })

  it('点选链接：方向/连接列/基数 + fan_out 警示 + 关系 tab 深链', async () => {
    const view = await mountView()
    clickChart({ data: { onto: { kind: 'link', ref: 'account_to_district' } } })
    await nextTick()
    const panel = view.find('.onto-panel')
    expect(panel.text()).toContain('account_to_district')
    expect(panel.text()).toContain('账户 → district') // 方向（标签回退数据集名）
    expect(panel.text()).toContain('district_id')
    expect(panel.text()).toContain('M:N')
    expect(panel.find('.onto-fanout').exists()).toBe(true)
    expect(panel.find('a').attributes('href')).toBe('/admin/semantic?ds=demo&tab=relationships')

    // 无 fan_out 的链接不挂警示。
    clickChart({ data: { onto: { kind: 'link', ref: 'loan_to_account' } } })
    await nextTick()
    expect(view.find('.onto-panel .onto-fanout').exists()).toBe(false)
    expect(view.find('.onto-panel').text()).toContain('1:N')
  })
})

describe('OntologyView — 空态 / 错误态 / 数据源切换', () => {
  it('零数据集：引导卡 + CTA 跳语义台（合法空态，不是错误）', async () => {
    graphBody = { objects: [], links: [] }
    const view = await mountView()
    expect(view.find('.state-panel.is-empty').exists()).toBe(true)
    expect(view.find('.onto-main').exists()).toBe(false)
    const cta = findButton(view.element, '去语义台声明')
    cta.click()
    await flushPromises()
    await nextTick()
    expect(router.currentRoute.value.name).toBe('admin-semantic')
    expect(router.currentRoute.value.query.ds).toBe('demo')
  })

  it('形状异常（缺 links 键）：错误卡 + 重试 —— 绝不渲染假空图', async () => {
    graphBody = { objects: [{ dataset: 'loan', declared: true }] }
    const view = await mountView()
    const panel = view.find('.state-panel.is-error')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('本体视图加载失败')
    expect(panel.text()).toContain('shape: objects/links missing') // 诊断原文照展
    expect(instances).toHaveLength(0) // 图从未被渲染
    expect(view.find('.onto-main').exists()).toBe(false)

    // 重试：服务器恢复 → 图上线，错误卡退场。
    graphBody = clone(GRAPH)
    findButton(view.element, '重试').click()
    await flushPromises()
    await flushPromises()
    expect(view.find('.state-panel.is-error').exists()).toBe(false)
    expect(graphNodes()).toHaveLength(3)
  })

  it('网络失败：错误卡带原始 detail（读不到 ≠ 空）', async () => {
    graphError = new Error('500 internal error')
    const view = await mountView()
    const panel = view.find('.state-panel.is-error')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('500 internal error')
  })

  it('无 ?ds= 深链：默认源自动落位并按它取图（URL 纪律与语义台同款）', async () => {
    const view = await mountView('/admin/ontology')
    expect((apiGet as any).mock.calls.map((c: unknown[]) => c[0])).toContain(
      '/v1/admin/semantic/demo/object-graph',
    )
    expect(router.currentRoute.value.query.ds).toBe('demo')
    expect(kpiValues(view)).toEqual(['3', '2', '0', '67%'])
  })

  it('切数据源：按新源重新取数 + 选中态清空（换的是另一张图）', async () => {
    const view = await mountView()
    clickChart({ data: { onto: { kind: 'object', ref: 'loan' } } })
    await nextTick()
    expect(view.find('.onto-panel').text()).toContain('贷款')

    const salesBtn = findButton(view.element, 'sales')
    salesBtn.click()
    await flushPromises()
    await flushPromises()
    expect((apiGet as any).mock.calls.map((c: unknown[]) => c[0])).toContain(
      '/v1/admin/semantic/sales/object-graph',
    )
    expect(router.currentRoute.value.query.ds).toBe('sales')
    // 上一张图的选中残值不渲染。
    expect(view.find('.onto-panel').text()).toContain('点击图中对象或链接查看声明详情')
  })
})
