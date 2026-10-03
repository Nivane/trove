/**
 * CheckpointsView — P6 §2.3 page header.
 *
 * This page has no §4.3 keys (it is reached with a session in hand, not
 * shared as a filtered list), so what is pinned here is the header contract
 * plus the confirm-dialog copy (the English `|| 'Cancel'` fallback is gone —
 * a missing key must fail loudly in tests, not ship English silently).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import CheckpointsView from '../src/views/admin/CheckpointsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

vi.mock('element-plus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('element-plus')>()
  return { ...actual, ElMessageBox: { confirm: vi.fn() } }
})

import { apiGet, apiPost } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

let wrapper: VueWrapper | null = null
let router: Router

beforeEach(() => {
  setActivePinia(createPinia())
  // W5: the resume action is gated on an authenticated admin (useReadOnly);
  // pin the real precondition so the admin path stays under test.
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  document.body.innerHTML = ''
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/admin/sessions/') && path.includes('/checkpoints')) {
      return {
        checkpoints: [
          {
            checkpoint_id: 'c1',
            step: 3,
            node: 'gen_sql',
            source: 'loop',
            ts: '2026-10-01T10:00:00Z',
            run_id: 'r1',
            state: { question: 'q', verdict: 'OK' },
          },
        ],
      }
    }
    return {
      sessions: [
        { session_id: 's1', title: 'loans', user_id: 'admin', updated_at: '2026-10-01T10:00:00Z' },
      ],
    }
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

async function mountView() {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/checkpoints', component: CheckpointsView },
    ],
  })
  await router.push('/admin/checkpoints')
  await router.isReady()
  wrapper = mount(CheckpointsView, {
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

describe('CheckpointsView page header (P6 §2.3)', () => {
  it('renders PageHeader with the root crumb and document.title', async () => {
    const view = await mountView()
    expect(view.find('h1').text()).toBe('Checkpoints')
    const crumbs = view.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Checkpoints'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(document.title).toBe('Checkpoints')
  })

  it('uses the localized cancel copy in the resume confirm, without an English fallback', async () => {
    useUiStore().lang = 'zh'
    const view = await mountView()
    // pick a session so a row (and its Resume button) exists
    const select = view.findComponent({ name: 'ElSelect' })
    select.vm.$emit('update:modelValue', 's1')
    select.vm.$emit('change', 's1')
    await flushPromises()
    ;(apiPost as any).mockResolvedValue({ summary: {} })
    ;(ElMessageBox.confirm as any).mockResolvedValue('confirm')
    const resume = view
      .findAll('button')
      .find((b) => b.text().includes('从此续跑'))!
    await resume.trigger('click')
    await flushPromises()
    expect(ElMessageBox.confirm).toHaveBeenCalledWith(
      '从该断点重新执行之后的管线?',
      '从此续跑',
      expect.objectContaining({ cancelButtonText: '取消' }),
    )
  })
})
