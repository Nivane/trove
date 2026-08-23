import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import ModelConfigView from '../src/views/admin/ModelConfigView.vue'

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
  'llm.fast_model': 'deepseek/deepseek-chat',
  'llm.providers': [
    {
      name: 'deepseek',
      has_api_key: true,
      litellm_params: { api_key: MASK, api_base: 'https://api.deepseek.com' },
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
  wrapper = mount(ModelConfigView, {
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

describe('ModelConfigView', () => {
  it('renders model inputs and provider rows with secrets blanked', async () => {
    const view = await mountView()
    await view.vm.$nextTick()
    const model = view
      .findAll('input')
      .find(
        (i) => (i.element as HTMLInputElement).value === 'deepseek/deepseek-reasoner',
      )
    expect(model).toBeTruthy()
    const fast = view
      .findAll('input')
      .find(
        (i) => (i.element as HTMLInputElement).value === 'deepseek/deepseek-chat',
      )
    expect(fast).toBeTruthy()
    // provider rows render with the secret blanked out
    const nameInputs = view.findAll('.provider-name input')
    expect(nameInputs.length).toBe(1)
    expect((nameInputs[0].element as HTMLInputElement).value).toBe('deepseek')
    expect(view.text()).not.toContain('sk-')
  })

  it('shows the env-fallback note', async () => {
    const view = await mountView()
    expect(view.text()).toContain('环境变量')
  })

  it('sends only changed model scalars and keeps provider secrets masked', async () => {
    const view = await mountView()
    await view.vm.$nextTick()
    ;(apiPut as any).mockResolvedValue({ values: baseValues })

    // edit the fast model — empty → deepseek/deepseek-chat? keep as is; change default
    const defaultInput = view
      .findAll('input')
      .find(
        (i) => (i.element as HTMLInputElement).value === 'deepseek/deepseek-reasoner',
      )
    await defaultInput!.setValue('deepseek/deepseek-v3')
    await flushPromises()

    await view.find('.view-header-right .el-button--primary').trigger('click')
    await flushPromises()

    expect(apiPut).toHaveBeenCalledTimes(1)
    const payload = (apiPut as any).mock.calls[0][1] as { values: Record<string, unknown> }
    const values = payload.values
    expect(values['llm.default_model']).toBe('deepseek/deepseek-v3')
    // unchanged fast model is not sent
    expect(values['llm.fast_model']).toBeUndefined()
    // providers always round-trip; the kept api_key stays the mask sentinel
    const providers = values['llm.providers'] as {
      name: string
      litellm_params: { api_key: string }
    }[]
    expect(providers[0].name).toBe('deepseek')
    expect(providers[0].litellm_params.api_key).toBe(MASK)
  })
})
