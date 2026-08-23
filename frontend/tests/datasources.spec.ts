import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
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

function mountView() {
  wrapper = mount(DatasourcesView, {
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
  return wrapper
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
    ;(apiGet as any).mockResolvedValue({
      datasources: [
        { name: 'financial', type: 'mysql', default: true, status: 'connected', kb_initialized: true, kb_items: { schema_notes: 12 } },
        { name: 'demo', type: 'demo', status: 'disconnected', kb_initialized: false, kb_items: {} },
      ],
    })
    const view = mountView()
    await flushPromises()
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
    ;(apiGet as any).mockResolvedValue({ datasources: [] })
    ;(apiPost as any).mockResolvedValue({ datasource: { name: 'newds' } })
    const view = mountView()
    await flushPromises()

    // opens the register dialog (empty state CTA)
    await view.find('button.add').trigger('click')
    await flushPromises()

    // switch type to MySQL → URL field appears
    const select = view.findAllComponents({ name: 'ElSelect' })[0]
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
    ;(apiGet as any).mockResolvedValue({ datasources: [] })
    ;(apiPost as any).mockResolvedValue({ datasource: { name: 'demo' } })
    const view = mountView()
    await flushPromises()
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
    ;(apiGet as any).mockResolvedValue({ datasources: [{ name: 'financial', type: 'mysql', status: 'connected', kb_initialized: false }] })
    ;(apiPost as any).mockResolvedValue({ ok: true, error: null })
    const view = mountView()
    await flushPromises()
    await view.find('button.test').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources/test-connection', { name: 'financial' })
  })

  it('disables edit once the KB is initialized', async () => {
    ;(apiGet as any).mockResolvedValue({ datasources: [
      { name: 'financial', type: 'mysql', status: 'connected', kb_initialized: true },
      { name: 'demo', type: 'demo', status: 'connected', kb_initialized: false },
    ] })
    const view = mountView()
    await flushPromises()
    const editBtns = view.findAll('button.edit')
    expect(editBtns.length).toBe(2)
    expect(editBtns[0].attributes('disabled')).toBeDefined() // KB locked
    expect(editBtns[1].attributes('disabled')).toBeDefined() // demo locked
  })

  it('edits a datasource connection through the dialog', async () => {
    ;(apiGet as any)
      .mockResolvedValueOnce({ datasources: [{ name: 'financial', type: 'mysql', status: 'connected', kb_initialized: false }] })
      .mockResolvedValueOnce({ datasource: { name: 'financial', type: 'mysql', url: 'mysql://user@localhost:3306/financial', status: 'connected', kb_initialized: false } })
    ;(apiPut as any).mockResolvedValue({ datasource: {} })
    const view = mountView()
    await flushPromises()

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