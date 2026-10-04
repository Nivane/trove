/**
 * 历史轮「分析过程」面板 —— 方案 ①② 的呈现门:
 *
 *   · 历史轮重建的步骤卡照常渲染(与直播同一映射),头部给出步数;
 *   · stepsTruncated > 0 → 截断注记(「其后 N 步未随历史保存」,不静默丢步);
 *   · 零步骤的完结轮 → 「本轮无步骤记录」—— 有轮次零步骤 ≠ 纯空白;
 *   · 直播中(status=streaming)零步骤不插话:状态条已在说明在跑。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AnalysisPanel from '../src/components/chat/AnalysisPanel.vue'
import { useChatStore, stepCardFromEvent, type Turn } from '../src/stores/chat'
import { useUiStore } from '../src/stores/ui'
import type { StepPayload } from '../src/api/types'

let wrapper: VueWrapper | null = null

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    question: '哪个地区的平均贷款金额最高?',
    thoughts: [],
    steps: [],
    answer: 'north 最高。',
    summary: null,
    status: 'done',
    ...overrides,
  }
}

/** 同一 pinia 上设语言(ui store 的 lang 初值只从 localStorage 读一次)。 */
async function mountPanel(turn: Turn) {
  const pinia = createPinia()
  setActivePinia(pinia)
  // 组件挂在这个新 pinia 上,语言必须在这同一个 pinia 上设(ui store 的
  // lang 初值只从 localStorage 读一次)。
  useUiStore().lang = 'en'
  useChatStore().turns.push(turn)
  wrapper = mount(AnalysisPanel, { global: { plugins: [pinia] } })
  await flushPromises()
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

describe('AnalysisPanel — 历史轮步骤与空态', () => {
  it('重建的步骤逐条渲染,头部报步数(历史轮无直播时钟)', async () => {
    const steps = [
      stepCardFromEvent({ node: 'schema_linking', seq: 1 } as StepPayload),
      stepCardFromEvent({ node: 'gen_sql', seq: 2, label: 'SQL' } as StepPayload),
    ]
    const view = await mountPanel(makeTurn({ steps }))
    // 每步一张卡(直播与历史共用同一容器)
    expect(view.findAll('.step-wrap')).toHaveLength(2)
    // 历史轮没有 startedAt → 头部只给步数,不补一个时钟
    const head = view.find('.analysis-head-stats').text()
    expect(head).toContain('2 steps')
    // 有步骤就不是空态
    expect(view.text()).not.toContain('No steps recorded for this turn')
  })

  it('stepsTruncated > 0 → 截断注记写明被截步数', async () => {
    const view = await mountPanel(
      makeTurn({
        steps: [stepCardFromEvent({ node: 'gen_sql', seq: 1 } as StepPayload)],
        stepsTruncated: 7,
      }),
    )
    expect(view.find('.analysis-truncated').text()).toContain(
      '7 later steps were not saved with the history',
    )
  })

  it('零步骤的完结轮 → 「本轮无步骤记录」,不留纯空白', async () => {
    const view = await mountPanel(makeTurn({ steps: [] }))
    const note = view.find('.analysis-empty-turn')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('No steps recorded for this turn')
    // 不是整面板空态(那是「还没提问」的文案)
    expect(view.text()).not.toContain('Ask a question to see')
  })

  it('直播中零步骤不插话(状态条已在说明在跑)', async () => {
    const view = await mountPanel(
      makeTurn({ steps: [], status: 'streaming', startedAt: Date.now() }),
    )
    expect(view.find('.analysis-empty-turn').exists()).toBe(false)
  })

  it('无当前轮 → 仍是整面板空态', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useUiStore().lang = 'en'
    wrapper = mount(AnalysisPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.find('.analysis-empty').exists()).toBe(true)
    expect(wrapper.find('.analysis-empty-turn').exists()).toBe(false)
  })
})
