/**
 * SemanticView · 本体形态(O1)—— 数据集抽屉的「本体实体」节 + bp_* 关系预填。
 *
 * 与 semantic.spec.ts 同款:对着页面真正消费的服务端契约打桩(GET
 * /v1/admin/semantic/{ds} · /history · /admin/audit,POST /validate ·
 * /drafts),断言的是页面自己的行为:
 *
 *  · 实体节只在数据集抽屉里出现;已声明的回填、未声明的空白(节本身不因
 *    未声明而消失 —— 「没声明」与「不能声明」不是一回事);
 *  · 保存走**既有** draft 通道(kind=entity,name=数据集名),先 validate
 *    后 create;空字段不写进 payload(缺席 = 没声明,不是空串);
 *  · 待审草稿以「本体实体」名词进 pending 列表,并在抽屉里给出草稿态徽标
 *    与冲突横幅(复用既有 sem-bad-draft);
 *  · 拒绝卡的 bp_* 深链预填关系对话框(两端 + 列对;列对可空),消费即清
 *    URL,坏键不炸页面。
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

import { apiGet, apiPost } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'

const DESTS = [{ name: 'demo', type: 'sqlite', default: true, status: 'connected' }]

const DATASETS = [
  {
    name: 'loan',
    source: 'loan',
    primary_key: ['loan_id'],
    description: 'One row per loan',
    // 已声明实体(设计 §4.1 形状)
    entity: {
      dataset: 'loan',
      display_name: 'Loan',
      natural_keys: ['loan_id'],
      aliases: ['credit'],
    },
    fields: [],
  },
  {
    name: 'account',
    source: 'account',
    primary_key: ['account_id'],
    description: '',
    // 未声明:键整个缺席(不是空实体)
    fields: [],
  },
]

const OK_VALIDATE = {
  ok: true,
  errors: [],
  warnings: [],
  normalized: { expression: '' },
}

let DETAIL: Record<string, unknown>
let PENDING: Record<string, unknown>[]

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

function resetFixtures() {
  PENDING = []
  DETAIL = {
    enabled: true,
    model: { name: 'demo_model', metrics: [], datasets: DATASETS, relationships: [] },
    issues: [],
    issue_items: [],
    drafts: { pending: PENDING, applied: [], rejected: [] },
    drift: { status: 'ok', checked_at: '2026-10-01T10:00:00Z', items: [] },
  }
}

function mockApi() {
  // 测试体先摆 PENDING 再 mockApi:这里现读,不赌 resetFixtures 时抓到的引用
  ;(DETAIL.drafts as { pending: Record<string, unknown>[] }).pending = PENDING
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path === '/v1/admin/datasources') return { datasources: clone(DESTS) }
    if (/^\/v1\/admin\/semantic\/[^/]+$/.test(path)) return { semantic: clone(DETAIL) }
    if (path.includes('/history')) return { history: [] }
    if (path.startsWith('/v1/admin/audit')) return { audit: [], total: 0 }
    return {}
  })
  ;(apiPost as any).mockImplementation(async (path: string) => {
    if (path.endsWith('/validate')) return clone(OK_VALIDATE)
    if (path.endsWith('/drafts')) return { draft: { id: 'new' } }
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

function findButton(root: ParentNode, text: string): HTMLButtonElement {
  const el = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) =>
    (b.textContent ?? '').trim().includes(text),
  )
  if (!el) throw new Error(`button not found: ${text}`)
  return el
}

/** 实体节的三只输入框(显示名 / 自然键 / 别名,按模板顺序)。 */
function entityInputs(): HTMLInputElement[] {
  const form = drawer().querySelector('.sem-entity-form')
  if (!form) throw new Error('entity section missing')
  return Array.from(form.querySelectorAll<HTMLInputElement>('input'))
}

/** 关系对话框(打开过才会在 DOM 里)。 */
function relationDialog(): HTMLElement {
  const dialog = Array.from(document.body.querySelectorAll<HTMLElement>('.el-dialog')).find(
    (d) => d.textContent?.includes('New relationship'),
  )
  if (!dialog) throw new Error('relationship dialog not open')
  return dialog
}

