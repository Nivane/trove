/**
 * ActionsView / api.actions —— 行动支柱前端的契约测试。
 *
 * 覆盖四件"错了也没人看得出来"的事:
 *   · 未闭环筛选必须扇出三个状态请求("取全部再前端过滤"会被列表上限
 *     静默切掉最老的那条);
 *   · 决策请求永远带 `{comment}`(后端 body 可选,但驳回理由只能从这里进
 *     审批轨迹);
 *   · 驳回必须非空理由(客户端拦一道,不然轨迹里只剩一条无理由的终态);
 *   · 载荷冻结:详情抽屉展示的就是 proposals.payload 本身。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type RouteRecordRaw } from 'vue-router'
import ActionsView from '../src/views/admin/ActionsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

import { apiGet, apiPost } from '../src/api/http'
import { fetchOpenActionProposals } from '../src/api/actions'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

const TEMPLATE = {
  name: 'notify-ops',
  title: 'Notify ops',
  description: 'd',
  status: 'pending',
  action_type: 'notify',
  target: { channel: 'ops-alerts', resource: '#ops' },
  risk: 'high',
  approvals_required: 1,
  payload_template: '{"rule":"{{rule_id}}"}',
  source: 'admin',
  created_at: '2026-10-01T08:00:00Z',
  updated_at: '2026-10-01T08:00:00Z',
  digest: 'abc123',
}

function proposal(over: Record<string, unknown> = {}) {
  return {
    id: 'p1',
    datasource: 'financial',
    rule_id: 'revenue-drop',
    rule_digest: 'd1',
    run_id: 7,
    job_id: 'job-1',
    origin_kind: 'verdict',
    action_type: 'notify',
    template: 'notify-ops',
    template_digest: 't1',
    target: { channel: 'ops-alerts' },
    payload: { rule: 'revenue-drop', message: 'delta -12%' },
    rationale: 'Revenue fell past the threshold',
    evidence_refs: { run_id: 7 },
    severity: 'warning',
    priority: 2,
    risk: 'medium',
    status: 'pending',
    idempotency_key: 'k-1',
    created_by: 'system',
    created_at: '2026-10-01T09:00:00Z',
    decided_at: '',
    expires_at: '2026-10-04T09:00:00Z',
    dispatched_at: '',
    attempts: 0,
    error: '',
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

async function mountView(url = '/admin/actions') {
  const routes: RouteRecordRaw[] = [
    { path: '/', name: 'chat', component: { render: () => null } },
    { path: '/admin', component: { render: () => null } },
    { path: '/admin/actions', name: 'admin-actions', component: { render: () => null } },
  ]
  router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(url)
  await router.isReady()
  wrapper = mount(ActionsView, {
    global: { plugins: [router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

function findButton(root: ParentNode, text: string): HTMLButtonElement {
  const btn = Array.from(root.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  )
  if (!btn) throw new Error(`button not found: ${text}`)
  return btn as HTMLButtonElement
}

/** 对话框按钮在 teleport 后的 footer 里,和行内同名按钮要分开找。 */
function dialogButton(text: string): HTMLButtonElement {
  const footer = document.querySelector('.el-dialog__footer')
  if (!footer) throw new Error('dialog is not open')
  return findButton(footer, text)
}

/** 抽屉同样 teleport 到 body;页面里有几个抽屉(预览/详情),只取打开的那个。 */
function drawerButton(text: string): HTMLButtonElement {
  const drawer = Array.from(document.querySelectorAll('.el-drawer')).find(
    (d) => !(d.parentElement as HTMLElement | null)?.style.display.includes('none'),
  )
  if (!drawer) throw new Error('drawer is not open')
  return findButton(drawer, text)
}

describe('fetchOpenActionProposals', () => {
  it('fans out over the three open statuses and merges newest-first', async () => {
    ;(apiGet as any).mockImplementation(async (url: string) => {
      if (url.includes('status=pending')) {
        return {
          proposals: [proposal({ id: 'old', created_at: '2026-10-01T01:00:00Z' })],
          counts: { pending: 1, approved: 1, failed: 1 },
          enabled: true,
        }
      }
      if (url.includes('status=approved')) {
        return {
          proposals: [proposal({ id: 'newest', created_at: '2026-10-03T01:00:00Z' })],
          counts: { pending: 1, approved: 1, failed: 1 },
          enabled: true,
        }
      }
      return {
        proposals: [proposal({ id: 'middle', created_at: '2026-10-02T01:00:00Z' })],
        counts: { pending: 1, approved: 1, failed: 1 },
        enabled: true,
      }
    })
    const body = await fetchOpenActionProposals()
    expect((apiGet as any).mock.calls.map((c: string[]) => c[0])).toEqual([
      expect.stringContaining('status=pending'),
      expect.stringContaining('status=approved'),
      expect.stringContaining('status=failed'),
    ])
    expect(body.proposals.map((p) => p.id)).toEqual(['newest', 'middle', 'old'])
    expect(body.enabled).toBe(true)
  })
})

