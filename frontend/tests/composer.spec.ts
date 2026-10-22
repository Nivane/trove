import { describe, it, expect, beforeEach, vi } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import Composer from '../src/components/chat/Composer.vue'
import { useChatStore } from '../src/stores/chat'
import { useUiStore } from '../src/stores/ui'

function mountComposer() {
  setActivePinia(createPinia())
  return mount(Composer, { global: { stubs: { 'el-icon': true } } })
}

describe('Composer', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('disables the send button when the draft is empty', () => {
    const wrapper = mountComposer()
    expect(
      wrapper.find('button[type="submit"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('submits the draft and clears the input', async () => {
    const wrapper = mountComposer()
    const chat = useChatStore()
    const sendSpy = vi.spyOn(chat, 'send').mockResolvedValue()
    await wrapper.find('textarea').setValue('哪个地区最高')
    await wrapper.find('form').trigger('submit')
    expect(sendSpy).toHaveBeenCalledWith('哪个地区最高')
    expect(
      (wrapper.find('textarea').element as HTMLTextAreaElement).value,
    ).toBe('')
  })

  it('renders the send button as icon-only circular', () => {
    const wrapper = mountComposer()
    const btn = wrapper.find('button.send-btn')
    expect(btn.classes()).toContain('circular')
    expect(btn.text()).toBe('') // icon-only: label removed, lucide svg has no text
    expect(btn.attributes('type')).toBe('submit')
  })

  it('swaps to a circular non-submitting stop button while streaming', async () => {
    const wrapper = mountComposer()
    const chat = useChatStore()
    chat.streaming = true
    await nextTick()
    expect(wrapper.find('button.send-btn').exists()).toBe(false)
    const stop = wrapper.find('button.stop-btn')
    expect(stop.exists()).toBe(true)
    expect(stop.classes()).toContain('circular')
    expect(stop.attributes('type')).toBe('button') // must not submit the form
    const stopSpy = vi.spyOn(chat, 'stop') // store stop() is sync — plain spy suffices
    await stop.trigger('click')
    expect(stopSpy).toHaveBeenCalled()
  })

  it('runs the highlighted slash command on Enter instead of sending it as a question', async () => {
    const wrapper = mountComposer()
    const chat = useChatStore()
    const sendSpy = vi.spyOn(chat, 'send').mockResolvedValue()
    const clearSpy = vi.spyOn(chat, 'clearConversation').mockResolvedValue()

    await wrapper.find('textarea').setValue('/clear')
    await nextTick()
    expect(wrapper.find('.command-menu').exists()).toBe(true)

    await wrapper.find('textarea').trigger('keydown', { key: 'Enter' })

    expect(clearSpy).toHaveBeenCalled()
    expect(sendSpy).not.toHaveBeenCalled()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')
  })

  it('keeps the composer free of hint text', () => {
    const wrapper = mountComposer()
    expect(wrapper.find('.composer-hint').exists()).toBe(false)
  })

  it('marks an uninitialized datasource as not ready in the picker', async () => {
    const wrapper = mountComposer()
    const ui = useUiStore()
    ui.lang = 'en'
    ui.datasourceList = [
      { name: 'financial', type: 'mysql', status: 'connected', kb_initialized: true },
      { name: 'sales', type: 'mysql', status: 'connected', kb_initialized: false },
    ] as any
    await nextTick()
    await wrapper.find('.plus-btn').trigger('click')
    const items = wrapper.findAll('.plus-item')
    const sales = items.find((i) => i.text().includes('sales'))!
    expect(sales.find('.plus-not-ready').exists()).toBe(true)
    expect(sales.text()).toContain('Not ready')
    const financial = items.find((i) => i.text().includes('financial'))!
    expect(financial.find('.plus-not-ready').exists()).toBe(false)
  })
})