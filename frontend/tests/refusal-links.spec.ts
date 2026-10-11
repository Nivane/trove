/**
 * 拒绝卡「已声明的链接」(本体形态 O1,设计 §5.3)——
 * `refusal.related_links` 的只读投影:一条 = 路径 + (显示名 · 基数)。
 *
 * 三态是三个不同的可区分事实,测试逐态钉住:
 *  · 键缺席/null = 后端没投影这件事 → 整节不渲染(老拒绝卡逐字节不变);
 *  · 空数组 = 有声明面、这个方向一条都没有 → 引导文案(不是空列表);
 *  · 非空 = 逐条渲染,显示名缺席回退关系名,基数记法逐字原样(不翻译)。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import RefusalRelatedLinks from '../src/components/chat/RefusalRelatedLinks.vue'
import StepCard from '../src/components/chat/StepCard.vue'
import { useUiStore } from '../src/stores/ui'
import { useAuthStore } from '../src/stores/auth'
import type { RelatedLinkInfo } from '../src/api/types'
import type { StepCard as StepCardType } from '../src/stores/chat'

let router: Router
let wrapper: VueWrapper | null = null

beforeEach(async () => {
  setActivePinia(createPinia())
  useUiStore().lang = 'zh'
  useAuthStore().user = { id: 1, username: 'u', role: 'admin' }
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin/semantic', component: { render: () => null } },
    ],
  })
  await router.push('/')
  await router.isReady()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

function mountLinks(links?: RelatedLinkInfo[] | null) {
  wrapper = mount(RefusalRelatedLinks, { props: { links }, global: { plugins: [router] } })
  return wrapper
}

describe('RefusalRelatedLinks', () => {
  it('renders each entry as path + (display name · cardinality)', () => {
    const w = mountLinks([
      { name: 'loan_customer', from: '贷款', to: '客户', display_name: '属于', cardinality: '1:N' },
      // 显示名缺席 → 回退关系名;基数记法原样,不做语言映射
      { name: 'loan_account', from: 'loan', to: 'account', cardinality: 'N:1' },
    ])
    expect(w.find('.refusal-links-label').text()).toBe('已声明的链接')
    const rows = w.findAll('.refusal-link-row').map((r) => r.text())
    expect(rows).toEqual(['贷款 → 客户（属于 · 1:N）', 'loan → account（loan_account · N:1）'])
  })

  it('three states: absent/null → nothing, [] → guidance, non-empty → rows', () => {
    // 缺席/null = 后端没给这件事 —— 整节不渲染(老拒绝卡逐字节不变)
    expect(mountLinks(undefined).find('.refusal-links').exists()).toBe(false)
    expect(mountLinks(null).find('.refusal-links').exists()).toBe(false)

    // 空数组 = 给了,且这个方向确实一条都没有 —— 引导文案,不是空列表
    const empty = mountLinks([])
    expect(empty.find('.refusal-links-list').exists()).toBe(false)
    expect(empty.find('.refusal-links-empty').text()).toContain('这个方向还没有声明的链接')
  })

  it('StepCard renders the links section alongside the existing exits, and omits it when absent', async () => {
    const card = (refusal: Record<string, unknown>): StepCardType => ({
      node: 'refuse',
      payload: { node: 'refuse', refusal } as never,
    })
    const link: RelatedLinkInfo = {
      name: 'loan_customer',
      from: '贷款',
      to: '客户',
      display_name: '属于',
      cardinality: '1:N',
    }
    const exit = {
      id: 'confirm_draft',
      kind: 'confirm_draft',
      label: '确认这份草稿',
      href: '/admin/semantic?ds=demo&tab=pending',
      admin_only: true,
    }

    wrapper = mount(StepCard, {
      props: { card: card({ related_links: [link], next_actions: [exit] }), status: 'done' },
      global: { plugins: [router] },
    })
    await flushPromises()
    const text = wrapper.text()
    // 链接节与出口块并存(一个是「已有什么」,一个是「下一步做什么」)
    expect(text).toContain('已声明的链接')
    expect(text).toContain('贷款 → 客户（属于 · 1:N）')
    expect(text).toContain('确认这份草稿')

    // 老形状的拒绝(没这个键):出口照常,链接节整节不渲染
    wrapper = mount(StepCard, {
      props: { card: card({ next_actions: [exit] }), status: 'done' },
      global: { plugins: [router] },
    })
    await flushPromises()
    expect(wrapper.find('.refusal-links').exists()).toBe(false)
    expect(wrapper.text()).toContain('确认这份草稿')
  })
})
