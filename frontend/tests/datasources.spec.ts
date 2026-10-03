/**
 * DatasourcesView — registration/edit dialogs plus the P6 additions:
 * §2.3 page header and §4.3 URL state (q / health / drift).
 *
 * The list endpoint takes no filter parameters, so the three filters run
 * client-side over the fetched catalog; the drift filter reads the per-source
 * drift counts (/v1/admin/drift?ds=...), which is why apiGet is mocked by
 * path rather than by call order.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import DatasourcesView from '../src/views/admin/DatasourcesView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}))

// Confirm dialogs gate billed/irreversible actions — stub the real dialog,
// keep the ElementPlus plugin (default export) intact for component mounts.
vi.mock('element-plus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('element-plus')>()
  return { ...actual, ElMessageBox: { confirm: vi.fn() } }
})

import { apiGet, apiPost, apiPut } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

let wrapper: VueWrapper | null = null
let router: Router

const DS = [
  { name: 'financial', type: 'mysql', default: true, status: 'connected', kb_initialized: true, kb_items: { schema_notes: 12 } },
  { name: 'demo', type: 'demo', status: 'disconnected', kb_initialized: false, kb_items: {} },
]

interface GetOpts {
  datasources?: unknown[]
  detail?: unknown
  /** per-source open drift-item counts, served through /v1/admin/drift */
  drift?: Record<string, number>
}

function mockGet(opts: GetOpts = {}) {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/admin/drift')) {
      const ds = new URLSearchParams(path.split('?')[1] ?? '').get('ds') ?? ''
      const n = opts.drift?.[ds] ?? 0
      return { items: Array.from({ length: n }, (_, i) => ({ id: i })) }
    }
    if (path.startsWith('/v1/admin/datasources/')) {
      return { datasource: opts.detail ?? null }
    }
    return { datasources: opts.datasources ?? [] }
  })
}

async function mountView(query = '') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/datasources', component: DatasourcesView },
    ],
  })
  await router.push(`/admin/datasources${query}`)
  await router.isReady()
  wrapper = mount(DatasourcesView, {
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function rowNames(view: VueWrapper): string[] {
  return view.findAll('.el-table__body tbody tr .ds-name').map((n) => n.text())
}

function dialogs(): HTMLElement[] {
  return Array.from(document.body.querySelectorAll<HTMLElement>('.el-dialog'))
}

async function setInput(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    'value',
  )!.set!
  setter.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

beforeEach(() => {
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('DatasourcesView', () => {
  it('renders datasources with status labels and row actions', async () => {
    mockGet({ datasources: DS })
    const view = await mountView()
    const text = view.text()
    expect(text).toContain('financial')
    expect(text).toContain('demo')
    expect(text).toContain('Connected')
    expect(text).toContain('Disconnected')
    expect(text).toContain('default')
    // KB status is gone from this page — actions are edit / test / delete
    expect(text).not.toContain('Initialized KB')
    // row actions are icon-only; the tooltip carries the label
    const testBtns = view.findAll('button.test')
    expect(testBtns.length).toBe(2)
    expect(testBtns[0].attributes('title')).toBe('Test connection')
    expect(text).not.toContain('Test connection')
  })

  it('registers a datasource through the dialog via POST', async () => {
    mockGet({ datasources: [] })
    ;(apiPost as any).mockResolvedValue({ datasource: { name: 'newds' } })
    const view = await mountView()

    // opens the register dialog (empty state CTA)
    await view.find('button.add').trigger('click')
    await flushPromises()

    // switch type to MySQL → URL field appears
    const select = view
      .findAllComponents({ name: 'ElSelect' })
      .find((c) => c.classes().includes('ds-type-select'))!
    select.vm.$emit('update:modelValue', 'mysql')
    await flushPromises()

    const dialog = dialogs()[0]
    const urlInput = dialog.querySelector<HTMLInputElement>(
      '.ds-url-input input',
    )!
    await setInput(urlInput, 'mysql://user@localhost:3306/financial')

    const nameInput = dialog.querySelector<HTMLInputElement>(
      '.ds-name-field input',
    )!
    await setInput(nameInput, 'newds')

    const register = Array.from(
      dialog.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent!.includes('Register'))!
    register.click()
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources', {
      name: 'newds',
      url: 'mysql://user@localhost:3306/financial',
    })
  })

  it('registers the built-in demo without a URL', async () => {
    mockGet({ datasources: [] })
    ;(apiPost as any).mockResolvedValue({ datasource: { name: 'demo' } })
    const view = await mountView()
    await view.find('button.add').trigger('click')
    await flushPromises()
    const dialog = dialogs()[0]
    const register = Array.from(
      dialog.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent!.includes('Register'))!
    register.click()
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources', {
      name: '',
      url: 'demo',
    })
  })

  it('tests a connection by name without touching the registration', async () => {
    mockGet({ datasources: [{ name: 'financial', type: 'mysql', status: 'connected', kb_initialized: false }] })
    ;(apiPost as any).mockResolvedValue({ ok: true, error: null })
    const view = await mountView()
    await view.find('button.test').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources/test-connection', { name: 'financial' })
  })

  it('disables edit once the KB is initialized', async () => {
    mockGet({ datasources: [
      { name: 'financial', type: 'mysql', status: 'connected', kb_initialized: true },
      { name: 'demo', type: 'demo', status: 'connected', kb_initialized: false },
    ] })
    const view = await mountView()
    const editBtns = view.findAll('button.edit')
    expect(editBtns.length).toBe(2)
    expect(editBtns[0].attributes('disabled')).toBeDefined() // KB locked
    expect(editBtns[1].attributes('disabled')).toBeDefined() // demo locked
  })

  it('edits a datasource connection through the dialog', async () => {
    mockGet({
      datasources: [{ name: 'financial', type: 'mysql', status: 'connected', kb_initialized: false }],
      detail: { name: 'financial', type: 'mysql', url: 'mysql://user@localhost:3306/financial', status: 'connected', kb_initialized: false },
    })
    ;(apiPut as any).mockResolvedValue({ datasource: {} })
    const view = await mountView()

    await view.find('button.edit').trigger('click')
    await flushPromises()

    const dialog = dialogs()[0]
    const urlInput = dialog.querySelector<HTMLInputElement>('.ds-url-input input')!
    expect(urlInput.value).toContain('mysql://user@localhost:3306/financial')
    await setInput(urlInput, 'mysql://user@localhost:3306/other')

    const save = Array.from(
      dialog.querySelectorAll<HTMLButtonElement>('button'),
    ).find((b) => b.textContent!.includes('Save'))!
    save.click()
    await flushPromises()
    expect(apiPut).toHaveBeenCalledWith('/v1/admin/datasources/financial', { url: 'mysql://user@localhost:3306/other' })
  })
})

