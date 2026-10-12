/**
 * OntologyView — 管理台「本体」页（E6/O2；2026-10-12 确定性结构图改版）。
 *
 * 对着页面真正消费的服务器契约做端到端：GET /v1/admin/datasources 与
 * GET /v1/admin/semantic/{ds}/object-graph，配真 router（?ds= 走 URL）。
 * 图是**真 SVG**（不再是 ECharts）：断言 DOM —— 节点/边/标签/身份砖、
 * 点击与键盘选中、点空白/Esc 清空。布局与身份的判据在
 * tests/ontology-layout.spec.ts 与 tests/ontology-identity.spec.ts。
 *
 * 钉住的页面行为：
 *  · KPI 全部纯派生自投影（对象/链接/层级/声明覆盖率；层级键缺席 → 0，不是错误）；
 *  · 三态纪律：零数据集 = 引导卡（有 CTA）；有对象零声明 = 图照常 + 弱化引导；
 *    缺 objects/links 键 = 错误卡 + 重试（绝不渲染假空图）；
 *  · 点选 → 侧栏对象/链接详情 + 深链回语义台（页内无第二编辑面）；
 *  · 清空出口唯一：点空白 / Esc 走同一条（图与侧栏一起回到未选态）；
 *  · 切数据源 = 换一张图（重新取数 + 选中态清空）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { nextTick } from 'vue'

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

function nodeIds(view: VueWrapper): string[] {
  return view.findAll('.node-g').map((g) => g.attributes('data-dataset') ?? '')
}

/** 点选一个对象（真 DOM 事件，走 @click.stop）。 */
async function clickNode(view: VueWrapper, dataset: string) {
  await view.find(`.node-g[data-dataset="${dataset}"]`).trigger('click')
  await nextTick()
}