/** 对话框里非 select 的输入框:[name, from_columns, to_columns, display_name, note] */
function relationInputs(): HTMLInputElement[] {
  return Array.from(relationDialog().querySelectorAll<HTMLInputElement>('input')).filter(
    (i) => !i.closest('.el-select'),
  )
}

async function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

function calls(path: string): Record<string, any>[] {
  return (apiPost as any).mock.calls
    .filter((c: unknown[]) => c[0] === path)
    .map((c: unknown[]) => (c[1] ?? {}) as Record<string, any>)
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

describe('SemanticView · 本体实体节(O1)', () => {
  it('declared entity pre-fills the three fields', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')

    await rowByText(view, 'loan').trigger('click')
    await settle()

    const text = drawer().textContent ?? ''
    expect(text).toContain('Ontology entity')
    // 没有待审草稿 → 没有徽标(徽标说的是「有草稿在等」,不是「有这个节」)
    const section = drawer().querySelector('.sem-entity-form')!.closest('section')!
    expect(section.textContent).not.toContain('Pending')

    const inputs = entityInputs()
    expect(inputs.map((i) => i.value)).toEqual(['Loan', 'loan_id', 'credit'])
  })

  it('undeclared entity renders the section empty (missing ≠ forbidden)', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')

    await rowByText(view, 'account').trigger('click')
    await settle()

    expect(drawer().textContent).toContain('Ontology entity')
    expect(entityInputs().map((i) => i.value)).toEqual(['', '', ''])
  })

  it('editing creates a draft through the existing channel (validate → create)', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')

    await rowByText(view, 'loan').trigger('click')
    await settle()

    const inputs = entityInputs()
    await type(inputs[0], '贷款')
    await type(inputs[1], 'loan_id, branch_id')
    await type(inputs[2], '借据, credit')

    findButton(drawer(), 'Create draft').click()
    await settle()

    const payload = {
      dataset: 'loan',
      display_name: '贷款',
      natural_keys: ['loan_id', 'branch_id'],
      aliases: ['借据', 'credit'],
    }
    // 先干跑校验、后落草稿,同一个 payload(确认门不变:落的是 pending)
    expect(calls('/v1/admin/semantic/demo/validate')[0]).toMatchObject({
      kind: 'entity',
      action: 'upsert',
      name: 'loan',
      payload,
    })
    expect(calls('/v1/admin/semantic/demo/drafts')[0]).toMatchObject({
      kind: 'entity',
      action: 'upsert',
      name: 'loan',
      payload,
    })
  })

  it('blank fields are omitted from the payload (absent, not empty string)', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')

    await rowByText(view, 'account').trigger('click')
    await settle()

    await type(entityInputs()[0], '账户')
    findButton(drawer(), 'Create draft').click()
    await settle()

    const { payload } = calls('/v1/admin/semantic/demo/drafts')[0]
    expect(payload).toEqual({ dataset: 'account', display_name: '账户' })
    expect(payload).not.toHaveProperty('natural_keys')
    expect(payload).not.toHaveProperty('aliases')
  })

  it('pending list renders the entity draft kind with the shared row style', async () => {
    PENDING = [
      {
        id: 'e1',
        kind: 'entity',
        action: 'upsert',
        name: 'loan',
        note: 'declare the loan entity',
        created_at: '2026-10-02T10:00:00Z',
        payload: { dataset: 'loan', display_name: 'Loan' },
      },
    ]
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=pending')

    const row = rowByText(view, 'loan')
    // 名词是「本体实体」——不是机制名 entity(与既有 kind 同款行)
    expect(row.text()).toContain('Ontology entity')
    expect(row.find('.pill').exists()).toBe(true)
  })

  it('the drawer badges a pending entity draft and reuses the conflict banner', async () => {
    PENDING = [
      {
        id: 'e2',
        kind: 'entity',
        action: 'upsert',
        name: 'loan',
        created_at: '2026-10-02T10:00:00Z',
        payload: { dataset: 'loan' },
        conflict: { code: 'name_declared', message: '实体「Loan」已声明' },
      },
    ]
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')

    await rowByText(view, 'loan').trigger('click')
    await settle()

    const text = drawer().textContent ?? ''
    expect(text).toContain('Pending')
    // 冲突横幅复用既有组件:服务端诊断逐字展示 + 既有措辞
    expect(text).toContain('Conflict')
    expect(text).toContain('实体「Loan」已声明')
  })
})

