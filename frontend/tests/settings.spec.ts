import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ElSwitch } from 'element-plus'
import ElementPlus from 'element-plus'
import SettingsView from '../src/views/admin/SettingsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiPut: vi.fn(),
}))

import { apiGet, apiPut } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

const MASK = '__trove_masked_key__'

const baseValues = {
  'llm.default_model': 'deepseek/deepseek-reasoner',
  'llm.fast_model': '',
  'llm.providers': [
    {
      name: 'openai',
      has_api_key: true,
      litellm_params: { api_key: MASK, api_base: 'https://api.openai.com' },
    },
  ],
  'app.language': 'zh',
  'app.date_parser': true,
  'app.explain_semantics': true,
  'app.fast_path': true,
  'app.reflect_skip': 'standard',
  'app.hitl': false,
  'app.insights': true,
  'app.result_cache': false,
  'app.decompose_llm_judge': true,
  'retention.max_sessions_per_user': 100,
  'retention.active_grace_min': 10,
  'retention.max_checkpoints_per_thread': 50,
  'retention.sweep_interval_hours': 24,
}

let wrapper: VueWrapper | null = null

async function mountView() {
  ;(apiGet as any).mockResolvedValue({ values: baseValues, mask: MASK })
  wrapper = mount(SettingsView, {
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'zh'
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('SettingsView', () => {
  it('loads and renders the current runtime values', async () => {
    const view = await mountView()
    await view.vm.$nextTick()
    // the language selector reflects the current value
    const langSelect = view.find('.settings-select')
    expect(langSelect.exists()).toBeTruthy()
    // model config is no longer on this page (moved to ModelConfigView)
    expect(view.findAll('.provider-name input').length).toBe(0)
    expect(view.text()).not.toContain('deepseek/deepseek-reasoner')
  })

  it('sends only changed scalars and never touches model keys', async () => {
    const view = await mountView()
    await view.vm.$nextTick()
    ;(apiPut as any).mockResolvedValue({ values: baseValues })

    // flip the 结果缓存 (result_cache) switch — false → true
    const cacheRow = view.findAll('.switch-row').find((r) => r.text().includes('结果缓存'))
    expect(cacheRow).toBeTruthy()
    const cacheSwitch = cacheRow!.findComponent(ElSwitch)
    await cacheSwitch.vm.$emit('update:modelValue', true)
    await cacheSwitch.vm.$emit('change', true)
    await flushPromises()

    await view.find('.settings-footer .el-button--primary').trigger('click')
    await flushPromises()

    expect(apiPut).toHaveBeenCalledTimes(1)
    const payload = (apiPut as any).mock.calls[0][1] as { values: Record<string, unknown> }
    const values = payload.values
    // the toggled scalar lands in the payload
    expect(values['app.result_cache']).toBe(true)
    // model/provider keys are managed by the separate ModelConfigView
    expect(values['llm.default_model']).toBeUndefined()
    expect(values['llm.providers']).toBeUndefined()
  })

  it('renders and saves the semantic layer path', async () => {
    const view = await mountView()
    await view.vm.$nextTick()
    const group = view.findAll('.settings-card').find((c) => c.text().includes('语义层'))
    expect(group).toBeTruthy()

    const input = group!.find('input')
    await input.setValue('.trove/semantic')
    await flushPromises()
    ;(apiPut as any).mockResolvedValue({ values: { ...baseValues, 'app.semantic_layer_path': '.trove/semantic' } })

    await view.find('.settings-footer .el-button--primary').trigger('click')
    await flushPromises()
    expect(apiPut).toHaveBeenCalledTimes(1)
    const payload = (apiPut as any).mock.calls[0][1] as { values: Record<string, unknown> }
    expect(payload.values['app.semantic_layer_path']).toBe('.trove/semantic')
  })
})