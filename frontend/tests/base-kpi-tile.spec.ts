import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import KpiTile from '../src/components/base/KpiTile.vue'

describe('KpiTile', () => {
  it('renders label, value and sub copy', () => {
    const w = mount(KpiTile, {
      props: { label: 'Disabled', value: 3, sub: 'cannot sign in' },
    })
    expect(w.find('.kpi-label').text()).toBe('Disabled')
    expect(w.find('.kpi-value').text()).toBe('3')
    expect(w.find('.kpi-sub').text()).toBe('cannot sign in')
  })

  it('emits click with the mouse event', async () => {
    const w = mount(KpiTile, { props: { label: 'All', value: 6 } })
    await w.trigger('click')
    expect(w.emitted('click')).toHaveLength(1)
  })

  it('is a real button so Enter/Space activate it natively', () => {
    const w = mount(KpiTile, { props: { label: 'All', value: 6 } })
    expect(w.element.tagName).toBe('BUTTON')
    expect(w.attributes('type')).toBe('button')
  })

  it('marks the pressed state for assistive tech', () => {
    const off = mount(KpiTile, { props: { label: 'All', value: 6 } })
    expect(off.attributes('aria-pressed')).toBe('false')
    expect(off.classes()).not.toContain('is-active')

    const on = mount(KpiTile, { props: { label: 'All', value: 6, active: true } })
    expect(on.attributes('aria-pressed')).toBe('true')
    expect(on.classes()).toContain('is-active')
  })

  it('lets value and sub come from slots', () => {
    const w = mount(KpiTile, {
      props: { label: 'All', value: 0 },
      slots: { value: '<b class="custom-value">six</b>', sub: '<i>ctx</i>' },
    })
    expect(w.find('.custom-value').text()).toBe('six')
    expect(w.find('.kpi-sub i').text()).toBe('ctx')
  })
})
