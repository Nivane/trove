/**
 * W5 阶段二 —— analyst 只读面的**视图级门控**（设计稿 §2.2 R3）。
 *
 * tests/analyst.spec.ts 钉的是契约（navModel ↔ 路由 meta ↔ 后端守卫）与
 * useReadOnly 本身；这里钉的是页面真的按它渲染：
 *
 *   · 写动作不渲染（而不是渲染了再吃 403）—— analyst 看不到
 *     新建/运行/编辑/删除/恢复，开关禁用；
 *   · 只读下仍保留的读动作不误伤 —— 运行历史、检查点详情照常可开；
 *   · 指向只读面之外页面的深链不留死链（TodoQueue 的「管理员专属」、
 *     InboxTable 的编辑深链过滤）。
 *
 * 每个视图都配一条 admin 对照：证明门控是「对 analyst 收窄」，
 * 不是「对所有人都关掉」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import {
  createMemoryHistory,
  createRouter,
  type RouteRecordRaw,
  type Router,
} from 'vue-router'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}))

import { apiGet } from '../src/api/http'
import { useAuthStore, type Role } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import JobsView from '../src/views/admin/JobsView.vue'
import CheckpointsView from '../src/views/admin/CheckpointsView.vue'
import TodoQueue from '../src/components/overview/TodoQueue.vue'
import InboxTable from '../src/components/governance/InboxTable.vue'
import type { OverviewTodos } from '../src/api/overview'
import type { GovernanceTodoItem } from '../src/api/types'

function setRole(role: Role) {
  useAuthStore().$patch({ token: 't', user: { id: 1, username: 'u', role } })
}

let wrapper: VueWrapper | null = null
let router: Router

async function mountWith(
  component: unknown,
  path: string,
  routes: RouteRecordRaw[] = [],
  props: Record<string, unknown> = {},
) {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/kb', component: { render: () => null } },
      { path: '/admin/governance', component: { render: () => null } },
      ...routes,
    ],
  })
  await router.push(path)
  await router.isReady()
  wrapper = mount(component as never, {
    props,
    global: { plugins: [ElementPlus, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  document.body.innerHTML = ''
  ;(apiGet as any).mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/admin/jobs')) {
      return {
        jobs: [
          {
            id: 'j1',
            name: 'daily loans',
            question: 'q',
            datasource: 'demo',
            workflow: 'reflection',
            schedule_type: 'interval',
            schedule: '60',
            enabled: true,
            alert_expr: '',
            alert_channel: '',
            alert_cooldown_min: 30,
            decision_rule: '',
            next_run_at: '2026-10-04T09:00:00Z',
            created_at: '2026-10-01T09:00:00Z',
            updated_at: '2026-10-01T09:00:00Z',
            recent_run: { status: 'ok' },
          },
        ],
        total: 1,
      }
    }
    if (path.startsWith('/v1/catalog/datasources')) {
      return { datasources: [{ name: 'demo', default: true }] }
    }
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
        {
          session_id: 's1',
          title: 'loans',
          user_id: 'admin',
          updated_at: '2026-10-01T10:00:00Z',
        },
      ],
    }
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

/* ── JobsView ─────────────────────────────────────────────────────────── */

async function mountJobs() {
  const view = await mountWith(JobsView, '/admin/jobs', [
    { path: '/admin/jobs', component: JobsView },
  ])
  return view
}

function rowButtonTexts(view: VueWrapper): string[] {
  return view
    .findAll('.el-table__body tbody tr')
    .flatMap((tr) => tr.findAll('button').map((b) => b.text()))
}

describe('JobsView 只读门控', () => {
  it('analyst：无新建/运行/编辑/删除/订阅，开关禁用；运行历史仍可打开', async () => {
    setRole('analyst')
    const view = await mountJobs()
    expect(view.find('.add').exists()).toBe(false)
    expect(view.find('.el-switch').classes()).toContain('is-disabled')
    const texts = rowButtonTexts(view)
    expect(texts.some((t) => t.includes('Run now'))).toBe(false)
    expect(texts.some((t) => t.includes('Run history'))).toBe(true)
    // 订阅抽屉走 /v1/admin/subscriptions(admin 专属,不在冻结只读清单)
    expect(texts.some((t) => t.includes('Subscriptions'))).toBe(false)
    expect(texts.length).toBe(1) // 只剩运行历史；编辑/删除是图标按钮
  })

  it('admin 对照：新建在、开关可点、行内动作齐全（含订阅）', async () => {
    setRole('admin')
    const view = await mountJobs()
    expect(view.find('.add').exists()).toBe(true)
    expect(view.find('.el-switch').classes()).not.toContain('is-disabled')
    const texts = rowButtonTexts(view)
    expect(texts.some((t) => t.includes('Run now'))).toBe(true)
    expect(texts.some((t) => t.includes('Subscriptions'))).toBe(true)
  })
})

