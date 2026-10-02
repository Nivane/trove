import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import DetailDrawer from '../src/components/base/DetailDrawer.vue'
import type { VueWrapper } from '@vue/test-utils'

let wrapper: VueWrapper | null = null

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

function mountDrawer(props: Record<string, unknown> = {}, slots: Record<string, string> = {}) {
  wrapper = mount(DetailDrawer, {
    props: { modelValue: false, title: 'User detail', closeLabel: 'Close', ...props },
    slots,
    global: { stubs: { teleport: true } },
    attachTo: document.body,
  })
  return wrapper
}

describe('DetailDrawer', () => {
  it('renders nothing while closed, then the dialog when open', async () => {
    const w = mountDrawer()
    expect(w.find('.drawer-panel').exists()).toBe(false)

    await w.setProps({ modelValue: true })
    const panel = w.find('.drawer-panel')
    expect(panel.exists()).toBe(true)
    expect(panel.attributes('role')).toBe('dialog')
    expect(panel.attributes('aria-modal')).toBe('true')
    expect(w.find('.drawer-title').text()).toBe('User detail')
    expect(panel.attributes('aria-labelledby')).toBe(w.find('.drawer-title').attributes('id'))
  })

  it('closes on Escape', async () => {
    const w = mountDrawer({ modelValue: true })
    await w.find('.drawer-panel').trigger('keydown', { key: 'Escape' })
    expect(w.emitted('update:modelValue')![0]).toEqual([false])
    expect(w.emitted('close')).toHaveLength(1)
  })

  it('closes on a click on the backdrop, but not inside the panel', async () => {
    const w = mountDrawer({ modelValue: true })
    await w.find('.drawer-body').trigger('click')
    expect(w.emitted('update:modelValue')).toBeUndefined()

    await w.find('.drawer-overlay').trigger('click')
    expect(w.emitted('update:modelValue')![0]).toEqual([false])
  })

  it('closes from the ✕ button', async () => {
    const w = mountDrawer({ modelValue: true })
    await w.find('.drawer-close').trigger('click')
    expect(w.emitted('update:modelValue')![0]).toEqual([false])
  })

  it('lets beforeClose veto the close (sync false)', async () => {
    const beforeClose = vi.fn().mockReturnValue(false)
    const w = mountDrawer({ modelValue: true, beforeClose })
    await w.find('.drawer-close').trigger('click')
    expect(beforeClose).toHaveBeenCalledTimes(1)
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('honours an async beforeClose that resolves false', async () => {
    const beforeClose = vi.fn().mockResolvedValue(false)
    const w = mountDrawer({ modelValue: true, beforeClose })
    await w.find('.drawer-panel').trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('closes once an async beforeClose resolves true', async () => {
    const beforeClose = vi.fn().mockResolvedValue(true)
    const w = mountDrawer({ modelValue: true, beforeClose })
    await w.find('.drawer-panel').trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(w.emitted('update:modelValue')![0]).toEqual([false])
  })

  it('stays open when the guard throws', async () => {
    const beforeClose = vi.fn().mockRejectedValue(new Error('nope'))
    const w = mountDrawer({ modelValue: true, beforeClose })
    await w.find('.drawer-close').trigger('click')
    await flushPromises()
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('moves focus inside on open, traps Tab, and restores it on close', async () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()

    const w = mountDrawer(
      { modelValue: true },
      { default: '<button class="inner">inner</button>' },
    )
    await flushPromises()
    expect(document.activeElement).toBe(w.find('.drawer-close').element)

    const inner = w.find('.inner').element as HTMLElement
    inner.focus()
    await w.find('.drawer-panel').trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(w.find('.drawer-close').element)

    await w.setProps({ modelValue: false })
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('renders body and footer slots', async () => {
    const w = mountDrawer(
      { modelValue: true },
      { default: '<p class="body-part">body</p>', footer: '<button>save</button>' },
    )
    expect(w.find('.drawer-body .body-part').text()).toBe('body')
    expect(w.find('.drawer-foot button').text()).toBe('save')
  })
})
