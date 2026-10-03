/**
 * JobsView — P6 §2.3 page header + §4.3 URL state.
 *
 * The list endpoint takes no filter parameters, so filtering/paging are the
 * page's own job over the fetched rows; what these tests pin is that the
 * URL is still the state: /admin/jobs?status=error (the shell's failed-jobs
 * drill-down) lands on a filtered list, the filter writes back into the URL
 * and a refresh keeps it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import JobsView from '../src/views/admin/JobsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}))

import { apiGet } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'

function job(over: Record<string, unknown>) {
  return {
    id: 'j1',
    name: 'daily loans',
    question: '贷款总额?',
    datasource: 'demo',
    workflow: 'reflection',
    schedule_type: 'interval',
    schedule: '60',
    enabled: true,
    alert_expr: '',
    alert_channel: '',
    alert_cooldown_min: 30,
    decision_rule: '',
    next_run_at: '2026-10-04T09:00:00Z',
    created_at: '2026-10-01T09:00:00Z',
    updated_at: '2026-10-01T09:00:00Z',
    recent_run: null,
    ...over,
  }
}

const JOBS = [
  job({ id: 'j1', name: 'daily loans', recent_run: { status: 'ok' } }),
  job({ id: 'j2', name: 'risky balances', recent_run: { status: 'error' } }),
  job({ id: 'j3', name: 'quiet orders', enabled: false, recent_run: { status: 'alert' } }),
  job({ id: 'j4', name: 'never ran', recent_run: null }),
]

let wrapper: VueWrapper | null = null
let router: Router

async function mountView(query = '') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/jobs', component: JobsView },
    ],
  })
  await router.push(`/admin/jobs${query}`)
  await router.isReady()
  wrapper = mount(JobsView, {
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function rowNames(view: VueWrapper): string[] {
  return view.findAll('.el-table__body tbody tr .job-name').map((n) => n.text())
}

beforeEach(() => {
  setActivePinia(createPinia())
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  document.body.innerHTML = ''
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/admin/jobs')) return { jobs: JOBS, total: JOBS.length }
    if (path.startsWith('/v1/catalog/datasources')) {
      return { datasources: [{ name: 'demo', default: true }] }
    }
    return {}
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('JobsView page header (P6 §2.3)', () => {
  it('renders PageHeader with the root crumb and document.title', async () => {
    const view = await mountView()
    expect(view.find('h1').text()).toBe('Scheduled jobs')
    const crumbs = view.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Scheduled jobs'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(document.title).toBe('Scheduled jobs')
  })
})

describe('JobsView URL state (§4.3)', () => {
  it('lands on the filtered list for the shell deep link ?status=error', async () => {
    const view = await mountView('?status=error')
    expect(rowNames(view)).toEqual(['risky balances'])
  })

  it('filters by last-run status and by the enable switch', async () => {
    const view = await mountView('?status=disabled')
    expect(rowNames(view)).toEqual(['quiet orders'])
  })

  it('filters by the search box and writes q into the URL', async () => {
    const view = await mountView()
    expect(rowNames(view).length).toBe(4)
    await view.find('.toolbar-search input').setValue('orders')
    await flushPromises()
    expect(rowNames(view)).toEqual(['quiet orders'])
    expect(router.currentRoute.value.query.q).toBe('orders')

    // a clean list keeps a clean URL
    await view.find('.toolbar-search input').setValue('')
    await flushPromises()
    expect(router.currentRoute.value.query.q).toBeUndefined()
    expect(rowNames(view).length).toBe(4)
  })

  it('clamps an out-of-range page back to 1 and drops the key from the URL', async () => {
    const view = await mountView('?status=error&page=2')
    // page 2 of a one-row result set clamps back to page 1 — the URL must
    // stop claiming a page that does not exist (default values are not written)
    expect(router.currentRoute.value.query.page).toBeUndefined()
    expect(router.currentRoute.value.query.status).toBe('error')
    expect(rowNames(view)).toEqual(['risky balances'])
  })
})
