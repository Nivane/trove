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
