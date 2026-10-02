import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ConfirmDialog from '../src/components/base/ConfirmDialog.vue'
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

function mountDialog(props: Record<string, unknown> = {}, slots: Record<string, string> = {}) {
  wrapper = mount(ConfirmDialog, {
    props: {
      modelValue: true,
      title: 'Delete 2 users',
      confirmText: 'Delete 2 users',
      cancelText: 'Cancel',
      ...props,
    },
    slots,
    global: { stubs: { teleport: true } },
    attachTo: document.body,
  })
  return wrapper
}

describe('ConfirmDialog', () => {
  it('renders the action title and both verb labels', () => {
    const w = mountDialog()
    expect(w.find('.confirm-title').text()).toBe('Delete 2 users')
    const buttons = w.findAll('.confirm-btn')
    expect(buttons[0].text()).toBe('Cancel')
    expect(buttons[1].text()).toBe('Delete 2 users')
  })

  it('describes the blast radius through the impact slot', () => {
    const w = mountDialog({}, { impact: '<ul><li>3 tokens are removed</li></ul>' })
    const impact = w.find('.confirm-impact')
    expect(impact.text()).toContain('3 tokens are removed')
    expect(w.find('.confirm-panel').attributes('aria-describedby')).toBe(impact.attributes('id'))
  })

  it('emits confirm and stays open so the caller can await its work', async () => {
    const w = mountDialog()
    await w.findAll('.confirm-btn')[1].trigger('click')
    expect(w.emitted('confirm')).toHaveLength(1)
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('cancels and closes itself', async () => {
    const w = mountDialog()
    await w.findAll('.confirm-btn')[0].trigger('click')
    expect(w.emitted('cancel')).toHaveLength(1)
    expect(w.emitted('update:modelValue')![0]).toEqual([false])
  })

  it('cancels on Escape and on a backdrop click', async () => {
    const w = mountDialog()
    await w.find('.confirm-panel').trigger('keydown', { key: 'Escape' })
    expect(w.emitted('cancel')).toHaveLength(1)

    await w.find('.confirm-overlay').trigger('click')
    expect(w.emitted('cancel')).toHaveLength(2)
  })

  it('locks both buttons and ignores Esc/backdrop while loading', async () => {
    const w = mountDialog({ loading: true })
    const buttons = w.findAll('.confirm-btn')
    expect(buttons.every((b) => b.attributes('disabled') !== undefined)).toBe(true)
    expect(w.find('.confirm-spin').exists()).toBe(true)

    await w.find('.confirm-panel').trigger('keydown', { key: 'Escape' })
    await w.find('.confirm-overlay').trigger('click')
    await buttons[1].trigger('click')
    expect(w.emitted('cancel')).toBeUndefined()
    expect(w.emitted('confirm')).toBeUndefined()
  })

  it('uses alertdialog and a danger button for destructive actions', () => {
    const w = mountDialog({ danger: true })
    expect(w.find('.confirm-panel').attributes('role')).toBe('alertdialog')
    expect(w.findAll('.confirm-btn')[1].classes()).toContain('is-danger')
  })

  it('uses a plain dialog for non-destructive confirmations', () => {
    const w = mountDialog({ danger: false })
    expect(w.find('.confirm-panel').attributes('role')).toBe('dialog')
    expect(w.findAll('.confirm-btn')[1].classes()).toContain('is-primary')
  })

  it('focuses the safe action when it opens', async () => {
    const w = mountDialog({ modelValue: false })
    await w.setProps({ modelValue: true })
    await flushPromises()
    expect(document.activeElement).toBe(w.findAll('.confirm-btn')[0].element)
  })

  it('renders nothing while closed', () => {
    const w = mountDialog({ modelValue: false })
    expect(w.find('.confirm-panel').exists()).toBe(false)
  })
})