/* ── CheckpointsView ──────────────────────────────────────────────────── */

async function mountCheckpoints() {
  const view = await mountWith(CheckpointsView, '/admin/checkpoints', [
    { path: '/admin/checkpoints', component: CheckpointsView },
  ])
  // 选会话 → 出行（行内才有动作按钮）
  const select = view.findComponent({ name: 'ElSelect' })
  select.vm.$emit('update:modelValue', 's1')
  select.vm.$emit('change', 's1')
  await flushPromises()
  return view
}

describe('CheckpointsView 只读门控', () => {
  it('analyst：无恢复按钮，查看详情保留', async () => {
    setRole('analyst')
    const view = await mountCheckpoints()
    const texts = rowButtonTexts(view)
    expect(texts.some((t) => t.includes('Resume here'))).toBe(false)
    expect(texts.some((t) => t.includes('View'))).toBe(true)
  })

  it('admin 对照：恢复按钮在', async () => {
    setRole('admin')
    const view = await mountCheckpoints()
    expect(
      rowButtonTexts(view).some((t) => t.includes('Resume here')),
    ).toBe(true)
  })
})

/* ── TodoQueue（总览的待办队列）────────────────────────────────────────── */

function todos(): OverviewTodos {
  return {
    total: 2,
    count_exact: true,
    items: [
      {
        kind: 'kb_lesson',
        count: 3,
        count_exact: true,
        available: true,
        samples: ['l1'],
        href: '/admin/kb?tab=lessons', // analyst 不可达（建模组）
        note: '',
      },
      {
        kind: 'drift',
        count: 1,
        count_exact: true,
        available: true,
        samples: ['financial.sales'],
        href: '/admin/governance?tab=drift', // analyst 只读面内
        note: '',
      },
    ],
  }
}

describe('TodoQueue 深链按角色过滤', () => {
  it('analyst：建模组条目显示「Admin only」，治理条目保留链接', async () => {
    setRole('analyst')
    const view = await mountWith(TodoQueue, '/admin', [], { todos: todos() })
    const links = view.findAll('.tq-link')
    expect(links.map((a) => a.attributes('href'))).toEqual([
      '/admin/governance?tab=drift',
    ])
    const nolinks = view.findAll('.tq-nolink')
    expect(nolinks.length).toBe(1)
    expect(nolinks[0].text()).toBe('Admin only')
  })

  it('admin 对照：两条都是链接', async () => {
    setRole('admin')
    const view = await mountWith(TodoQueue, '/admin', [], { todos: todos() })
    expect(view.findAll('.tq-link').length).toBe(2)
    expect(view.findAll('.tq-nolink').length).toBe(0)
  })
})

/* ── InboxTable（治理中心收件箱）───────────────────────────────────────── */

function inboxItem(over: Partial<GovernanceTodoItem> = {}): GovernanceTodoItem {
  return {
    kind: 'kb_lesson',
    id: 't1',
    ds: 'demo',
    title: 'A lesson',
    summary: 's',
    severity: null,
    confidence: 0.9,
    created_at: '2026-10-01T10:00:00Z',
    href: '/admin/kb?tab=lessons',
    actionable: { confirm: true, reject: true, batch: true, edit_url: '/admin/kb?tab=lessons' },
    diff: null,
    source: 'pending',
    ...over,
  }
}

async function mountInbox() {
  return await mountWith(InboxTable, '/admin', [], {
    items: [inboxItem()],
  })
}

describe('InboxTable 只读门控', () => {
  it('analyst：无勾选列、无确认/拒绝，编辑深链被过滤', async () => {
    setRole('analyst')
    const view = await mountInbox()
    expect(view.find('.dt-th.is-check').exists()).toBe(false)
    expect(view.find('.act-btn').exists()).toBe(false)
    expect(view.find('.link-btn').exists()).toBe(false)
    // 条目本身仍然可读（不是整行消失）
    expect(view.text()).toContain('A lesson')
  })

  it('admin 对照：勾选列与动作齐全，编辑深链保留', async () => {
    setRole('admin')
    const view = await mountInbox()
    expect(view.find('.dt-th.is-check').exists()).toBe(true)
    expect(view.findAll('.act-btn').length).toBe(2)
    expect(view.find('.link-btn').exists()).toBe(true)
  })
})
