import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { nextTick } from 'vue'
import NextActions from '../src/components/chat/NextActions.vue'
import { useAuthStore } from '../src/stores/auth'
import type { NextActionInfo } from '../src/api/types'

// 拒绝出口(A1):后端给全 label 与深链,前端只渲染 + 按登录角色过闸。
// 分类判断永远在判定侧 —— 这里没有 sort/switch/映射表,只有角色闸与形状过滤。

const EXITS: NextActionInfo[] = [
  {
    id: 'datasource_init',
    kind: 'datasource_init',
    label: '去初始化语义模型',
    href: '/admin/kb?ds=demo',
    admin_only: true,
  },
  {
    id: 'ask_again',
    kind: 'ask_again',
    label: '换个问法重问',
    href: '/',
    admin_only: false,
  },
]

let router: Router

function mountActions(actions?: NextActionInfo[] | null, role = 'admin') {
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'u', role }
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin/kb', component: { render: () => null } },
      { path: '/admin/semantic', component: { render: () => null } },
    ],
  })
  return mount(NextActions, {
    props: { actions },
    global: { plugins: [router] },
  })
}

describe('NextActions', () => {
  it('renders nothing without actions (absent / empty / null)', () => {
    for (const absent of [undefined, null, []] as const) {
      expect(mountActions(absent).find('.next-actions').exists()).toBe(false)
    }
  })

  it('renders the backend labels as real links (文案与落点都来自后端)', async () => {
    const w = mountActions(EXITS)
    await router.isReady()
    await nextTick()
    const links = w.findAll('a.next-action-btn')
    expect(links).toHaveLength(2)
    expect(links[0].text()).toContain('去初始化语义模型')
    expect(links[0].attributes('href')).toBe('/admin/kb?ds=demo')
  })

  it('hides admin_only exits from non-admins', async () => {
    const w = mountActions(EXITS, 'user')
    await router.isReady()
    await nextTick()
    const links = w.findAll('a.next-action-btn')
    expect(links).toHaveLength(1)
    expect(links[0].text()).toContain('换个问法重问')
  })

  it('relays payload as bp_* params mechanically — no key is interpreted here', async () => {
    const w = mountActions(
      [
        {
          id: 'new_from_draft',
          kind: 'new_from_draft',
          label: '以这份草稿为蓝本新建',
          href: '/admin/semantic?ds=demo&tab=pending',
          admin_only: true,
          payload: {
            draft_kind: 'metric',
            draft_name: 'avg_amount',
            expression: 'AVG(loan.amount)',
            datasets: ['loan', 'account'],
            conflict_code: 'name_declared',
            conflict_message: '指标「avg_amount」已声明',
            empty: '',
            nothing: null,
          },
        },
      ],
      'admin',
    )
    await router.isReady()
    await nextTick()
    const href = w.find('a.next-action-btn').attributes('href') ?? ''
    const q = new URLSearchParams(href.split('?')[1] ?? '')
    // 原有 query 保留,payload 逐键加 bp_ 前缀并入
    expect(q.get('ds')).toBe('demo')
    expect(q.get('tab')).toBe('pending')
    expect(q.get('bp_draft_kind')).toBe('metric')
    expect(q.get('bp_expression')).toBe('AVG(loan.amount)')
    expect(q.get('bp_conflict_code')).toBe('name_declared')
    // 数组以逗号连接(目标页按同一约定拆)
    expect(q.get('bp_datasets')).toBe('loan,account')
    // 空值/空串不写进 URL(缺席 = 没这个键,不是空键)
    expect(href).not.toContain('bp_empty')
    expect(href).not.toContain('bp_nothing')
    // 机械转运:这里没有 sort/switch —— 未知键也照样带过去
    expect(q.get('bp_conflict_message')).toBe('指标「avg_amount」已声明')
  })

  it('a payload-less action keeps its href byte-identical', async () => {
    const w = mountActions(EXITS)
    await router.isReady()
    await nextTick()
    const links = w.findAll('a.next-action-btn')
    expect(links[0].attributes('href')).toBe('/admin/kb?ds=demo')
    expect(links[1].attributes('href')).toBe('/')
  })

  it('filters entries the backend could not complete (no href / no label)', async () => {
    const w = mountActions(
      [
        { kind: 'broken', label: '没有落点' },
        { kind: 'broken2', href: '/admin/kb' },
        ...EXITS,
      ],
      'admin',
    )
    await router.isReady()
    await nextTick()
    expect(w.findAll('a.next-action-btn')).toHaveLength(2)
  })
})
