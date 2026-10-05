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