describe('ActionsView — templates', () => {
  it('lists templates and confirms through the template endpoint', async () => {
    ;(apiGet as any).mockResolvedValue({
      templates: [TEMPLATE],
      enabled: true,
      channels: ['ops-alerts'],
      sample_variables: ['rule_id', 'metric'],
    })
    ;(apiPost as any).mockResolvedValue({ name: 'notify-ops', status: 'confirmed', injection_hits: [] })

    const view = await mountView()
    expect(view.text()).toContain('notify-ops')
    expect(view.text()).toContain('High')
    expect(view.text()).toContain('Pending')

    findButton(view.element, 'Confirm').click()
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/actions/templates/notify-ops/confirm')
  })

  it('surfaces injection hits on confirm instead of closing silently', async () => {
    ;(apiGet as any).mockResolvedValue({
      templates: [TEMPLATE],
      enabled: true,
      channels: [],
      sample_variables: [],
    })
    ;(apiPost as any).mockResolvedValue({
      name: 'notify-ops',
      status: 'confirmed',
      injection_hits: ['ignore all previous instructions'],
    })

    const view = await mountView()
    findButton(view.element, 'Confirm').click()
    await flushPromises()
    expect(bodyText()).toContain('ignore all previous instructions')
  })

  it('renders enabled=false as a banner, not a page error', async () => {
    ;(apiGet as any).mockResolvedValue({
      templates: [TEMPLATE],
      enabled: false,
      channels: [],
      sample_variables: [],
    })
    const view = await mountView()
    expect(view.text()).toContain('The action layer is off')
    // 半可用:模板照常列出、照常可确认。
    expect(view.text()).toContain('notify-ops')
  })

  it('shows a broken template as unconfirmable and offers no confirm button', async () => {
    ;(apiGet as any).mockResolvedValue({
      templates: [{ ...TEMPLATE, error: 'invalid yaml at line 3' }],
      enabled: true,
      channels: [],
      sample_variables: [],
    })
    const view = await mountView()
    expect(view.text()).toContain('invalid yaml at line 3')
    const labels = view.findAll('button').map((b) => b.text())
    expect(labels.filter((l) => l === 'Confirm')).toHaveLength(0)
  })
})

