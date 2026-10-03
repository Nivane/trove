/**
 * UsersView — the P6/P7 vertical slice.
 *
 * The page is exercised end-to-end against a mock of the *server* contract
 * (parameterised /v1/admin/users, inlined grants, tokens, audit) and a real
 * router, so what the assertions pin is the page's own behaviour: URL-backed
 * filters, KPI-as-filter, staged drawer edits, blast-radius confirmations,
 * field-level validation and the four list states.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import UsersView from '../src/views/admin/UsersView.vue'

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

import { apiDelete, apiGet, apiPatch, apiPost, apiPut, ApiError } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'

interface Row {
  id: number
  username: string
  display_name: string
  role: string
  disabled: boolean
  created_at: string
  datasources: string[]
}

let USERS: Row[] = []
let TOKENS: Record<number, { id: number; label: string; revoked: number; created_at: string }[]> = {}

function resetFixtures() {
  USERS = [
    {
      id: 1,
      username: 'admin',
      display_name: 'Administrator',
      role: 'admin',
      disabled: false,
      created_at: '2026-01-01T08:00:00Z',
      datasources: ['demo'],
    },
    {
      id: 2,
      username: 'lin.wang',
      display_name: 'Lin Wang',
      role: 'analyst',
      disabled: false,
      created_at: '2026-02-01T08:00:00Z',
      datasources: ['demo', 'sales'],
    },
    {
      id: 3,
      username: 'bob',
      display_name: 'Bob',
      role: 'user',
      disabled: true,
      created_at: '2026-03-01T08:00:00Z',
      datasources: [],
    },
  ]
  TOKENS = {
    1: [{ id: 11, label: 'ci', revoked: 0, created_at: '2026-01-02T00:00:00Z' }],
    2: [
      { id: 21, label: 'etl', revoked: 0, created_at: '2026-02-02T00:00:00Z' },
      { id: 22, label: 'old', revoked: 1, created_at: '2026-02-03T00:00:00Z' },
    ],
    3: [],
  }
}

/** The server as the contract describes it: filter → count → page. */
function serverList(path: string): { users: Row[]; total: number } {
  const url = new URL(path, 'http://trove')
  const q = (url.searchParams.get('q') ?? '').toLowerCase()
  const role = url.searchParams.get('role') ?? ''
  const status = url.searchParams.get('status') ?? ''
  const rows = USERS.filter((u) => {
    if (q && !`${u.username} ${u.display_name}`.toLowerCase().includes(q)) return false
    if (role && u.role !== role) return false
    if (status === 'active' && u.disabled) return false
    if (status === 'disabled' && !u.disabled) return false
    if (status === 'nogrant' && u.datasources.length) return false
    return true
  })
  const limit = Number(url.searchParams.get('limit') ?? '20')
  const offset = Number(url.searchParams.get('offset') ?? '0')
  return { users: rows.slice(offset, offset + limit), total: rows.length }
}

function mockApi(overrides: { list?: (path: string) => unknown } = {}) {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/admin/users?')) {
      return overrides.list ? overrides.list(path) : serverList(path)
    }
    if (path === '/v1/catalog/datasources') {
      return { datasources: [{ name: 'demo' }, { name: 'sales' }] }
    }
    const tk = path.match(/^\/v1\/admin\/users\/(\d+)\/tokens$/)
    if (tk) return { tokens: TOKENS[Number(tk[1])] ?? [] }
    if (path.startsWith('/v1/admin/audit')) {
      return { audit: [{ id: 99, ts: '2026-03-05T10:00:00Z', action: 'query.run' }], total: 1 }
    }
    return {}
  })
}

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountView(url = '/admin/users') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/users', component: { render: () => null } },
      { path: '/admin/audit', component: { render: () => null } },
    ],
  })
  await router.push(url)
  await router.isReady()
  wrapper = mount(UsersView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

/**
 * Every *page* list request the page made, newest last.
 *
 * The KPI tiles read the same endpoint with `limit=1` and no offset, so the
 * page requests are exactly the ones that carry an offset — without this the
 * "last list call" would often be a KPI count, not the filtered page.
 */
function listCalls(): string[] {
  return (apiGet as any).mock.calls
    .map((c: unknown[]) => String(c[0]))
    .filter((p: string) => p.startsWith('/v1/admin/users?') && p.includes('offset='))
}

function lastListCall(): string {
  const calls = listCalls()
  return calls[calls.length - 1] ?? ''
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

function findButton(root: ParentNode, text: string): HTMLElement {
  const btn = Array.from(root.querySelectorAll<HTMLElement>('button')).find((b) =>
    (b.textContent ?? '').trim().includes(text),
  )
  if (!btn) throw new Error(`button not found: ${text}`)
  return btn
}

/**
 * Wait for one render cycle *and* Element Plus's form-item error debounce
 * (`refDebounced(validateState, 100)`): validation messages appear ~100ms
 * after the state flips, which `flushPromises()` alone never reaches.
 */
async function settle(ms = 140) {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, ms))
  await flushPromises()
}