describe('DatasourcesView page header (P6 §2.3)', () => {
  it('renders PageHeader with the root crumb and document.title', async () => {
    mockGet({ datasources: DS })
    const view = await mountView()
    expect(view.find('h1').text()).toBe('Datasources')
    const crumbs = view.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Datasources'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(document.title).toBe('Datasources')
  })
})

describe('DatasourcesView URL state (§4.3)', () => {
  it('lands on the drifted sources for ?drift=open', async () => {
    mockGet({ datasources: DS, drift: { financial: 2 } })
    const view = await mountView('?drift=open')
    expect(rowNames(view)).toEqual(['financial'])
    // the count rides along as a warn pill on the policy row
    expect(view.text()).toContain('Drifted 2')
  })

  it('lands on the offline sources for ?health=disconnected', async () => {
    mockGet({ datasources: DS })
    const view = await mountView('?health=disconnected')
    expect(rowNames(view)).toEqual(['demo'])
  })

  it('keeps the deep-linked filters across a remount (refresh)', async () => {
    mockGet({ datasources: DS })
    const view = await mountView('?q=fin&health=connected')
    expect(rowNames(view)).toEqual(['financial'])
    expect(router.currentRoute.value.query).toMatchObject({
      q: 'fin',
      health: 'connected',
    })
  })

  it('filters by search and writes q into the URL', async () => {
    mockGet({ datasources: DS })
    const view = await mountView()
    expect(rowNames(view).length).toBe(2)
    await view.find('.toolbar-search input').setValue('demo')
    await flushPromises()
    expect(rowNames(view)).toEqual(['demo'])
    expect(router.currentRoute.value.query.q).toBe('demo')

    // a clean list keeps a clean URL
    await view.find('.toolbar-search input').setValue('')
    await flushPromises()
    expect(router.currentRoute.value.query.q).toBeUndefined()
    expect(rowNames(view).length).toBe(2)
  })
})
