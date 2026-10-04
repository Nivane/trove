/**
 * DecisionsView — P6 §2.3 page header and §4.3 URL state.
 *
 * The page owns one key: `ds`. The decisions endpoint takes exactly
 * `datasource=`, so the selector is a real filter — a shared link
 * /admin/decisions?ds=demo must land on the same rule list (and the page,
 * like KbView, reflects its auto-picked default back into the URL).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import DecisionsView from '../src/views/admin/DecisionsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPut: vi.fn(),
}))

import { apiGet } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

let wrapper: VueWrapper | null = null
let router: Router

const DSOURCES = {
  datasources: [
    { name: 'demo', status: 'connected', default: true },
    { name: 'financial', status: 'connected' },
  ],
}

function rule(id: string) {
  return {
    id,
    name: `rule ${id}`,
    enabled: true,
    severity: 'warning',
    owner_role: 'admin',
    window: '7d',
    scope: 'kpi',
    emit: 'alert',
    conditions: ['avg > 1'],
    condition_mode: 'all',
    referenced_by: [],
  }
}

function mockGet(rules: Record<string, ReturnType<typeof rule>[]> = {}) {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/catalog/datasources')) return DSOURCES
    const ds = new URLSearchParams(path.split('?')[1] ?? '').get('datasource')
    return { rules: rules[ds ?? ''] ?? [], issues: [], digest: 'abc123' }
  })
}

async function mountView(query = '') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/decisions', component: DecisionsView },
    ],
  })
  await router.push(`/admin/decisions${query}`)
  await router.isReady()
  wrapper = mount(DecisionsView, {
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function ruleIds(view: VueWrapper): string[] {
  return view
    .findAll('.el-table__body tbody tr .cell-mono')
    .map((n) => n.text())
    .filter((x) => x.startsWith('r-'))
}

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

describe('DecisionsView page header (P6 §2.3)', () => {
  it('renders PageHeader with the root crumb and document.title', async () => {
    mockGet({ demo: [rule('r-demo')] })
    const view = await mountView()
    expect(view.find('h1').text()).toBe('Decision rules')
    const crumbs = view.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Decision rules'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(document.title).toBe('Decision rules')
  })
})

describe('DecisionsView URL state (§4.3)', () => {
  it('lands on the deep-linked datasource for ?ds=financial', async () => {
    mockGet({ demo: [rule('r-demo')], financial: [rule('r-fin')] })
    const view = await mountView('?ds=financial')
    expect(ruleIds(view)).toEqual(['r-fin'])
    expect(router.currentRoute.value.query.ds).toBe('financial')
  })

  it('reflects the auto-picked default datasource back into the URL', async () => {
    // a clean URL must not silently show demo rules while claiming nothing
    mockGet({ demo: [rule('r-demo')] })
    const view = await mountView()
    expect(ruleIds(view)).toEqual(['r-demo'])
    expect(router.currentRoute.value.query.ds).toBe('demo')
  })

  it('writes a selector change into the URL', async () => {
    mockGet({ demo: [rule('r-demo')], financial: [rule('r-fin')] })
    const view = await mountView('?ds=demo')
    const select = view.findComponent({ name: 'ElSelect' })
    // a real pick emits both: v-model writes the state, change triggers load
    select.vm.$emit('update:modelValue', 'financial')
    select.vm.$emit('change', 'financial')
    await flushPromises()
    expect(router.currentRoute.value.query.ds).toBe('financial')
    expect(ruleIds(view)).toEqual(['r-fin'])
  })
})

describe('DecisionsView noise band (B2)', () => {
  it('renders the band line only for rules that declare one', async () => {
    mockGet({
      demo: [
        rule('r-plain'),
        {
          ...rule('r-band'),
          seasonal: { grain: 'month', lookback: 12, mode: 'trailing', k: 3.5 },
          significance: { require: 'outside_band' },
        },
        {
          ...rule('r-record'),
          seasonal: { grain: '', lookback: 8, mode: 'same_phase', k: 3.5 },
          significance: { require: '' },
        },
      ],
    })
    const view = await mountView()
    const text = view.text()
    // 声明了的规则:季节基线 + 口径 pill;auto 粒度显示为 auto
    expect(text).toContain('month × 12 · k=3.5')
    expect(text).toContain('Requires outside band')
    expect(text).toContain('auto × 8 · k=3.5')
    expect(text).toContain('Noise band · record only')
    // 未声明的规则不带这一行 —— 出现次数等于声明数,不是规则数
    expect(text.match(/Seasonal baseline/g)).toHaveLength(2)
  })
})

describe('DecisionsView table layout (F1)', () => {
  it('pins the verdict-history column to the right edge of the table', async () => {
    mockGet({ demo: [rule('r-demo')] })
    const view = await mountView()
    const headers = view.findAll('.el-table__header th')
    expect(headers.length).toBe(9)
    const last = headers[headers.length - 1]
    expect(last.text()).toContain('Verdict history')
    // Element Plus renders fixed columns as sticky cells carrying
    // `el-table-fixed-column--right`; without it a narrow viewport pushes the
    // only action column out of sight instead of pinning it.
    expect(last.classes()).toContain('el-table-fixed-column--right')
  })
})