/** 点选一条链接。 */
async function clickLink(view: VueWrapper, name: string) {
  await view.find(`.edge-g[data-link="${name}"]`).trigger('click')
  await nextTick()
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
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
  it('KPI 纯派生（对象/链接/层级缺席→0/声明覆盖率）；图按投影逐条画出来', async () => {
    const view = await mountView()
    expect(kpiValues(view)).toEqual(['3', '2', '0', '67%'])

    // 节点：声明顺序保留，位置由结构决定（x 随层号递增）。
    expect(nodeIds(view)).toEqual(['loan', 'account', 'district'])
    const xs = view.findAll('.node-g').map((g) => Number(/translate\(([-\d.]+),/.exec(g.attributes('transform') ?? '')?.[1]))
    expect(xs[0]).toBeLessThan(xs[1])
    expect(xs[1]).toBeLessThan(xs[2])

    // 边：逐条画出，扇出那条带警示类。
    expect(view.findAll('.edge-g').map((g) => g.attributes('data-link'))).toEqual([
      'loan_to_account',
      'account_to_district',
    ])
    expect(view.find('.edge-g[data-link="account_to_district"]').classes()).toContain('is-fanout')
    expect(view.find('.edge-g[data-link="loan_to_account"]').classes()).not.toContain('is-fanout')

    // 标签：声明了名字 → 「名字 · 基数」；未声明 → 只标基数（连接串不进图）。
    const labels = view.findAll('.elab text').map((t) => t.text())
    expect(labels).toContain('发放 · 1:N')
    expect(labels).toContain('M:N')
    expect(labels.some((l) => l.includes('district_id'))).toBe(false)

    // 卡片文字：显示名 / 自然键 / 度量·字段；未声明用数据集名。
    const loan = view.find('.node-g[data-dataset="loan"]')
    expect(loan.find('.nname').text()).toBe('贷款')
    expect(loan.find('.nsub').text()).toContain('loan_id')
    expect(loan.find('.nmeta').text()).toContain('2')
    expect(view.find('.node-g[data-dataset="district"] .nname').text()).toBe('district')

    // 未声明但存在 ≠ 空态：引导卡不出现，图与侧栏在位。
    expect(view.find('.state-panel').exists()).toBe(false)
    expect(view.find('.onto-panel').exists()).toBe(true)
  })

  it('身份层：声明对象拿身份色，未声明保持灰「表」（声明即物化）', async () => {
    const view = await mountView()
    const loanTile = view.find('.node-g[data-dataset="loan"] .ntile rect')
    expect(loanTile.attributes('fill')).toBe('#7c3aed') // loan → money 身份色
    expect(loanTile.attributes('fill-opacity')).toBe('0.13')
    const districtTile = view.find('.node-g[data-dataset="district"] .ntile rect')
    expect(districtTile.attributes('fill')).toBe('#a1a1aa')
    expect(districtTile.attributes('fill-opacity')).toBe('0.35')
    expect(view.find('.node-g[data-dataset="district"]').classes()).toContain('is-undeclared')

    // 图例在位（已声明 / 未声明 / 箭头 / 扇出四条）。
    expect(view.findAll('.onto-lg')).toHaveLength(4)
  })

  it('规模 > 60：出提示条（不聚合，节点一个不少）', async () => {
    graphBody = {
      objects: Array.from({ length: 61 }, (_, i) => ({ dataset: `t${i}`, declared: false })),
      links: [],
    }
    const view = await mountView()
    expect(view.findAll('.node-g')).toHaveLength(61)
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
    expect(view.findAll('.node-g')).toHaveLength(2)
    expect(view.find('.state-panel').exists()).toBe(false)
    expect(kpiValues(view)).toEqual(['2', '0', '0', '0%'])
  })

  it('悬空链接（指向未声明数据集）：不画，但图上说得出来', async () => {
    graphBody = {
      objects: [{ dataset: 'loan', declared: true }],
      links: [
        {
          name: 'ghost',
          from: 'loan',
          to: 'nope',
          from_columns: ['x'],
          to_columns: ['y'],
          display_name: '幽灵链接',
        },
      ],
    }
    const view = await mountView()
    expect(view.findAll('.edge-g')).toHaveLength(0)
    expect(view.find('.onto-dangling').text()).toContain('1')
  })

  it('层级卡：缺席 → 空态如实说；有声明 → 层级链 + 数据集 chips', async () => {
    const view = await mountView()
    expect(view.find('.onto-hier-empty').exists()).toBe(true)

    graphBody = {
      ...clone(GRAPH),
      hierarchies: [
        { name: 'geo', display_name: '地区层级', dataset: 'district', levels: ['region', 'district'] },
        { name: 'bare', levels: [] },
      ],
    }
    const view2 = await mountView()
    expect(view2.find('.onto-hier-empty').exists()).toBe(false)
    const rows = view2.findAll('.onto-hier-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('地区层级')
    expect(rows[0].text()).toContain('region')
    expect(rows[0].text()).toContain('district')
    expect(rows[1].find('.onto-dash').text()).toBe('—')
    expect(kpiValues(view2)[2]).toBe('2')
  })
})

describe('OntologyView — 点选 / 清空', () => {
  it('点对象：侧栏给身份头 + 统计 + 自然键/别名 + 深链回语义台', async () => {
    const view = await mountView()
    expect(view.find('.onto-panel').text()).toContain('点击图中对象或链接查看声明详情')

    await clickNode(view, 'loan')
    const panel = view.find('.onto-panel')
    expect(panel.find('.onto-panel-title').text()).toBe('贷款')
    expect(panel.find('.onto-mono').text()).toBe('loan')
    expect(panel.text()).toContain('已声明')
    expect(panel.text()).toContain('类型身份 · 按名派生')
    expect(panel.text()).toContain('loan_id')
    expect(panel.text()).toContain('借据')
    // 统计三项：度量 / 字段 / 链接（loan 只有一条出链接）。
    expect(panel.findAll('.onto-stats b').map((b) => b.text())).toEqual(['2', '8', '1'])
    // 身份砖用的是同一份派生（loan → money 紫）。
    expect(panel.find('.onto-tile rect').attributes('fill')).toBe('#7c3aed')
    expect(panel.find('a').attributes('href')).toBe('/admin/semantic?ds=demo&tab=datasets&q=loan')
    // 图上同步进入强调态。
    expect(view.find('.node-g[data-dataset="loan"]').classes()).toContain('is-hot')
    expect(view.find('.node-g[data-dataset="district"]').classes()).toContain('is-dim')
  })

  it('对象的出/入链接行是真按钮：点一下切到那条链接的详情', async () => {
    const view = await mountView()
    await clickNode(view, 'account')
    const rows = view.findAll('.onto-lrow')
    expect(rows.length).toBe(2) // 出：account→district；入：loan→account
    // 未声明名字的关系行铺列连接串（等宽弱化），不是凭空编一个名字。
    expect(rows[0].find('.onto-lname').text()).toContain('district_id')
    expect(rows[0].find('.onto-lname').classes()).toContain('is-unnamed')
    expect(rows[1].find('.onto-lname').text()).toBe('发放')
    expect(rows[1].find('.onto-lname').classes()).not.toContain('is-unnamed')
    // 行尾是「另一端」的数据集名（出看 to、入看 from）。
    expect(rows[0].find('.onto-ltgt').text()).toBe('district')
    expect(rows[1].find('.onto-ltgt').text()).toBe('loan')
    await rows[1].trigger('click')
    await nextTick()
    expect(view.find('.onto-panel .onto-mono').text()).toBe('loan_to_account')
  })

  it('点链接：方向（两端身份砖）/连接列/基数 + fan_out 警示 + 关系 tab 深链', async () => {
    const view = await mountView()
    await clickLink(view, 'account_to_district')
    const panel = view.find('.onto-panel')
    expect(panel.find('.onto-mono').text()).toBe('account_to_district')
    // 未声明名字：标题回退列连接串（面板标题给足宽度，不按行内 150px 截）。
    expect(panel.find('.onto-panel-title').text()).toBe('district_id → district_id')
    expect(panel.findAll('.onto-dir .onto-mono2').map((e) => e.text())).toEqual([
      'account',
      'district',
    ])
    // 连接列那一段（.onto-join）：.onto-mono2 在方向行里也用（两端名），必须限定。
    expect(panel.find('.onto-join').text()).toBe('district_id → district_id')
    expect(panel.text()).toContain('M:N')
    expect(panel.find('.onto-fanout').exists()).toBe(true)
    expect(panel.findAll('.onto-dir .onto-tile')).toHaveLength(2)
    expect(panel.find('a').attributes('href')).toBe('/admin/semantic?ds=demo&tab=relationships')
    // 图上那条边 hot，其余 dim。
    expect(view.find('.edge-g[data-link="account_to_district"]').classes()).toContain('is-hot')
    expect(view.find('.edge-g[data-link="loan_to_account"]').classes()).toContain('is-dim')

    // 声明了名字的关系：标题就是名字；无 fan_out 不挂警示。
    await clickLink(view, 'loan_to_account')
    const p2 = view.find('.onto-panel')
    expect(p2.find('.onto-panel-title').text()).toBe('发放')
    expect(p2.find('.onto-fanout').exists()).toBe(false)
    expect(p2.text()).toContain('无')
  })

  it('键盘可达：卡片可聚焦、Enter 选中；Esc / 点空白清空（图与侧栏同步）', async () => {
    const view = await mountView()
    const node = view.find('.node-g[data-dataset="loan"]')
    expect(node.attributes('tabindex')).toBe('0')
    await node.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(view.find('.onto-panel-title').text()).toBe('贷款')

    // Esc：回到未选态（图示与侧栏一起回）。
    await view.find('.onto-svg').trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(view.find('.onto-panel').text()).toContain('点击图中对象或链接查看声明详情')
    expect(view.find('.node-g[data-dataset="loan"]').classes()).not.toContain('is-hot')

    // 点空白（svg 本体，不是卡片/连线）：同一条出口。
    await node.trigger('keydown', { key: ' ' })
    await nextTick()
    expect(view.find('.onto-panel-title').text()).toBe('贷款')
    await view.find('.onto-svg').trigger('click')
    await nextTick()
    expect(view.find('.onto-panel').text()).toContain('点击图中对象或链接查看声明详情')
  })

  it('缩放只动 viewBox（只放大缩小，没有平移）：按钮禁用态与复位', async () => {
    const view = await mountView()
    const svg = view.find('.onto-svg')
    const base = svg.attributes('viewBox')
    const zoomIn = view.find('.onto-zoom button:nth-child(2)')
    const zoomOut = view.find('.onto-zoom button:nth-child(1)')
    const reset = view.find('.onto-zoom button:nth-child(3)')
    // 初始：缩小/复位不可用（没有「复位到更小」这回事）。
    expect(zoomOut.attributes('disabled')).toBeDefined()
    expect(reset.attributes('disabled')).toBeDefined()
    await zoomIn.trigger('click')
    expect(svg.attributes('viewBox')).not.toBe(base)
    expect(zoomOut.attributes('disabled')).toBeUndefined()
    await reset.trigger('click')
    expect(svg.attributes('viewBox')).toBe(base)
    expect(zoomOut.attributes('disabled')).toBeDefined()
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
    expect(view.find('.onto-svg').exists()).toBe(false) // 图从未被渲染
    expect(view.find('.onto-main').exists()).toBe(false)

    // 重试：服务器恢复 → 图上线，错误卡退场。
    graphBody = clone(GRAPH)
    findButton(view.element, '重试').click()
    await flushPromises()
    await flushPromises()
    expect(view.find('.state-panel.is-error').exists()).toBe(false)
    expect(view.findAll('.node-g')).toHaveLength(3)
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
    await clickNode(view, 'loan')
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
