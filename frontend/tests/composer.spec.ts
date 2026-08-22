import { describe, it, expect, beforeEach, vi } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import Composer from '../src/components/chat/Composer.vue'
import { useChatStore } from '../src/stores/chat'

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

  it('keeps the composer free of hint text', () => {
    const wrapper = mountComposer()
    expect(wrapper.find('.composer-hint').exists()).toBe(false)
  })
})