import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import Sidebar from '../src/components/layout/Sidebar.vue'
import { useUiStore } from '../src/stores/ui'
import { useChatStore } from '../src/stores/chat'

function mountSidebar() {
  setActivePinia(createPinia())
  // onMounted fires listSessions → silence the network call in unit tests
  vi.spyOn(useChatStore(), 'listSessions').mockResolvedValue(undefined)
  return mount(Sidebar, {
    global: { stubs: { 'router-link': true, 'el-icon': true } },
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

  it('shows the resizer in the expanded state', () => {
    const wrapper = mountSidebar()
    expect(wrapper.find('.sidebar-resizer').exists()).toBe(true)
  })

  it('resizer drag updates and persists the width', async () => {
    const wrapper = mountSidebar()
    const ui = useUiStore()
    // @vue/test-utils trigger() maps pointer events to MouseEvent whose
    // clientX is getter-only in jsdom — dispatch a real PointerEvent instead.
    wrapper
      .find('.sidebar-resizer')
      .element.dispatchEvent(
        new PointerEvent('pointerdown', { clientX: 300, bubbles: true }),
      )
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: 500 }))
    window.dispatchEvent(new PointerEvent('pointerup'))
    // 260 + 200 = 460 → clamp 到 400
    expect(ui.sidebarWidth).toBe(400)
    expect(localStorage.getItem('trove_ui_sidebar_width')).toBe('400')
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