function kpiTile(view: VueWrapper, label: string) {
  const tile = view.findAll('.kpi-tile').find((el) => el.text().includes(label))
  if (!tile) throw new Error(`KPI tile not found: ${label}`)
  return tile
}

beforeEach(() => {
  resetFixtures()
  pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = 'en'
  ;(apiPost as any).mockResolvedValue({})
  ;(apiPatch as any).mockResolvedValue({})
  ;(apiPut as any).mockResolvedValue({})
  ;(apiDelete as any).mockResolvedValue(undefined)
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('UsersView', () => {
  it('renders the server list with KPI counts, roles, status and grants', async () => {
    mockApi()
    const view = await mountView()
    const text = view.text()
    expect(text).toContain('admin')
    expect(text).toContain('lin.wang')
    expect(text).toContain('Analyst')
    expect(text).toContain('Active')
    expect(text).toContain('Disabled')
    expect(text).toContain('2026/01/01')
    // grant column: the zero-grant user is called out, counts are shown
    expect(kpiTile(view, 'Needs grants').text()).toContain('1')
    expect(kpiTile(view, 'All users').text()).toContain('3')
    expect(text).toContain('2 datasources')

    // the list is fetched server-side with sort/order/paging
    const call = lastListCall()
    expect(call).toContain('sort=created_at')
    expect(call).toContain('order=desc')
    expect(call).toContain('limit=20')
    expect(call).toContain('offset=0')
    // no per-user grant N+1 on load
    expect(
      (apiGet as any).mock.calls.filter((c: unknown[]) =>
        /\/users\/\d+\/datasources/.test(String(c[0])),
      ),
    ).toHaveLength(0)
  })

  it('writes the search box into the URL and refetches with it', async () => {
    mockApi()
    const view = await mountView()
    await view.find('.toolbar-search input').setValue('lin')
    await flushPromises()
    expect(router.currentRoute.value.query.q).toBe('lin')
    expect(lastListCall()).toContain('q=lin')
  })

  it('treats KPI tiles as filters and can return to all users', async () => {
    mockApi()
    const view = await mountView()
    await kpiTile(view, 'Needs grants').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.status).toBe('nogrant')
    expect(lastListCall()).toContain('status=nogrant')
    expect(view.findAll('.dt-row')).toHaveLength(1)
    expect(kpiTile(view, 'Needs grants').classes()).toContain('is-active')

    await kpiTile(view, 'All users').trigger('click')
    await flushPromises()
    expect('status' in router.currentRoute.value.query).toBe(false)
    expect(lastListCall()).not.toContain('status=')
    expect(view.findAll('.dt-row')).toHaveLength(3)
  })

  it('sorts server-side from the column headers via the URL', async () => {
    mockApi()
    const view = await mountView()
    const usernameSort = view.findAll('.dt-sort')[0]
    await usernameSort.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.sort).toBe('username')
    expect(router.currentRoute.value.query.order).toBe('asc')
    expect(lastListCall()).toContain('sort=username')
  })

  it('keeps the page size in the URL and refetches with it (U3)', async () => {
    mockApi()
    // a deep link keeps its rows-per-page: page 2 of a 50-row window
    const view = await mountView('/admin/users?size=50&page=2')
    expect(lastListCall()).toContain('limit=50')
    expect(lastListCall()).toContain('offset=50')
    expect(view.findComponent('.pager-size').props('modelValue')).toBe(50)

    // a picked size lands in the URL and refetches
    view.findComponent('.pager-size').vm.$emit('change', 100)
    await flushPromises()
    expect(router.currentRoute.value.query.size).toBe('100')
    expect(lastListCall()).toContain('limit=100')
  })

  it('drops a hand-edited size the pager cannot offer', async () => {
    mockApi()
    await mountView('/admin/users?size=25')
    // the URL must not claim a page size the selector cannot show
    expect('size' in router.currentRoute.value.query).toBe(false)
    expect(lastListCall()).toContain('limit=20')
  })

  it('enables or disables the selection from the bulk bar', async () => {
    mockApi()
    const view = await mountView()
    const checks = view.findAll('.dt-check')
    await checks[1].trigger('click')
    await checks[2].trigger('click')
    expect(view.findAll('.dt-row.is-selected')).toHaveLength(2)
    expect(view.find('.bulk-bar').text()).toContain('2 selected')

    findButton(view.find('.bulk-bar').element, 'Disable').click()
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledWith('/v1/admin/users/1', { disabled: true })
    expect(apiPatch).toHaveBeenCalledWith('/v1/admin/users/2', { disabled: true })
    // selection cleared and list refetched
    expect(view.find('.bulk-bar').exists()).toBe(false)
  })

  it('stages drawer edits: unsaved hint, no implicit PUT, explicit save', async () => {
    mockApi()
    const view = await mountView()
    await view.findAll('.dt-row')[1].trigger('click')
    await flushPromises()

    const panel = document.body.querySelector('.drawer-panel')
    expect(panel).not.toBeNull()
    expect(panel!.textContent).toContain('lin.wang')
    // recent activity comes from the audit endpoint, read-only
    expect(panel!.textContent).toContain('query.run')
    expect(
      panel!.querySelector<HTMLAnchorElement>('a[href*="/admin/audit"]')?.getAttribute('href'),
    ).toContain('user_id=2')

    // clean draft: nothing to save yet
    expect(panel!.textContent).not.toContain('Unsaved changes')
    const saveBtn = () =>
      findButton(document.body.querySelector('.drawer-panel')!, 'Save changes')
    expect(saveBtn().hasAttribute('disabled')).toBe(true)

    // toggle a grant — staged, not written
    const sales = Array.from(
      document.body.querySelectorAll<HTMLInputElement>('.ds-check-input'),
    ).find((i) => i.getAttribute('aria-label') === 'sales')!
    sales.checked = false
    sales.dispatchEvent(new Event('change', { bubbles: true }))
    await flushPromises()
    expect(apiPut).not.toHaveBeenCalled()
    expect(bodyText()).toContain('Unsaved changes')
    expect(saveBtn().hasAttribute('disabled')).toBe(false)

    saveBtn().click()
    await flushPromises()
    expect(apiPut).toHaveBeenCalledWith('/v1/admin/users/2/datasources', {
      datasources: ['demo'],
    })
    expect(bodyText()).not.toContain('Unsaved changes')
  })

  it('guards the drawer close while a draft is unsaved', async () => {
    mockApi()
    const view = await mountView()
    await view.findAll('.dt-row')[0].trigger('click')
    await flushPromises()
    const name = document.body.querySelector<HTMLInputElement>('.drawer-panel input')!
    name.value = 'Renamed'
    name.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()

    ;(document.body.querySelector('.drawer-close') as HTMLElement).click()
    await flushPromises()
    expect(bodyText()).toContain('Discard unsaved changes?')
    expect(document.body.querySelector('.drawer-panel')).not.toBeNull()

    findButton(document.body, 'Keep editing').click()
    await flushPromises()
    expect(document.body.querySelector('.drawer-panel')).not.toBeNull()
  })

  it('confirms deletion with the blast radius, then deletes', async () => {
    mockApi()
    const view = await mountView()
    // row 2 = lin.wang: 1 active token (plus 1 revoked), 2 datasource grants
    view.findAll('.dt-row')[1].find('.icon-btn.is-danger').element.click()
    await flushPromises()

    const confirm = document.body.querySelector('.confirm-panel')
    expect(confirm).not.toBeNull()
    expect(confirm!.textContent).toContain('Delete this user')
    // revoked tokens are already inert — they must not inflate the count
    expect(confirm!.textContent).toContain('Active API tokens: 1')
    expect(confirm!.textContent).toContain('Datasource grants: 2')
    expect(confirm!.textContent).toContain('not affected')
    expect(apiDelete).not.toHaveBeenCalled()

    findButton(confirm as ParentNode, 'Delete this user').click()
    await flushPromises()
    expect(apiDelete).toHaveBeenCalledWith('/v1/admin/users/2')
  })

  it('validates the create dialog per field and posts a valid user', async () => {
    mockApi()
    const view = await mountView()
    view.find('.ph-actions button').element.click()
    await flushPromises()
    const dialog = document.body.querySelector('.el-dialog') as HTMLElement
    expect(dialog).not.toBeNull()

    const submit = () => findButton(document.body.querySelector('.el-dialog')!, 'Create user')
    submit().click()
    await settle()
    expect(apiPost).not.toHaveBeenCalled()
    expect(bodyText()).toContain('Username is required')
    expect(bodyText()).toContain('Password is required')

    const inputs = () => Array.from(document.querySelectorAll<HTMLInputElement>('.el-dialog input'))
    const type = async (pick: (list: HTMLInputElement[]) => HTMLInputElement, value: string) => {
      const el = pick(inputs())
      el.value = value
      el.dispatchEvent(new Event('input', { bubbles: true }))
      await flushPromises()
    }
    const typeUser = (v: string) => type((l) => l[0], v)
    const typePass = (v: string) => type((l) => l.find((i) => i.type === 'password')!, v)

    await typeUser('ca rol')
    await typePass('short')
    submit().click()
    await settle()
    expect(apiPost).not.toHaveBeenCalled()
    expect(bodyText()).toContain('Password must be at least 8 characters')

    await typePass('longenough1')
    submit().click()
    await settle()
    expect(apiPost).not.toHaveBeenCalled()
    expect(bodyText()).toContain('Username cannot contain spaces')

    // a duplicate from the server lands under the field, not in a toast
    await typeUser('carol')
    ;(apiPost as any).mockRejectedValueOnce(new ApiError(400, 'user already exists: carol'))
    submit().click()
    await settle()
    expect(bodyText()).toContain('user already exists: carol')

    await typeUser('carol')
    ;(apiPost as any).mockResolvedValueOnce({})
    submit().click()
    await settle()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/users', {
      username: 'carol',
      display_name: '',
      password: 'longenough1',
      role: 'user',
    })
  })

  /* ── the four list states ───────────────────────────────────────────── */

  it('shows skeleton rows while the first page loads', async () => {
    mockApi({
      list: (path) =>
        path.includes('limit=1') ? serverList(path) : new Promise(() => {}),
    })
    const view = await mountView()
    expect(view.findAll('.dt-skel-row')).toHaveLength(6)
    expect(view.find('.dt-row').exists()).toBe(false)
  })

  it('shows the first-run empty state with a create CTA', async () => {
    USERS = []
    mockApi()
    const view = await mountView()
    expect(view.text()).toContain('No accounts yet')
    expect(findButton(view.element, 'Create user')).toBeTruthy()
  })

  it('shows the filtered empty state with a way back', async () => {
    mockApi()
    const view = await mountView('/admin/users?q=zzz')
    expect(view.text()).toContain('No matching users')
    const clear = findButton(view.element, 'Clear filters')
    clear.click()
    await flushPromises()
    expect('q' in router.currentRoute.value.query).toBe(false)
    expect(view.findAll('.dt-row')).toHaveLength(3)
  })

  it('shows an error state with the raw detail and retries', async () => {
    let fail = true
    mockApi({
      list: (path) => {
        if (path.includes('limit=1')) return serverList(path)
        if (fail) throw new Error('boom: 500')
        return serverList(path)
      },
    })
    const view = await mountView()
    expect(view.text()).toContain('Could not load users')
    expect(view.text()).toContain('boom: 500')
    expect(view.find('.dt-table').exists()).toBe(false)

    fail = false
    findButton(view.element, 'Retry').click()
    await flushPromises()
    expect(view.findAll('.dt-row')).toHaveLength(3)
    expect(view.text()).not.toContain('Could not load users')
  })
})
