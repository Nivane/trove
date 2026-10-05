import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import Sidebar from '../src/components/layout/Sidebar.vue'
import { useUiStore } from '../src/stores/ui'
import { useChatStore } from '../src/stores/chat'

function mountSidebar() {
  setActivePinia(createPinia())
  // onMounted fires listSessions → silence the network call in unit tests
  vi.spyOn(useChatStore(), 'listSessions').mockResolvedValue(undefined)
  return mount(Sidebar, {
    global: { stubs: { 'router-link': RouterLinkStub, 'el-icon': true } },
  })
}

describe('Sidebar', () => {
  beforeEach(() => localStorage.clear())

  it('applies rail class when collapsed', async () => {
    const wrapper = mountSidebar()
    const ui = useUiStore()
    ui.sidebarOpen = false
    await wrapper.vm.$nextTick()
    expect(wrapper.classes()).toContain('rail')
  })

  it('has no resizer (fixed width, not draggable)', () => {
    const wrapper = mountSidebar()
    expect(wrapper.find('.sidebar-resizer').exists()).toBe(false)
  })

  it('brand mark is a link to home (点击 logo 回首页)', () => {
    const wrapper = mountSidebar()
    const brand = wrapper.findComponent('.brand-mark')
    expect(brand.exists()).toBe(true)
    expect(brand.props('to')).toBe('/')
  })

  it('toggles between rail and expanded with the panel button', async () => {
    const wrapper = mountSidebar()
    const ui = useUiStore()
    ui.sidebarOpen = false
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.rail-btn').exists()).toBe(true)
    expect(wrapper.find('.history-popover').exists()).toBe(false)
    await wrapper.find('.rail-btn').trigger('click')
    expect(ui.sidebarOpen).toBe(true)
  })
})
