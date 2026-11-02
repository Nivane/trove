/**
 * SemanticView · 本体形态(O1)—— 数据集抽屉的「本体实体」/「钻取层级」节
 * + bp_* 关系预填。
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
 *  · 层级节:已声明行可编辑(名称只读)/ 可删(走确认对话框),新建先校验
 *    后落 pending 草稿;pending-only 行可见但只读,已声明的编辑以最新草稿
 *    为基线 —— 同一条 draft → validate → pending → confirm 通道;
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
  {
    // 层级归属的数据集:级别字段要在它自己的 fields 里(声明形状)
    name: 'district',
    source: 'district',
    primary_key: ['district_id'],
    description: 'One row per district',
    fields: [
      { name: 'district_id', datatype: 'integer', role: 'identifier' },
      { name: 'A3', datatype: 'text', role: 'dimension' },
      { name: 'A2', datatype: 'text', role: 'dimension' },
    ],
  },
]

const HIERARCHIES = [
  {
    name: 'region_to_district',
    display_name: 'Region to District',
    dataset: 'district',
    levels: ['A3', 'A2'],
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
    model: {
      name: 'demo_model',
      metrics: [],
      datasets: DATASETS,
      relationships: [],
      hierarchies: HIERARCHIES,
    },
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

/** 钻取层级节(数据集抽屉里,独立于实体节)。 */
function hierarchySection(): HTMLElement {
  const section = drawer().querySelector<HTMLElement>('.sem-hier-block')
  if (!section) throw new Error('hierarchy section missing')
  return section
}

/** 层级表单的三只输入框(名称 / 显示名 / 级别,按模板顺序)。 */
function hierarchyInputs(): HTMLInputElement[] {
  const form = drawer().querySelector<HTMLElement>('.sem-hier-form')
  if (!form) throw new Error('hierarchy form missing')
  return Array.from(form.querySelectorAll<HTMLInputElement>('input'))
}

function hierarchyRow(text: string): HTMLElement {
  const row = Array.from(drawer().querySelectorAll<HTMLElement>('.sem-hier-row')).find((el) =>
    (el.textContent ?? '').includes(text),
  )
  if (!row) throw new Error(`hierarchy row not found: ${text}`)
  return row
}

/** 层级动作区里的按钮:抽屉里实体节也有一个 Create draft,必须收窄。 */
function hierarchyAction(text: string): HTMLButtonElement {
  const actions = hierarchySection().querySelector<HTMLElement>('.sem-hier-actions')
  if (!actions) throw new Error('hierarchy actions missing')
  return findButton(actions, text)
}

