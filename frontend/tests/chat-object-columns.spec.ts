/**
 * ChatView × object_columns 传输线(E6/O2 前端收口)。
 *
 * 后端 summary.object_columns 必须**原样**传到答案卡的表格表头
 * (ChatView → MarkdownView → DataTable);键缺席 → 裸列名,与现状逐字节
 * 一致(判据是键在不在,不是版本号)。结构消费面(表头增强/值一字不改)
 * 已有专测(markdown-view.spec.ts)—— 这里只钉 ChatView 这一跳:
 * prop 接错线,后端算得再对表头也不会变。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiFetch: vi.fn(),
}))
vi.mock('../src/api/sse', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sse')>()
  return { ...actual, streamSse: vi.fn(actual.streamSse) }
})

import { apiGet } from '../src/api/http'
import { useChatStore } from '../src/stores/chat'
import type { DoneSummary } from '../src/api/types'
import ChatView from '../src/views/ChatView.vue'

const TABLE = [
  '| name | balance |',
  '| --- | --- |',
  '| 张伟 | 1000 |',
].join('\n')

let wrappers: VueWrapper[] = []

/** 摆一轮已完成的回答(绕开 SSE;store 状态机另有专测)。 */
function seedTurn(summary: DoneSummary) {
  const chat = useChatStore()
  chat.turns.push({
    question: 'q',
    thoughts: [],
    steps: [],
    answer: TABLE,
    summary,
    status: 'done',
  })
}

/**
 * ChatView 的 ``onMounted`` 会 ``clearSession()``(进页 = 新对话),挂载后
 * 再摆状态才是这一跳要测的姿位;挂载期两条旁路请求(mock 里按 URL 应答)
 * 只是别让它们把清单清空。
 */
async function mountWithTurn(summary: DoneSummary) {
  const w = mount(ChatView, {
    global: {
      plugins: [ElementPlus],
      stubs: { Sidebar: true, AnalysisPanel: true, RouterLink: true },
    },
    attachTo: document.body,
  })
  wrappers.push(w as VueWrapper)
  await flushPromises()
  seedTurn(summary)
  await flushPromises()
  return w
}

describe('ChatView — object_columns 传输线', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.mocked(apiGet)
      .mockReset()
      .mockImplementation(async (url: string) => {
        if (url.startsWith('/v1/catalog/datasources')) {
          return {
            datasources: [{ name: 'demo', type: 'sqlite', default: true }],
          }
        }
        return { sessions: [] }
      })
  })

  afterEach(() => {
    for (const w of wrappers) w.unmount()
    wrappers = []
    document.body.innerHTML = ''
  })

  it('summary.object_columns 原样到表头:「name · 客户」,值不动', async () => {
    const w = await mountWithTurn({
      columns: ['name', 'balance'],
      final_response: TABLE,
      object_columns: { name: { entity: '客户', field: 'name' } },
    })

    const ths = w.findAll('.data-table thead th')
    expect(ths[0]?.text()).toBe('name · 客户')
    expect(ths[0]?.attributes('title')).toBe('name')
    // 未映射列原样
    expect(ths[1]?.text()).toBe('balance')
    // 单元格值一字不改
    expect(w.find('.data-table tbody td')?.text()).toBe('张伟')
  })

  it('键缺席:表头就是裸列名,不加任何 title(与现状逐字节一致)', async () => {
    const w = await mountWithTurn({
      columns: ['name', 'balance'],
      final_response: TABLE,
    })

    const ths = w.findAll('.data-table thead th')
    expect(ths[0]?.text()).toBe('name')
    expect(ths[1]?.text()).toBe('balance')
    expect(ths.map((t) => t.attributes('title'))).toEqual([undefined, undefined])
  })

  it('显式 null 与键缺席同款渲染(协议两态都当「无可映射」)', async () => {
    const w = await mountWithTurn({
      columns: ['name', 'balance'],
      final_response: TABLE,
      object_columns: null,
    })

    const ths = w.findAll('.data-table thead th')
    expect(ths[0]?.text()).toBe('name')
    expect(ths[0]?.attributes('title')).toBeUndefined()
  })
})
