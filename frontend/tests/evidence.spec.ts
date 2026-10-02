import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import EvidenceDrawer from '../src/components/chat/EvidenceDrawer.vue'
import { apiGet, apiPost } from '../src/api/http'
import { useChatStore } from '../src/stores/chat'
import { useUiStore } from '../src/stores/ui'
import type { Turn } from '../src/stores/chat'
import type { RunReplay } from '../src/api/types'
import type { VueWrapper } from '@vue/test-utils'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

let wrapper: VueWrapper | null = null

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    question: '哪个地区的平均贷款金额最高?',
    thoughts: [],
    steps: [],
    answer: 'north 最高。',
    status: 'done',
    at: '2026-10-02T14:03:12',
    summary: {
      run_id: '3f9a2c1b-1111-2222-3333-444455556666',
      datasource: 'financial',
      model: 'mock/gen',
      sql: 'SELECT district, AVG(amount) FROM loan GROUP BY district',
      columns: ['district', 'avg_loan_amount'],
      row_count: 5,
      rows: [
        ['north', 264200],
        ['central', 218500],
        ['south', 190700],
        ['east', 180000],
        ['west', 170000],
      ],
      verdict: 'OK',
      total_elapsed_ms: 1400,
      execution_evidence: {
        estimated_rows: 15000,
        source: 'table_profile',
        limit_applied: 1000,
        data_as_of: '2026-09-30',
        as_of_basis: 'table_profile',
      },
      confidence: 0.82,
      confidence_evidence: [{ kind: 'result', key: 'self_check', why: '结果自检通过' }],
      ...overrides.summary,
    },
    ...overrides,
  }
}

async function mountDrawer(turn: Turn | null, turnIndex = 0, focus?: 'evidence' | 'replay') {
  const pinia = createPinia()
  setActivePinia(pinia)
  // 组件挂在这个新 pinia 上,语言必须在这同一个 pinia 上设(ui store 的 lang
  // 初值只从 localStorage 读一次,另建的 pinia 拿不到 beforeEach 里设的值)。
  useUiStore().lang = 'en'
  if (turn) useChatStore().turns.push(turn)
  wrapper = mount(EvidenceDrawer, {
    props: { modelValue: true, turn, turnIndex, focus },
    global: { plugins: [pinia], stubs: { teleport: true } },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('EvidenceDrawer — 依据收拢', () => {
  it('renders SQL, execution highlights, preview (3 of 5) and checks', async () => {
    ;(apiGet as any).mockRejectedValue(new Error('no trace'))
    const view = await mountDrawer(makeTurn())
    const text = view.text()
    expect(text).toContain('Generated SQL')
    expect(text).toContain('AVG(amount)')
    expect(text).toContain('Execution & plan')
    expect(text).toContain('Rows returned')
    expect(text).toContain('15,000') // estimated scan
    expect(text).toContain('table_profile')
    expect(text).toContain('2026-09-30')
    expect(text).toContain('Result preview')
    expect(text).toContain('first 3 rows · of 5 rows')
    // 只画前 3 行,第 4/5 行不出现
    expect(text).not.toContain('east')
    expect(text).toContain('Validation & reflection')
    expect(text).toContain('Reflection verdict = OK')
    expect(text).toContain('结果自检通过')
  })

  it('omits sections with no data (never invents a process)', async () => {
    ;(apiGet as any).mockRejectedValue(new Error('no trace'))
    const view = await mountDrawer(
      makeTurn({ summary: { sql: '', rows: [], columns: [], verdict: '', execution_evidence: null } }),
    )
    const text = view.text()
    expect(text).not.toContain('Generated SQL')
    expect(text).not.toContain('Result preview')
    expect(text).not.toContain('Validation & reflection')
    expect(text).not.toContain('Run replay') // fetch 失败 → 整节不渲染
    // 反馈入口仍在(它是入口,不依赖数据)
    expect(text).toContain('Feedback')
  })

  it('does not call the replay endpoint without a run_id', async () => {
    await mountDrawer(makeTurn({ summary: { run_id: '', sql: 'SELECT 1' } }))
    expect(apiGet).not.toHaveBeenCalled()
    expect(wrapper!.text()).not.toContain('Run replay')
  })

  it('trace replay renders nodes / LLM / tools and a completeness note', async () => {
    const replay: RunReplay = {
      run_id: '3f9a2c1b-1111-2222-3333-444455556666',
      source: 'trace',
      complete: false,
      timeline: [
        { name: 'gen_sql', seq: 1, depth: 0, elapsed_ms: 1200, tokens: { total: 15 }, status: 'ok' },
        { name: 'execute_sql', seq: 2, depth: 0, elapsed_ms: null, status: 'running' },
      ],
      llm_calls: [{ node: 'gen_sql', model: 'mock/gen', elapsed_ms: 900, tokens: { total: 15 } }],
      tools: [{ name: 'validate_sql', node: 'gen_sql' }],
    }
    ;(apiGet as any).mockResolvedValue(replay)
    const view = await mountDrawer(makeTurn())
    expect(apiGet).toHaveBeenCalledWith(
      '/v1/runs/3f9a2c1b-1111-2222-3333-444455556666',
    )
    const text = view.text()
    expect(text).toContain('Run replay')
    expect(text).toContain('gen_sql')
    expect(text).toContain('1.2s')
    expect(text).toContain('15 tok')
    expect(text).toContain('running')
    expect(text).toContain('validate_sql')
    expect(text).toContain('did not reach a terminal state')
  })

  it('session-source replay says "terminal summary only" — no fake process', async () => {
    ;(apiGet as any).mockResolvedValue({
      run_id: 'r',
      source: 'session',
      complete: true,
      timeline: [],
      llm_calls: [],
      tools: [],
    } as RunReplay)
    const view = await mountDrawer(makeTurn())
    expect(view.text()).toContain('Terminal summary only')
  })
})

describe('EvidenceDrawer — 反馈入口(复用评分通道)', () => {
  it('upvote submits vote=1 and shows a receipt that does not promise adoption', async () => {
    ;(apiGet as any).mockRejectedValue(new Error('no trace'))
    ;(apiPost as any).mockResolvedValue({ status: 'ok' })
    const view = await mountDrawer(makeTurn())
    const buttons = view.findAll('.ev-fbbtn')
    await buttons[0].trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/v1/kb/ratings',
      expect.objectContaining({ question: '哪个地区的平均贷款金额最高?', vote: 1 }),
    )
    const text = view.find('.ev-receipt').text()
    expect(text).toContain('admin')
    expect(text).not.toContain('adopted')
  })

  it('downvote submits vote=-1 and lands as a pending draft (not "adopted")', async () => {
    ;(apiGet as any).mockRejectedValue(new Error('no trace'))
    ;(apiPost as any).mockResolvedValue({ status: 'ok' })
    const view = await mountDrawer(makeTurn())
    await view.findAll('.ev-fbbtn')[1].trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/v1/kb/ratings',
      expect.objectContaining({ vote: -1 }),
    )
    expect(view.find('.ev-receipt').text()).toContain('pending draft')
  })

  it('a failed submit shows an honest failure line, not a fake receipt', async () => {
    ;(apiGet as any).mockRejectedValue(new Error('no trace'))
    ;(apiPost as any).mockRejectedValue(new Error('boom'))
    const view = await mountDrawer(makeTurn())
    await view.findAll('.ev-fbbtn')[0].trigger('click')
    await flushPromises()
    expect(view.find('.ev-receipt.warn').text()).toContain('failed')
  })
})