function confirmPanel(): HTMLElement {
  const panel = document.body.querySelector<HTMLElement>('.confirm-panel')
  if (!panel) throw new Error('confirm dialog not open')
  return panel
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

describe('SemanticView · 钻取层级声明(审计 #3 剩余)', () => {
  async function openDistrict() {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')
    await rowByText(view, 'district').trigger('click')
    await settle()
    return view
  }

  it('declared hierarchy renders; Edit pre-fills with the name locked', async () => {
    await openDistrict()

    const row = hierarchyRow('Region to District')
    expect(row.textContent).toContain('A3')
    expect(row.textContent).toContain('A2')

    findButton(row, 'Edit').click()
    await settle()

    const inputs = hierarchyInputs()
    expect(inputs.map((i) => i.value)).toEqual([
      'region_to_district',
      'Region to District',
      'A3, A2',
    ])
    // 层级名是草稿寻址键:编辑时只读(改名 = 删旧建新)
    expect(inputs[0].disabled).toBe(true)
  })

  it('create validates first, then lands a hierarchy draft', async () => {
    await openDistrict()

    hierarchyAction('New hierarchy').click()
    await settle()
    const inputs = hierarchyInputs()
    await type(inputs[0], 'district_geo')
    await type(inputs[1], 'District Geo')
    await type(inputs[2], 'A3, A2')

    hierarchyAction('Create draft').click()
    await settle()

    const payload = {
      dataset: 'district',
      display_name: 'District Geo',
      levels: ['A3', 'A2'],
    }
    // 先干跑校验、后落草稿,同一个 payload(确认门不变:落的是 pending)
    expect(calls('/v1/admin/semantic/demo/validate')[0]).toMatchObject({
      kind: 'hierarchy',
      action: 'upsert',
      name: 'district_geo',
      payload,
    })
    expect(calls('/v1/admin/semantic/demo/drafts')[0]).toMatchObject({
      kind: 'hierarchy',
      action: 'upsert',
      name: 'district_geo',
      payload,
    })
  })

  it('blank display name is omitted from the payload (absent, not empty string)', async () => {
    await openDistrict()

    hierarchyAction('New hierarchy').click()
    await settle()
    const inputs = hierarchyInputs()
    await type(inputs[0], 'district_geo')
    await type(inputs[2], 'A3, A2')
    hierarchyAction('Create draft').click()
    await settle()

    const { payload } = calls('/v1/admin/semantic/demo/drafts')[0]
    expect(payload).toEqual({ dataset: 'district', levels: ['A3', 'A2'] })
    expect(payload).not.toHaveProperty('display_name')
  })

  it('levels are required before Create draft is enabled', async () => {
    await openDistrict()

    hierarchyAction('New hierarchy').click()
    await settle()
    expect(hierarchyAction('Create draft').disabled).toBe(true)

    await type(hierarchyInputs()[0], 'district_geo')
    expect(hierarchyAction('Create draft').disabled).toBe(true)

    await type(hierarchyInputs()[2], 'A3, A2')
    expect(hierarchyAction('Create draft').disabled).toBe(false)
  })

  it('a declared name cannot be created again (guard, no POST)', async () => {
    await openDistrict()

    hierarchyAction('New hierarchy').click()
    await settle()
    const inputs = hierarchyInputs()
    await type(inputs[0], 'region_to_district')
    await type(inputs[2], 'A3, A2')

    expect(hierarchyAction('Create draft').disabled).toBe(true)
    expect(hierarchySection().textContent).toContain('already exists')
    hierarchyAction('Create draft').click()
    await settle()
    expect(calls('/v1/admin/semantic/demo/drafts')).toHaveLength(0)
  })

  it('pending-only drafts are visible and read-only; pending list names the kind', async () => {
    PENDING = [
      {
        id: 'h1',
        kind: 'hierarchy',
        action: 'upsert',
        name: 'district_geo',
        created_at: '2026-10-02T10:00:00Z',
        payload: { dataset: 'district', levels: ['A3', 'A2'] },
      },
    ]
    await openDistrict()

    const row = hierarchyRow('district_geo')
    expect(row.textContent).toContain('Pending')
    // 草稿态行只读:修正 / 撤销走待审列表(再存只会叠第二条草稿)
    expect(row.querySelector('button')).toBeNull()

    const view2 = await mountView('/admin/semantic?ds=demo&tab=pending')
    const pendingRow = rowByText(view2, 'district_geo')
    expect(pendingRow.text()).toContain('Drill hierarchies')
  })

  it('a pending upsert is the edit baseline (not the stale model value)', async () => {
    PENDING = [
      {
        id: 'h2',
        kind: 'hierarchy',
        action: 'upsert',
        name: 'region_to_district',
        created_at: '2026-10-02T10:00:00Z',
        payload: { dataset: 'district', levels: ['A2'] },
      },
    ]
    await openDistrict()

    const row = hierarchyRow('Region to District')
    expect(row.textContent).toContain('Pending')
    findButton(row, 'Edit').click()
    await settle()

    const inputs = hierarchyInputs()
    // 层级以最新草稿为基线;缺席的 display_name 回退模型值
    expect(inputs[1].value).toBe('Region to District')
    expect(inputs[2].value).toBe('A2')
  })

  it('delete goes through the confirm dialog and lands a delete draft', async () => {
    await openDistrict()

    findButton(hierarchyRow('Region to District'), 'Delete').click()
    await settle()
    expect(confirmPanel().textContent).toContain('region_to_district')

    confirmPanel().querySelector<HTMLButtonElement>('.confirm-btn.is-solid')!.click()
    await settle()

    expect(calls('/v1/admin/semantic/demo/drafts')[0]).toMatchObject({
      kind: 'hierarchy',
      action: 'delete',
      name: 'region_to_district',
      payload: {},
      note: '',
    })
  })

  it('editing saves name + dataset + levels with the name field still locked', async () => {
    await openDistrict()

    findButton(hierarchyRow('Region to District'), 'Edit').click()
    await settle()
    await type(hierarchyInputs()[2], 'A3, A2, A1')
    expect(hierarchyInputs()[0].disabled).toBe(true)

    hierarchyAction('Create draft').click()
    await settle()

    expect(calls('/v1/admin/semantic/demo/drafts')[0]).toMatchObject({
      kind: 'hierarchy',
      action: 'upsert',
      name: 'region_to_district',
      payload: {
        dataset: 'district',
        display_name: 'Region to District',
        levels: ['A3', 'A2', 'A1'],
      },
    })
  })

  it('switching assets closes the form and discards the half-typed input', async () => {
    const view = await openDistrict()

    hierarchyAction('New hierarchy').click()
    await settle()
    await type(hierarchyInputs()[0], 'half_typed')

    await rowByText(view, 'account').trigger('click')
    await settle()
    await rowByText(view, 'district').trigger('click')
    await settle()

    expect(drawer().querySelector('.sem-hier-form')).toBeNull()
  })

  it('the draft drawer validates a hierarchy draft as hierarchy (DraftKind widening)', async () => {
    PENDING = [
      {
        id: 'h3',
        kind: 'hierarchy',
        action: 'upsert',
        name: 'district_geo',
        created_at: '2026-10-02T10:00:00Z',
        payload: { dataset: 'district', levels: ['A3', 'A2'] },
      },
    ]
    mockApi()
    await mountView('/admin/semantic?ds=demo&tab=pending&draft=h3')
    await settle()

    expect(calls('/v1/admin/semantic/demo/validate')[0]).toMatchObject({
      kind: 'hierarchy',
      action: 'upsert',
      name: 'district_geo',
      payload: { dataset: 'district', levels: ['A3', 'A2'] },
    })
  })

  it('asset delete still uses the asset kind (ConfirmCtx widening does not leak)', async () => {
    mockApi()
    const view = await mountView('/admin/semantic?ds=demo&tab=datasets')
    await rowByText(view, 'account').trigger('click')
    await settle()

    // account 没有层级行 → 抽屉里只有 footer 的 Delete
    findButton(drawer(), 'Delete').click()
    await settle()
    expect(confirmPanel().textContent).toContain('account')
    confirmPanel().querySelector<HTMLButtonElement>('.confirm-btn.is-solid')!.click()
    await settle()

    expect(calls('/v1/admin/semantic/demo/drafts')[0]).toMatchObject({
      kind: 'dataset',
      action: 'delete',
      name: 'account',
      payload: {},
    })
  })
})
