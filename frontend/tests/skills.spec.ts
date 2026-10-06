import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import SkillsView from '../src/views/admin/SkillsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

import { apiGet, apiPost } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

let wrapper: VueWrapper | null = null
let router: Router

beforeEach(() => {
  setActivePinia(createPinia())
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

async function mountRows(rows: unknown[], query = '') {
  ;(apiGet as any).mockResolvedValue({ skills: rows })
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/skills', component: SkillsView },
    ],
  })
  await router.push(`/admin/skills${query}`)
  await router.isReady()
  wrapper = mount(SkillsView, {
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

const ORG = {
  source: 'admin',
  status: 'confirmed',
  description: 'd',
  triggers: {},
}

describe('SkillsView tier column', () => {
  it('names the validator tier instead of falling through to available', async () => {
    // validator 是第三档,不是"没选 required 的 available" —— 它按结果断言
    // 运行、正文从不投给模型。落到 available 分支上就是显示别人的名字。
    const view = await mountRows([
      { ...ORG, name: 'credit-guard', tier: 'validator' },
      { ...ORG, name: 'soft', tier: 'available' },
    ])
    const text = view.text()
    expect(text).toContain('validator (result assertions)')
    expect(text).toContain('available (on demand)')
    // 校验器那行的提示说清"手写 SKILL.md",而不是让人去找一个不存在的按钮
    const titles = view
      .findAll('[title]')
      .map((el) => el.attributes('title') ?? '')
    expect(titles.some((h) => h.includes('SKILL.md'))).toBe(true)
  })

  it('hides the tier toggle for validator rows only', async () => {
    // 这条按钮对 validator 行**恒 400**(service 结构性拒绝:四字段按当前
    // tier 投影,两个方向都校验不出)—— 留一个必定失败的按钮比不给更坏。
    const view = await mountRows([
      { ...ORG, name: 'credit-guard', tier: 'validator' },
      { ...ORG, name: 'soft', tier: 'available' },
    ])
    const labels = view.findAll('button').map((b) => b.text())
    expect(labels.filter((l) => l === 'Set required').length).toBe(1)
    expect(labels.filter((l) => l === 'Set available').length).toBe(0)
  })

  it('still posts the toggle for a non-validator row', async () => {
    const view = await mountRows([{ ...ORG, name: 'soft', tier: 'required' }])
    ;(apiPost as any).mockResolvedValue({})
    const btn = view.findAll('button').find((b) => b.text() === 'Set available')!
    await btn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/skills/soft/tier', {
      tier: 'available',
    })
  })
})

describe('SkillsView page header (P6 §2.3)', () => {
  it('renders PageHeader with the root crumb and document.title', async () => {
    const view = await mountRows([{ ...ORG, name: 'soft', tier: 'available' }])
    expect(view.find('h1').text()).toBe('Methodology skills')
    const crumbs = view.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Methodology skills'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(document.title).toBe('Methodology skills')
    // the page-level actions moved into the header slot
    const actionTexts = view
      .findAll('header .ph-actions button')
      .map((b) => b.text())
    expect(actionTexts.some((x) => x.includes('New draft'))).toBe(true)
  })
})

describe('SkillsView URL state (§4.3)', () => {
  const ROWS = [
    { ...ORG, name: 'credit-guard', tier: 'validator' },
    { ...ORG, name: 'soft', tier: 'available' },
    { ...ORG, name: 'queued', tier: 'available', status: 'pending' },
  ]

  function names(view: VueWrapper): string[] {
    return view
      .findAll('.el-table__body tbody tr .cell-mono')
      .map((n) => n.text())
      .filter((x) => ROWS.some((r) => r.name === x))
  }

  it('lands on the review queue for ?status=pending', async () => {
    const view = await mountRows(ROWS, '?status=pending')
    expect(names(view)).toEqual(['queued'])
  })

  it('lands on the validator tier for ?tier=validator', async () => {
    const view = await mountRows(ROWS, '?tier=validator')
    expect(names(view)).toEqual(['credit-guard'])
  })

  it('writes the status filter back into the URL and keeps a clean URL clean', async () => {
    const view = await mountRows(ROWS)
    expect(names(view).length).toBe(3)
    const select = view
      .findAllComponents({ name: 'ElSelect' })
      .find((c) => c.classes().includes('filter-select'))!
    select.vm.$emit('update:modelValue', 'pending')
    await flushPromises()
    expect(names(view)).toEqual(['queued'])
    expect(router.currentRoute.value.query.status).toBe('pending')

    select.vm.$emit('update:modelValue', '')
    await flushPromises()
    expect(router.currentRoute.value.query.status).toBeUndefined()
    expect(names(view).length).toBe(3)
  })
})

