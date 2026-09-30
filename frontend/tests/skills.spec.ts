import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import SkillsView from '../src/views/admin/SkillsView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

import { apiGet, apiPost } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

let wrapper: VueWrapper | null = null

beforeEach(() => {
  setActivePinia(createPinia())
  useUiStore().lang = 'en'
  vi.clearAllMocks()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

async function mountRows(rows: unknown[]) {
  ;(apiGet as any).mockResolvedValue({ skills: rows })
  wrapper = mount(SkillsView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

const ORG = {
  source: 'admin',
  status: 'confirmed',
  description: 'd',
  triggers: {},
}

describe('SkillsView tier column', () => {
  it('names the validator tier instead of falling through to available', async () => {
    // validator 是第三档,不是"没选 required 的 available" —— 它按结果断言
    // 运行、正文从不投给模型。落到 available 分支上就是显示别人的名字。
    const view = await mountRows([
      { ...ORG, name: 'credit-guard', tier: 'validator' },
      { ...ORG, name: 'soft', tier: 'available' },
    ])
    const text = view.text()
    expect(text).toContain('validator (result assertions)')
    expect(text).toContain('available (on demand)')
    // 校验器那行的提示说清"手写 SKILL.md",而不是让人去找一个不存在的按钮
    const titles = view
      .findAll('[title]')
      .map((el) => el.attributes('title') ?? '')
    expect(titles.some((h) => h.includes('SKILL.md'))).toBe(true)
  })

  it('hides the tier toggle for validator rows only', async () => {
    // 这条按钮对 validator 行**恒 400**(service 结构性拒绝:四字段按当前
    // tier 投影,两个方向都校验不出)—— 留一个必定失败的按钮比不给更坏。
    const view = await mountRows([
      { ...ORG, name: 'credit-guard', tier: 'validator' },
      { ...ORG, name: 'soft', tier: 'available' },
    ])
    const labels = view.findAll('button').map((b) => b.text())
    expect(labels.filter((l) => l === 'Set required').length).toBe(1)
    expect(labels.filter((l) => l === 'Set available').length).toBe(0)
  })

  it('still posts the toggle for a non-validator row', async () => {
    const view = await mountRows([{ ...ORG, name: 'soft', tier: 'required' }])
    ;(apiPost as any).mockResolvedValue({})
    const btn = view.findAll('button').find((b) => b.text() === 'Set available')!
    await btn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/v1/admin/skills/soft/tier', {
      tier: 'available',
    })
  })
})