describe('ActionsView — proposals', () => {
  const openPages = {
    pending: proposal({ id: 'p1', status: 'pending' }),
    approved: proposal({ id: 'p2', status: 'approved' }),
    failed: proposal({ id: 'p3', status: 'failed', error: 'timeout' }),
  }

  function mockList() {
    ;(apiGet as any).mockImplementation(async (url: string) => {
      const hit = (['pending', 'approved', 'failed'] as const).find((s) =>
        url.includes(`status=${s}`),
      )
      return {
        proposals: hit ? [openPages[hit]] : [],
        counts: { pending: 1, approved: 1, failed: 1 },
        enabled: true,
      }
    })
  }

  it('asks for the three open statuses and shows every row', async () => {
    mockList()
    const view = await mountView('/admin/actions?tab=proposals&status=open')
    const urls = (apiGet as any).mock.calls.map((c: string[]) => c[0])
    expect(urls.filter((u: string) => u.includes('/proposals?'))).toHaveLength(3)
    expect(view.text()).toContain('revenue-drop')
    expect(view.text()).toContain('3 open')
  })

  it('keeps "all statuses" in the URL instead of silently reverting to open', async () => {
    // `all` 是个能写进 URL 的真实值 —— 用空串表示"全部"会被 useListQuery
    // 当成"回默认",刷新一次就变回未闭环。
    mockList()
    await mountView('/admin/actions?tab=proposals&status=all')
    const urls = (apiGet as any).mock.calls.map((c: string[]) => c[0])
    const listUrls = urls.filter((u: string) => u.includes('/proposals?'))
    expect(listUrls).toHaveLength(1)
    expect(listUrls[0]).not.toContain('status=')
    expect(router.currentRoute.value.query.status).toBe('all')
  })

  it('posts the decision with a comment payload', async () => {
    mockList()
    ;(apiPost as any).mockResolvedValue({ proposal: proposal({ status: 'approved' }) })
    const view = await mountView('/admin/actions?tab=proposals&status=open')

    findButton(view.element, 'Approve').click()
    await flushPromises()
    const textarea = document.querySelector('.el-dialog textarea') as HTMLTextAreaElement
    expect(textarea).toBeTruthy()
    textarea.value = 'lgtm'
    textarea.dispatchEvent(new Event('input'))
    await flushPromises()
    dialogButton('Approve').click()
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/v1/admin/actions/proposals/p1/approve', {
      comment: 'lgtm',
    })
  })

  it('offers dry_run in the drawer and posts it like any other verb', async () => {
    // 预演是闭集动词之一(镜像后端 _DECISIONS)。它不该只在后端存在而前端
    // 够不着 —— 那样"预演"就只剩 API 调用者用得上。
    ;(apiGet as any).mockImplementation(async (url: string) => {
      if (url.includes('/proposals/p1')) {
        return {
          proposal: proposal({ status: 'pending' }),
          approvals: [],
          deliveries: [
            {
              proposal_id: 'p1',
              channel: 'ops-alerts',
              status: 'dry_run',
              http_status: null,
              response_excerpt: '{"rule":"revenue-drop"}',
              error: '',
              attempted_at: '2026-10-01T09:30:00Z',
            },
          ],
          stale: false,
        }
      }
      const hit = (['pending', 'approved', 'failed'] as const).find((s) =>
        url.includes(`status=${s}`),
      )
      return {
        proposals: hit ? [openPages[hit]] : [],
        counts: { pending: 1, approved: 1, failed: 1 },
        enabled: true,
      }
    })
    ;(apiPost as any).mockResolvedValue({ proposal: proposal({ status: 'pending' }) })
    const view = await mountView('/admin/actions?tab=proposals&status=open')

    findButton(view.element, 'Detail').click()
    await flushPromises()

    // 回执行里的预演行原样展示(不是绿色「成功」,也不是错误)。
    expect(bodyText()).toContain('dry_run')

    drawerButton('Dry run').click()
    await flushPromises()
    dialogButton('Dry run').click()
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/v1/admin/actions/proposals/p1/dry_run', {
      comment: '',
    })
  })

  it('refuses to submit a rejection without a reason', async () => {
    mockList()
    const view = await mountView('/admin/actions?tab=proposals&status=open')

    findButton(view.element, 'Reject').click()
    await flushPromises()
    expect(dialogButton('Reject').disabled).toBe(true)

    const textarea = document.querySelector('.el-dialog textarea') as HTMLTextAreaElement
    textarea.value = 'not our caliber'
    textarea.dispatchEvent(new Event('input'))
    await flushPromises()
    expect(dialogButton('Reject').disabled).toBe(false)
  })

  it('renders the frozen payload, approval trail and receipts in the drawer', async () => {
    ;(apiGet as any).mockImplementation(async (url: string) => {
      if (url.includes('/proposals/p1')) {
        return {
          proposal: proposal({ status: 'dispatched' }),
          approvals: [
            { proposal_id: 'p1', user_id: 'admin', action: 'approve', comment: 'go', created_at: '2026-10-01T10:00:00Z' },
          ],
          deliveries: [
            { proposal_id: 'p1', channel: 'ops-alerts', status: 'sent', http_status: 200, response_excerpt: 'ok', error: '', attempted_at: '2026-10-01T10:01:00Z' },
          ],
          stale: false,
        }
      }
      const hit = (['pending', 'approved', 'failed'] as const).find((s) =>
        url.includes(`status=${s}`),
      )
      return {
        proposals: hit ? [openPages[hit]] : [],
        counts: { pending: 1, approved: 1, failed: 1 },
        enabled: true,
      }
    })
    const view = await mountView('/admin/actions?tab=proposals&status=open')

    findButton(view.element, 'Detail').click()
    await flushPromises()

    const text = bodyText()
    expect(text).toContain('delta -12%') // 冻结载荷本身
    expect(text).toContain('admin') // 审批轨迹
    expect(text).toContain('ops-alerts') // 回执
    expect(text).toContain('200')
  })
})