describe('SkillsView disable / enable (E6)', () => {
  function labels(view: VueWrapper): string[] {
    return view.findAll('button').map((b) => b.text())
  }

  function rowNames(view: VueWrapper, known: string[]): string[] {
    return view
      .findAll('.el-table__body tbody tr .cell-mono')
      .map((n) => n.text())
      .filter((x) => known.includes(x))
  }

  it('labels a disabled row as disabled (not rejected) with the muted pill', async () => {
    // disabled 是第四态;落到 statusLabel 的 else 分支上会显示"已拒绝" —— 一个
    // 明确可恢复的状态被说成已删除,操作员会去找根本不需要的重建路径。
    const view = await mountRows([
      { ...ORG, name: 'paused', tier: 'available', status: 'disabled' },
    ])
    expect(view.text()).toContain('disabled')
    expect(view.text()).not.toContain('rejected')
    const pill = view.findAll('.pill').find((p) => p.text() === 'disabled')
    expect(pill?.classes()).toContain('pill-disabled')
  })

  it('offers exactly the legal transition per status', async () => {
    // pending → 确认/拒绝;confirmed → 停用(+档位切换);disabled → 仅启用。
    // 后端状态机严格非幂等,UI 不给任何非法组合。
    const view = await mountRows([
      { ...ORG, name: 'active', tier: 'available' },
      { ...ORG, name: 'paused', tier: 'available', status: 'disabled' },
      { ...ORG, name: 'queued', tier: 'available', status: 'pending' },
    ])
    const all = labels(view)
    expect(all.filter((l) => l === 'Disable').length).toBe(1)
    expect(all.filter((l) => l === 'Enable').length).toBe(1)
    expect(all.filter((l) => l === 'Confirm').length).toBe(1)
    expect(all.filter((l) => l === 'Reject').length).toBe(1)
    expect(all.filter((l) => l === 'Set required').length).toBe(1)
  })

  it('never shows reject on a disabled row (backend reject deletes without a state check)', async () => {
    // 后端的 reject 只查目录存在、不查状态:对 disabled 行调用会直接删掉资产。
    // 这是"hide 一个按钮"背后真正的承重项,不是样式问题。
    const view = await mountRows([
      { ...ORG, name: 'paused', tier: 'available', status: 'disabled' },
    ])
    expect(labels(view)).not.toContain('Reject')
    expect(labels(view)).toContain('Enable')
  })

  it('posts disable for a confirmed row', async () => {
    const view = await mountRows([{ ...ORG, name: 'soft', tier: 'available' }])
    ;(apiPost as any).mockResolvedValue({})
    const btn = view.findAll('button').find((b) => b.text() === 'Disable')!
    await btn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/skills/soft/disable')
  })

  it('posts enable for a disabled row', async () => {
    const view = await mountRows([
      { ...ORG, name: 'soft', tier: 'available', status: 'disabled' },
    ])
    ;(apiPost as any).mockResolvedValue({})
    const btn = view.findAll('button').find((b) => b.text() === 'Enable')!
    await btn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/skills/soft/enable')
  })

  it('carries the granular-disable hint on the disable button', async () => {
    const view = await mountRows([{ ...ORG, name: 'soft', tier: 'available' }])
    const titles = view
      .findAll('button[title]')
      .map((b) => b.attributes('title') ?? '')
    expect(titles.some((h) => h.includes('Granular disable'))).toBe(true)
  })

  it('names the guard tier, hints the hand-written SKILL.md, and hides its tier toggle', async () => {
    // guard 与 validator 同族:手写档、正文不投模型、档位切换恒 400。
    const view = await mountRows([
      { ...ORG, name: 'guardrail', tier: 'guard' },
      { ...ORG, name: 'soft', tier: 'available' },
    ])
    expect(view.text()).toContain('guard (SQL assertions)')
    const all = labels(view)
    expect(all.filter((l) => l === 'Set available').length).toBe(0)
    expect(all.filter((l) => l === 'Set required').length).toBe(1)
    const titles = view.findAll('[title]').map((el) => el.attributes('title') ?? '')
    expect(titles.some((h) => h.includes('guard block'))).toBe(true)
  })

  it('filters to disabled rows from ?status=disabled', async () => {
    const known = ['active', 'paused']
    const view = await mountRows(
      [
        { ...ORG, name: 'active', tier: 'available' },
        { ...ORG, name: 'paused', tier: 'available', status: 'disabled' },
      ],
      '?status=disabled',
    )
    expect(rowNames(view, known)).toEqual(['paused'])
  })

  it('filters to guard rows from ?tier=guard', async () => {
    const known = ['guardrail', 'soft']
    const view = await mountRows(
      [
        { ...ORG, name: 'guardrail', tier: 'guard' },
        { ...ORG, name: 'soft', tier: 'available' },
      ],
      '?tier=guard',
    )
    expect(rowNames(view, known)).toEqual(['guardrail'])
  })
})