describe('SemanticView · bp_* 关系预填(O1)', () => {
  it('four keys prefill the relationship dialog (ends + column pairs)', async () => {
    mockApi()
    await mountView(
      '/admin/semantic?ds=demo&bp_draft_kind=relationship' +
        '&bp_from_dataset=loan&bp_to_dataset=account' +
        '&bp_from_columns=loan_id&bp_to_columns=account_id' +
        '&bp_conflict_message=关系「loan_account」未声明',
    )
    await settle()

    // 打开即预填:名字默认取确定性 slug(可改),列对逐列摆好
    const inputs = relationInputs()
    expect(inputs[0].value).toBe('loan_account')
    expect(inputs[1].value).toBe('loan_id')
    expect(inputs[2].value).toBe('account_id')
    // 两端进 select(label 可见)
    expect(relationDialog().textContent).toContain('loan')
    expect(relationDialog().textContent).toContain('account')
    // 拒绝轮诊断走既有的蓝本注记行
    expect(relationDialog().textContent).toContain('Prefilled from the draft')

    // 提交的就是两端 + 列对(reason 只进注记,不伪造列对)
    findButton(relationDialog(), 'Create draft').click()
    await settle()
    expect(calls('/v1/admin/semantic/demo/drafts')[0]).toMatchObject({
      kind: 'relationship',
      action: 'upsert',
      name: 'loan_account',
      payload: { from: 'loan', to: 'account', from_columns: ['loan_id'], to_columns: ['account_id'] },
    })
  })

  it('an empty column pair stays empty (the backend only gives what is certain)', async () => {
    mockApi()
    await mountView(
      '/admin/semantic?ds=demo&bp_draft_kind=relationship' +
        '&bp_from_dataset=loan&bp_to_dataset=account',
    )
    await settle()

    const inputs = relationInputs()
    expect(inputs[1].value).toBe('')
    expect(inputs[2].value).toBe('')

    findButton(relationDialog(), 'Create draft').click()
    await settle()
    const { payload } = calls('/v1/admin/semantic/demo/drafts')[0]
    expect(payload).toEqual({ from: 'loan', to: 'account' })
    expect(payload).not.toHaveProperty('from_columns')
    expect(payload).not.toHaveProperty('to_columns')
  })

  it('consumption is one-time: the URL is cleared, the prefill survives', async () => {
    mockApi()
    await mountView(
      '/admin/semantic?ds=demo&bp_draft_kind=relationship' +
        '&bp_from_dataset=loan&bp_to_dataset=account',
    )
    await settle()

    const left = Object.keys(router.currentRoute.value.query).filter((k) => k.startsWith('bp_'))
    expect(left).toEqual([])
    // 清键只清 URL,不清已经预填到对话框里的内容(刷新/后退不会重放半消费状态)
    expect(router.currentRoute.value.query.ds).toBe('demo')
    expect(relationInputs()[0].value).toBe('loan_account')
  })

  it('unknown or incomplete blueprints are ignored without breaking the page', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&bp_draft_kind=widget&bp_from_dataset=x')
    await settle()

    // 未知 kind:不预填也不弹错,页面照常(参数照样被清)
    expect(view.find('.sem-page').exists()).toBe(true)
    expect(document.body.querySelector('.el-dialog')).toBeNull()
    expect(
      Object.keys(router.currentRoute.value.query).filter((k) => k.startsWith('bp_')),
    ).toEqual([])

    // kind 认识但端点缺失:对话框开、空着、提交被禁用(没有 from/to 的「关系」不是关系)
    await mountView('/admin/semantic?ds=demo&bp_draft_kind=relationship&bp_junk=whatever')
    await settle()
    const inputs = relationInputs()
    expect(inputs[0].value).toBe('')
    expect(findButton(relationDialog(), 'Create draft').disabled).toBe(true)
  })
})
