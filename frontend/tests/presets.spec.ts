/**
 * PresetCard — 治理中心「覆盖与体检」页里的预设包卡片。
 *
 * 这个组件存在的理由只有一个:**把套用报告诚实摊开**。所以断言集中在
 * 「报告怎么渲染」这条线上,而不是样式:
 *
 *   · 三态计数(drafted / skipped / unresolved)各自出现,且 unresolved
 *     逐条带 reason —— 一次全没解析上的套用不该看起来像成功;
 *   · 套用只打一个请求,name/datasource 原样送达(后端才是判断者);
 *   · 只读角色(analyst)看不到「套用」按钮 —— 后端仍会 403,隐藏是体验层;
 *   · 清单取不到是错误态而不是空态(空态会让「没预设」与「没取到」混为一谈)。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'

vi.mock('../src/api/presets', () => ({
  fetchPresets: vi.fn(),
  applyPreset: vi.fn(),
}))

import { applyPreset, fetchPresets } from '../src/api/presets'
import type { PresetApplyReport, PresetBrief } from '../src/api/presets'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import PresetCard from '../src/components/governance/PresetCard.vue'

const LANG = 'en' as const

function brief(over: Partial<PresetBrief> = {}): PresetBrief {
  return {
    name: 'financial-analysis',
    version: 1,
    description: 'Starting checklist.',
    author: 'trove',
    source: 'builtin',
    path: '/srv/trove/presets/financial-analysis',
    counts: { skills: 2, decisions: 2 },
    shadowed: false,
    ...over,
  }
}

function report(over: Partial<PresetApplyReport> = {}): PresetApplyReport {
  return {
    preset: 'financial-analysis',
    datasource: 'demo',
    source: 'builtin',
    counts: { drafted: 2, skipped: 1, unresolved: 1 },
    items: [
      {
        section: 'skills',
        item: 'period-comparison',
        status: 'drafted',
        reason: '已落为待审 skill 草稿(skills/period-comparison),确认后才进入投递面',
      },
      {
        section: 'decisions',
        item: 'watch-ratio',
        status: 'drafted',
        reason: '条件解析通过;已落规则草稿(enabled: false 默认携带)',
      },
      {
        section: 'domains',
        item: 'credit-risk',
        status: 'unresolved',
        reason: 'datasets 未声明或未在目标语义模型找到',
      },
      {
        section: 'semantics',
        item: 'notes',
        status: 'skipped',
        reason: '仅提示:本段不写文件',
      },
    ],
    ...over,
  }
}

let wrapper: VueWrapper | null = null

async function mountCard(props: Record<string, unknown> = {}) {
  wrapper = mount(PresetCard, {
    props: { datasources: ['demo', 'sales'], datasource: 'demo', ...props },
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function findButton(el: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(el.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  )
  if (!btn) throw new Error(`button not found: ${text}`)
  return btn as HTMLButtonElement
}

beforeEach(() => {
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = LANG
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('PresetCard — listing', () => {
  it('lists builtin and org rows and marks the shadowed one', async () => {
    ;(fetchPresets as any).mockResolvedValue([
      brief(),
      brief({
        name: 'financial-analysis',
        source: 'org',
        shadowed: false,
        path: '/home/u/.trove/presets/financial-analysis',
      }),
      brief({ name: 'legacy-pack', source: 'builtin', shadowed: true }),
    ])
    const view = await mountCard()

    // 打开第一个下拉:两源同名的两份都在选项里,被遮蔽的那份带标记
    const sel = document.querySelector(
      '.preset-card .el-select__wrapper',
    ) as HTMLElement
    sel.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    const labels = Array.from(
      document.body.querySelectorAll<HTMLElement>('.el-select-dropdown__item'),
    ).map((i) => i.textContent ?? '')
    expect(labels.some((l) => l.includes('Org'))).toBe(true)
    expect(labels.some((l) => l.includes('Shadowed by org'))).toBe(true)
    expect(view.text()).toContain('pending drafts') // 提示条:确认才生效
  })

  it('a failed listing is an error state, not an empty state', async () => {
    ;(fetchPresets as any).mockRejectedValue(new Error('boom'))
    const view = await mountCard()
    expect(view.text()).toContain('Could not load presets')
    expect(view.text()).not.toContain('No presets available')
  })
})

describe('PresetCard — apply', () => {
  beforeEach(() => {
    ;(fetchPresets as any).mockResolvedValue([brief()])
  })

  it('sends the picked preset + datasource and renders the three-way counts', async () => {
    ;(applyPreset as any).mockResolvedValue(report())
    const view = await mountCard()

    findButton(view.element as HTMLElement, 'Apply').click()
    await flushPromises()

    expect(applyPreset).toHaveBeenCalledWith('financial-analysis', 'demo')
    const text = view.text()
    expect(text).toContain('2 drafted')
    expect(text).toContain('1 skipped')
    expect(text).toContain('1 unresolved')
    expect(text).toContain('financial-analysis → demo')
  })

  it('renders every item with its own reason and a distinct unresolved mark', async () => {
    ;(applyPreset as any).mockResolvedValue(report())
    const view = await mountCard()
    findButton(view.element as HTMLElement, 'Apply').click()
    await flushPromises()

    expect(view.findAll('.pc-item')).toHaveLength(4)
    const unresolved = view.findAll('.pc-item.is-unresolved')
    expect(unresolved).toHaveLength(1)
    expect(unresolved[0].text()).toContain('datasets 未声明')
    // 三条状态各自的样式类都在(颜色由 token 决定,这里钉的是分档本身)
    expect(view.findAll('.pc-item.is-drafted')).toHaveLength(2)
    expect(view.findAll('.pc-item.is-skipped')).toHaveLength(1)
  })

  it('is disabled until a datasource is picked', async () => {
    ;(applyPreset as any).mockResolvedValue(report())
    const view = await mountCard({ datasources: [], datasource: '' })
    const btn = findButton(view.element as HTMLElement, 'Apply')
    expect(btn.disabled).toBe(true)
    btn.click()
    await flushPromises()
    expect(applyPreset).not.toHaveBeenCalled()
  })

  it('fills the target when the async datasource list arrives', async () => {
    // 真实页面上 dsNames 是挂载后才取回的:到达时没有已选目标就补第一个,
    // 否则单源安装的「套用」会一直停在禁用态(要求先手点一次下拉)。
    ;(applyPreset as any).mockResolvedValue(report())
    const view = await mountCard({ datasources: [], datasource: '' })
    expect(findButton(view.element as HTMLElement, 'Apply').disabled).toBe(true)

    await view.setProps({ datasources: ['demo', 'sales'] })
    await flushPromises()

    const btn = findButton(view.element as HTMLElement, 'Apply')
    expect(btn.disabled).toBe(false)
    btn.click()
    await flushPromises()
    expect(applyPreset).toHaveBeenCalledWith('financial-analysis', 'demo')
  })

  it('hides the apply button for a read-only role', async () => {
    useAuthStore().user = { id: 2, username: 'analyst', role: 'analyst' }
    const view = await mountCard()
    // 卡片上唯一的按钮就是「套用」—— 只读角色一个按钮都不该有。
    expect(view.findAll('button')).toHaveLength(0)
    expect(fetchPresets).toHaveBeenCalled() // 列表仍然给看
  })
})
