import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import UsersView from '../src/views/admin/UsersView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiPut: vi.fn(),
}))

// prompt dialogs gated behind mocks — no real dialogs in jsdom
vi.mock('element-plus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('element-plus')>()
  return { ...actual, ElMessageBox: { confirm: vi.fn(), prompt: vi.fn() } }
})

import { apiGet, apiPost, apiDelete, apiPut } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

const base = {
  users: [
    { id: 1, username: 'admin', display_name: 'Administrator', role: 'admin', disabled: false, created_at: '2026-01-01T08:00:00Z' },
    { id: 2, username: 'bob', display_name: 'Bob', role: 'user', disabled: true, created_at: '2026-02-01T08:00:00Z' },
  ],
}

function mockApi(overrides: Record<string, unknown> = {}) {
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path === '/v1/admin/users') return base
    if (path === '/v1/catalog/datasources') return { datasources: [{ name: 'demo' }] }
    if (path.includes('/datasources')) return { datasources: [] }
    if (path.includes('/tokens')) return { tokens: [] }
    return overrides[path] ?? {}
  })
}

let wrapper: VueWrapper | null = null

function mountView() {
  wrapper = mount(UsersView, {
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
  return wrapper
}

function bodyDialogs(): HTMLElement[] {
  return Array.from(document.body.querySelectorAll<HTMLElement>('.el-dialog'))
}

async function openDialog(text: string) {
  const view = mountView()
  await flushPromises()
  const btn = view
    .findAll('button')
    .find((b) => b.text().includes(text))
  await btn!.trigger('click')
  await flushPromises()
  return view
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

describe('UsersView', () => {
  it('renders users with role, status pills and created time', async () => {
    mockApi()
    const view = mountView()
    await flushPromises()
    const text = view.text()
    expect(text).toContain('admin')
    expect(text).toContain('Administrator')
    expect(text).toContain('bob')
    expect(text).toContain('Disabled')
    expect(text).toContain('Active')
    expect(text).toContain('2026/01/01')
  })

  it('creates a user via POST with the dialog role', async () => {
    mockApi()
    ;(apiPost as any).mockResolvedValue({})
    await openDialog('Create user')
    const dialog = bodyDialogs()[0]
    const inputs = dialog.querySelectorAll('input')
    const username = Array.from(inputs).find(
      (i) => !i.getAttribute('type')?.includes('password') && !i.disabled,
    )
    const password = Array.from(inputs).find((i) => i.type === 'password')
    await setInput(username!, 'carol')
    await setInput(password!, 'pw')
    const confirm = Array.from(
      dialog.querySelectorAll('button'),
    ).find((b) => b.textContent!.includes('Confirm') || b.textContent!.includes('OK'))!
    confirm.dispatchEvent(new MouseEvent('click'))
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/v1/admin/users',
      expect.objectContaining({ username: 'carol', password: 'pw', role: 'user' }),
    )
  })

  it('saves datasource grants via PUT on change', async () => {
    mockApi()
    ;(apiPut as any).mockResolvedValue({})
    const view = mountView()
    await flushPromises()
    const selects = view.findAllComponents({ name: 'ElSelect' })
    expect(selects.length).toBeGreaterThan(0)
    selects[0].vm.$emit('change', ['demo'])
    await flushPromises()
    expect(apiPut).toHaveBeenCalledWith('/v1/admin/users/1/datasources', {
      datasources: ['demo'],
    })
  })

  it('opens token management and revokes a token', async () => {
    ;(apiGet as any).mockImplementation(async (path: string) => {
      if (path === '/v1/admin/users') return base
      if (path === '/v1/catalog/datasources') return { datasources: [] }
      if (path.includes('/datasources')) return { datasources: [] }
      if (path.includes('/tokens')) {
        return {
          tokens: [
            { id: 7, label: 'ci', revoked: 0, created_at: '2026-01-01T00:00:00Z' },
          ],
        }
      }
      return {}
    })
    ;(apiDelete as any).mockResolvedValue(undefined)
    await openDialog('API tokens')
    expect(document.body.textContent).toContain('ci')
    const revoke = Array.from(
      document.body.querySelectorAll<HTMLElement>('.el-dialog button'),
    ).find((b) => b.textContent!.includes('Revoke'))!
    revoke.click()
    await flushPromises()
    expect(apiDelete).toHaveBeenCalledWith('/v1/admin/tokens/7')
  })
})

async function setInput(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    'value',
  )!.set!
  setter.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}