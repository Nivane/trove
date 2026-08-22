import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import DatasourcesView from '../src/views/admin/DatasourcesView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
}))

// Confirm dialogs gate billed/irreversible actions — stub the real dialog,
// keep the ElementPlus plugin (default export) intact for component mounts.
vi.mock('element-plus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('element-plus')>()
  return { ...actual, ElMessageBox: { confirm: vi.fn() } }
})

import { apiGet, apiPost, apiDelete } from '../src/api/http'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'

beforeEach(() => {
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
})

describe('DatasourcesView', () => {
  it('renders datasources with status and kb labels', async () => {
    ;(apiGet as any).mockResolvedValue({
      datasources: [
        { name: 'financial', type: 'mysql', default: true, status: 'connected', kb_initialized: true, kb_items: { schema_notes: 12 } },
        { name: 'demo', type: 'demo', status: 'disconnected', kb_initialized: false, kb_items: {} },
      ],
    })
    const wrapper = mount(DatasourcesView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('financial')
    expect(wrapper.text()).toContain('demo')
    expect(wrapper.text()).toContain('Connected')
    expect(wrapper.text()).toContain('Disconnected')
    // KB status labels — initialized vs not
    expect(wrapper.text()).toContain('Initialized')
    expect(wrapper.text()).toContain('Not initialized')
    // default marker column
    expect(wrapper.text()).toContain('default')
  })

  it('registers a datasource via POST', async () => {
    ;(apiGet as any).mockResolvedValue({ datasources: [] })
    ;(apiPost as any).mockResolvedValue({ datasource: { name: 'newds' } })
    const wrapper = mount(DatasourcesView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper.find('input[placeholder*="name" i]').setValue('newds')
    await wrapper.find('input[placeholder*="url" i]').setValue('sqlite://:memory:')
    await wrapper.find('button.add').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources', { name: 'newds', url: 'sqlite://:memory:' })
  })

  it('init asks for confirmation, then POSTs only after confirm', async () => {
    ;(apiGet as any).mockResolvedValue({ datasources: [{ name: 'financial', type: 'mysql', status: 'connected', kb_initialized: false }] })
    ;(apiPost as any).mockResolvedValue({ summary: 'Initialized' })
    const wrapper = mount(DatasourcesView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    // cancelled — billed LLM call must not fire
    ;(ElMessageBox.confirm as any).mockRejectedValue(new Error('cancel'))
    await wrapper.find('button.init').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()

    // confirmed — POST fires
    ;(ElMessageBox.confirm as any).mockResolvedValue('confirm')
    await wrapper.find('button.init').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources/financial/kb/init', {})
  })
})
