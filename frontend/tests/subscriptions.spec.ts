/**
 * 订阅面(定时报告)—— JobsView 订阅抽屉 + 用户端「我的订阅」的契约测试。
 *
 * 覆盖三件"错了也没人看得出来"的事:
 *   · 订阅列表按 job 过滤(抽屉是任务级的,拿到全量会串任务);
 *   · 添加订阅的 body 带全 channel/mode(后端字段可选,但"空通道 = 沿用
 *     任务通道"正是要的语义,不能靠恰好没传);
 *   · 投递记录是事后取证:失败行必须把 error 一并带出来。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createMemoryHistory, createRouter, type RouteRecordRaw } from 'vue-router'
import JobsView from '../src/views/admin/JobsView.vue'
import SubscriptionsView from '../src/views/SubscriptionsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}))

import { apiGet, apiPost, apiDelete } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

const JOB = {
  id: 'job-1',
  name: 'Revenue watch',
  question: 'Weekly revenue',
  datasource: 'financial',
  workflow: 'reflection',
  schedule_type: 'interval',
  schedule: '30',
  enabled: true,
  alert_expr: 'revenue < 100',
  alert_channel: 'ops-alerts',
  alert_cooldown_min: 30,
  decision_rule: '',
  next_run_at: '',
  created_at: '2026-10-01T08:00:00Z',
  updated_at: '2026-10-01T08:00:00Z',
  recent_run: null,
}

function sub(over: Record<string, unknown> = {}) {
  return {
    id: 's1',
    job_id: 'job-1',
    job_name: 'Revenue watch',
    subscriber: 'alice',
    channel: '',
    mode: 'always',
    enabled: true,
    created_by: 'admin',
    created_at: '2026-10-01T09:00:00Z',
    updated_at: '2026-10-01T09:00:00Z',
    ...over,
  }
}

function delivery(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    subscription_id: 's1',
    job_id: 'job-1',
    run_id: 7,
    subscriber: 'alice',
    channel: 'ops-alerts',
    status: 'sent',
    error: '',
    excerpt: 'Revenue fell past the threshold',
    created_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

let wrapper: VueWrapper | null = null
let router: ReturnType<typeof createRouter>

beforeEach(() => {
  setActivePinia(createPinia())
  useUiStore().lang = 'en'
  vi.clearAllMocks()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

function makeRouter(url: string): ReturnType<typeof createRouter> {
  const routes: RouteRecordRaw[] = [
    { path: '/', name: 'chat', component: { render: () => null } },
    { path: '/admin', component: { render: () => null } },
    { path: '/admin/jobs', name: 'admin-jobs', component: { render: () => null } },
    {
      path: '/subscriptions',
      name: 'subscriptions',
      component: { render: () => null },
    },
  ]
  const r = createRouter({ history: createMemoryHistory(), routes })
  void r.push(url)
  return r
}

function findButton(root: ParentNode, text: string): HTMLButtonElement {
  const btn = Array.from(root.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  )
  if (!btn) throw new Error(`button not found: ${text}`)
  return btn as HTMLButtonElement
}

function drawer(): HTMLElement {
  const el = document.querySelector('.drawer-panel')
  if (!el) throw new Error('drawer is not open')
  return el as HTMLElement
}

describe('JobsView — subscription drawer', () => {
  async function mountJobs() {
    ;(apiGet as any).mockImplementation(async (url: string) => {
      if (url.startsWith('/v1/admin/jobs')) return { jobs: [JOB] }
      if (url.startsWith('/v1/catalog/datasources')) return { datasources: [] }
      if (url.startsWith('/v1/admin/subscriptions')) {
        return { subscriptions: [sub(), sub({ id: 's2', subscriber: 'bob', mode: 'alert_only' })], total: 2 }
      }
      if (url.startsWith('/v1/admin/deliveries')) {
        return {
          deliveries: [
            delivery(),
            delivery({ id: 2, status: 'failed', error: 'channel reported failure' }),
          ],
          total: 2,
        }
      }
      return {}
    })
    router = makeRouter('/admin/jobs')
    await router.isReady()
    wrapper = mount(JobsView, {
      global: { plugins: [router, ElementPlus] },
      attachTo: document.body,
    })
    await flushPromises()
    return wrapper
  }

  it('lists the job’s subscribers and its delivery log', async () => {
    const view = await mountJobs()
    findButton(view.element, 'Subscriptions').click()
    await flushPromises()

    // 抽屉是任务级的:列表必须按 job_id 过滤,不能拿全量。
    const urls = (apiGet as any).mock.calls.map((c: string[]) => c[0])
    expect(urls).toContain('/v1/admin/subscriptions?job_id=job-1')
    expect(urls).toContain('/v1/admin/deliveries?job_id=job-1&limit=50')

    const text = drawer().textContent ?? ''
    expect(text).toContain('alice')
    expect(text).toContain('bob')
    expect(text).toContain('Alerts only')
    // 空通道不是缺省显示,而是"沿用任务通道"这条语义。
    expect(text).toContain('Inherits the job channel')
    // 投递取证:摘要 + 失败原因都要看得见。
    expect(text).toContain('Revenue fell past the threshold')
    expect(text).toContain('channel reported failure')
  })

  it('adds a subscriber with an explicit channel/mode payload', async () => {
    const view = await mountJobs()
    findButton(view.element, 'Subscriptions').click()
    await flushPromises()
    ;(apiPost as any).mockResolvedValue({ subscription: sub() })

    const input = drawer().querySelector('.subs-in-name input') as HTMLInputElement
    expect(input).toBeTruthy()
    input.value = 'carol'
    input.dispatchEvent(new Event('input'))
    await flushPromises()

    findButton(drawer(), 'Add subscriber').click()
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/v1/admin/jobs/job-1/subscriptions', {
      subscriber: 'carol',
      channel: '',
      mode: 'always',
    })
  })

  it('refuses an empty subscriber before it reaches the API', async () => {
    const view = await mountJobs()
    findButton(view.element, 'Subscriptions').click()
    await flushPromises()

    findButton(drawer(), 'Add subscriber').click()
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('SubscriptionsView — my subscriptions', () => {
  async function mountMy(rows = [sub(), sub({ id: 's2', job_id: 'job-2', job_name: '', channel: 'webhook:https://x.test/h' })]) {
    ;(apiGet as any).mockImplementation(async (url: string) => {
      if (url.startsWith('/v1/subscriptions/')) {
        return { deliveries: [delivery()], total: 1 }
      }
      if (url.startsWith('/v1/subscriptions')) {
        return { subscriptions: rows, total: rows.length }
      }
      return {}
    })
    router = makeRouter('/subscriptions')
    await router.isReady()
    wrapper = mount(SubscriptionsView, {
      global: {
        plugins: [router, ElementPlus],
        // 侧栏不属于本页契约(会话列表有自己的一摊取数),stub 掉。
        stubs: { Sidebar: true },
      },
      attachTo: document.body,
    })
    await flushPromises()
    return wrapper
  }

  it('lists my subscriptions with mode, channel and job fallback', async () => {
    const view = await mountMy()
    const text = view.text()
    expect(text).toContain('Revenue watch')
    expect(text).toContain('Every run')
    expect(text).toContain('Inherits the job channel')
    expect(text).toContain('webhook:https://x.test/h')
    // job 已删(job_name 空)时退到 job_id,而不是显示空白任务名。
    expect(text).toContain('job-2')
  })

  it('opens the delivery history through my own endpoint', async () => {
    const view = await mountMy()
    findButton(view.element, 'Deliveries').click()
    await flushPromises()
    expect(apiGet).toHaveBeenCalledWith('/v1/subscriptions/s1/deliveries?limit=50')
    expect(drawer().textContent).toContain('Revenue fell past the threshold')
  })

  it('unsubscribes through my own endpoint after confirmation', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as any)
    ;(apiDelete as any).mockResolvedValue(undefined)
    const view = await mountMy()

    findButton(view.element, 'Unsubscribe').click()
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalled()
    expect(apiDelete).toHaveBeenCalledWith('/v1/subscriptions/s1')
  })
})
