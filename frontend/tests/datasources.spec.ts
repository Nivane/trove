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

import { apiGet, apiPost, apiDelete } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'

beforeEach(() => {
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
})

describe('DatasourcesView', () => {
  it('renders datasources with kb status', async () => {
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

  it('init button confirms then POSTs', async () => {
    ;(apiGet as any).mockResolvedValue({ datasources: [{ name: 'financial', type: 'mysql', status: 'connected', kb_initialized: false }] })
    ;(apiPost as any).mockResolvedValue({ summary: 'Initialized' })
    const wrapper = mount(DatasourcesView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper.find('button.init').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/datasources/financial/kb/init', {})
  })
})
