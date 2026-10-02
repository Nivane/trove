import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import StatePanel from '../src/components/base/StatePanel.vue'

describe('StatePanel', () => {
  it('announces loading with a spinner and a busy status', () => {
    const w = mount(StatePanel, {
      props: { mode: 'loading', title: 'Loading users' },
    })
    expect(w.find('.sp-spin').exists()).toBe(true)
    expect(w.attributes('aria-busy')).toBe('true')
    expect(w.attributes('role')).toBe('status')
    expect(w.find('.sp-title').text()).toBe('Loading users')
  })

  it('renders the empty state with copy and an action slot', () => {
    const w = mount(StatePanel, {
      props: { mode: 'empty', title: 'No matching users', description: 'Clear a filter' },
      slots: { action: '<button class="clear">Clear filters</button>' },
    })
    expect(w.find('.sp-title').text()).toBe('No matching users')
    expect(w.find('.sp-desc').text()).toBe('Clear a filter')
    expect(w.find('.clear').text()).toBe('Clear filters')
    expect(w.find('.sp-actions').exists()).toBe(true)
  })

  it('renders the error state as an alert with technical detail', () => {
    const w = mount(StatePanel, {
      props: {
        mode: 'error',
        title: 'Could not load users',
        description: 'The request timed out.',
        detail: 'GET /v1/admin/users → 504 (run 3f9a)',
      },
    })
    expect(w.attributes('role')).toBe('alert')
    expect(w.attributes('aria-live')).toBe('assertive')
    expect(w.find('.sp-detail').text()).toContain('504')
  })

  it('shows the built-in retry button only when retryText is given, and emits retry', async () => {
    const without = mount(StatePanel, { props: { mode: 'error', title: 'Oops' } })
    expect(without.find('.sp-btn').exists()).toBe(false)

    const w = mount(StatePanel, {
      props: { mode: 'error', title: 'Oops', retryText: 'Retry' },
    })
    await w.find('.sp-btn').trigger('click')
    expect(w.emitted('retry')).toHaveLength(1)
  })

  it('lets the action slot replace the built-in retry button', () => {
    const w = mount(StatePanel, {
      props: { mode: 'error', title: 'Oops', retryText: 'Retry' },
      slots: { action: '<a class="custom">Reload</a>' },
    })
    expect(w.find('.sp-btn').exists()).toBe(false)
    expect(w.find('.custom').text()).toBe('Reload')
  })

  it('keeps copy out of the component: no title or description renders when omitted', () => {
    const w = mount(StatePanel, { props: { mode: 'empty' } })
    expect(w.find('.sp-title').exists()).toBe(false)
    expect(w.find('.sp-desc').exists()).toBe(false)
  })
})
