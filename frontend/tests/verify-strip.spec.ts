/**
 * 答案卡验证条 + 「已验证」印章（2026-10 视觉升级）——
 *
 * 六段骨架：走过的段点亮（.vseg.lit），没走的灰着。
 * 印章三态纪律（与溯源条同源）：只有 verdict=OK 才盖章（绿）；
 * verdict 有值但非 OK → 琥珀照实说；没有 verdict → 灰、不表态。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import VerifyStrip from '../src/components/chat/VerifyStrip.vue'
import { stepCardFromEvent, type StepCard } from '../src/stores/chat'
import { useUiStore } from '../src/stores/ui'
import type { DoneSummary, StepPayload } from '../src/api/types'

let wrapper: VueWrapper | null = null

const S = (node: string, extra: Record<string, unknown> = {}): StepCard =>
  stepCardFromEvent({ node, seq: 1, ...extra } as StepPayload)

async function mountStrip(props: {
  steps: StepCard[]
  summary?: DoneSummary | null
  truncated?: boolean
}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUiStore().lang = 'en'
  wrapper = mount(VerifyStrip, { props, global: { plugins: [pinia] } })
  return wrapper
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('VerifyStrip — 六段骨架', () => {
  it('走过的段点亮，没走的灰着（不虚报）', async () => {
    const view = await mountStrip({
      steps: [S('route_intent'), S('gen_sql'), S('validate')],
      summary: { verdict: 'OK' },
    })
    const segs = view.findAll('.vseg')
    expect(segs).toHaveLength(6)
    const lit = view.findAll('.vseg.lit')
    expect(lit.map((s) => s.text())).toEqual(['Route', 'Generate', 'Verify'])
  })
})

describe('VerifyStrip — 「已验证」印章', () => {
  it('verdict=OK → 绿章「Verified · N steps」+ 反思轮数', async () => {
    const view = await mountStrip({
      steps: [
        S('gen_sql'),
        S('validate'),
        S('reflect', { retry_count: 0 }),
      ],
      summary: { verdict: 'OK' },
    })
    const seal = view.find('.vok')
    expect(seal.classes()).toContain('tone-ok')
    expect(seal.text()).toContain('Verified · 3 steps')
    // 反思跑过（retry_count=0）→ 写「0 rework round(s)」，不装没发生
    expect(seal.text()).toContain('0 rework round(s)')
    expect(view.find('.vok-rework').classes()).not.toContain('hit')
  })

  it('verdict 有值但非 OK → 琥珀照实说（认不出 ≠ 通过）', async () => {
    const view = await mountStrip({
      steps: [S('gen_sql'), S('reflect', { retry_count: 2 })],
      summary: { verdict: 'EMPTY' },
    })
    const seal = view.find('.vok')
    expect(seal.classes()).toContain('tone-warn')
    expect(seal.text()).toContain('EMPTY · 2 steps')
    expect(seal.text()).not.toContain('Verified')
    // 重来过 → 修正轮数带 warn 色
    expect(view.find('.vok-rework').classes()).toContain('hit')
  })

  it('没有 verdict → 灰、只报工序数，不盖章', async () => {
    const view = await mountStrip({
      steps: [S('gen_sql')],
      summary: null,
    })
    const seal = view.find('.vok')
    expect(seal.classes()).toContain('tone-neutral')
    expect(seal.text()).toBe('1 steps')
    expect(seal.text()).not.toContain('Verified')
  })

  it('历史截断 → 工序数带 +', async () => {
    const view = await mountStrip({
      steps: [S('gen_sql')],
      summary: { verdict: 'OK' },
      truncated: true,
    })
    expect(view.find('.vok').text()).toContain('1+ steps')
  })

  it('点印章 → emit open（打开工序面板）', async () => {
    const view = await mountStrip({
      steps: [S('gen_sql')],
      summary: { verdict: 'OK' },
    })
    await view.find('.vok').trigger('click')
    expect(view.emitted('open')).toHaveLength(1)
  })
})
