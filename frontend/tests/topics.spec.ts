/**
 * 主题域(问数范围收敛)—— 选择器 / 请求体 / 起始问句 / 换源重置。
 *
 * 契约来自 GET /v1/semantic/topics(见 src/api/topics.ts):
 *   · `topics: []` 或 404(该源无语义模型)→ 选择器整体不出现,不是错误态;
 *   · `status: "empty_scope"`(域声明还在、数据集已失效)→ 照常列出但置灰,
 *     静默消失会让用户带着一个选不中的旧值继续提问;
 *   · 只有「在当前清单里且 status=ok」的域才会随请求带出去 —— 换源残留名、
 *     后来过期的域,一律退化成「不限定」而不是让后端去拒绝。
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
import { streamSse } from '../src/api/sse'
import { useUiStore } from '../src/stores/ui'
import { useChatStore } from '../src/stores/chat'
import type { TopicInfo } from '../src/api/topics'
import TopicSelect from '../src/components/chat/TopicSelect.vue'
import ChatView from '../src/views/ChatView.vue'

function topic(over: Partial<TopicInfo> = {}): TopicInfo {
  return {
    name: 'Loans',
    description: 'Loan portfolio and repayments',
    synonyms: [],
    datasets: ['loan'],
    scope: ['loan', 'account'],
    status: 'ok',
    metrics: [],
    examples: [],
    ...over,
  }
}

const TOPICS: TopicInfo[] = [
  topic({ examples: ['Which district has the highest average loan?'] }),
  topic({
    name: 'Cards',
    description: 'Card issue and usage',
    datasets: ['card'],
    scope: [],
    status: 'empty_scope',
  }),
]

/** 直接摆好 store 状态,绕开真实的接口拉取(那部分由换源测试单独验)。 */
function seed(
  ds = 'demo',
  topics: TopicInfo[] = TOPICS,
  selected = '',
  extraSources: string[] = [],
) {
  const ui = useUiStore()
  ui.datasourceList = [
    { name: ds, type: 'sqlite', default: true },
    ...extraSources.map((n) => ({ name: n, type: 'sqlite' })),
  ]
  ui.datasourcesLoaded = true
  ui.setDatasource(ds)
  ui.topicList = topics
  ui.topicsFor = ds
  ui.topicsLoaded = true
  if (selected) ui.setTopic(selected)
  return ui
}

function mockApi(topicsByDs: Record<string, TopicInfo[]>) {
  vi.mocked(apiGet).mockImplementation(async (url: string) => {
    if (url.startsWith('/v1/semantic/topics')) {
      const ds = decodeURIComponent(url.split('datasource=')[1] ?? '')
      return { datasource: ds, topics: topicsByDs[ds] ?? [] }
    }
    if (url.startsWith('/v1/catalog/datasources')) {
      // Composer 挂载即拉数据源清单;这里必须回同一批名字,
      // 否则 store 的清单被清空,选择器会连带消失(与主题域无关的旁路)。
      return {
        datasources: Object.keys(topicsByDs).map((name) => ({
          name,
          type: 'sqlite',
        })),
      }
    }
    if (url.startsWith('/v1/sessions')) return { sessions: [] }
    return {}
  })
}

let wrappers: VueWrapper[] = []

function mountView(comp: unknown) {
  const w = mount(comp as never, {
    global: {
      plugins: [ElementPlus],
      stubs: { Sidebar: true, AnalysisPanel: true, RouterLink: true },
    },
    attachTo: document.body,
  })
  wrappers.push(w as VueWrapper)
  return w
}

/** 下拉项在 teleport 到 body 的 popper 里(Element Plus persistent),按文档查。 */
function dropdownItems(): HTMLElement[] {
  return Array.from(document.body.querySelectorAll<HTMLElement>('.el-select-dropdown__item'))
}

describe('topic domains', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.mocked(apiGet).mockReset().mockResolvedValue({ sessions: [] })
    vi.mocked(streamSse).mockReset()
  })

  afterEach(() => {
    for (const w of wrappers) w.unmount()
    wrappers = []
    document.body.innerHTML = ''
  })

  // ① 无主题域 → 选择器不出现(不是禁用态、不是空下拉)
  it('hides the selector when the datasource declares no topics', async () => {
    seed('demo', [])
    mockApi({ demo: [] })
    const w = mountView(ChatView)
    await flushPromises()
    expect(w.find('.chat-ds-select').exists()).toBe(true)
    expect(w.find('.chat-topic-select').exists()).toBe(false)
  })

  // ② 有域 → 出现且列出(名字/描述/scope 表数;首个选项是「不限定」)
  it('lists domains with description and scope count', async () => {
    seed()
    mountView(TopicSelect)
    await flushPromises()
    const items = dropdownItems()
    expect(items.map((i) => i.textContent)).toContain('不限定主题')
    const loans = items.find((i) => i.textContent?.includes('Loans'))
    expect(loans).toBeTruthy()
    expect(loans!.textContent).toContain('Loan portfolio and repayments')
    expect(loans!.textContent).toContain('2 张表')
  })

  // ③ empty_scope → 照常列出但置灰(选了必然被拒,挡在手滑之前)
  it('keeps expired domains listed but unselectable', async () => {
    seed()
    mountView(TopicSelect)
    await flushPromises()
    const cards = dropdownItems().find((i) => i.textContent?.includes('Cards'))
    expect(cards).toBeTruthy()
    expect(cards!.textContent).toContain('已失效')
    expect(cards!.classList.contains('is-disabled')).toBe(true)
  })

  // ④ 选中后请求体带 topic
  it('sends the selected topic in the chat body', async () => {
    const ui = seed('demo', TOPICS, 'Loans')
    expect(ui.activeTopic).toBe('Loans')
    const chat = useChatStore()
    const mocked = vi
      .mocked(streamSse)
      .mockResolvedValue(new Response(null, { status: 200 }))
    await chat.send('which district has the most loans')
    const body = mocked.mock.calls[0][1] as Record<string, unknown>
    expect(body.topic).toBe('Loans')
  })

  // ④' 过期/不在清单里的域不随请求带出去(退化成「不限定」)
  it('drops expired or unknown topics from the request', async () => {
    const ui = seed('demo', TOPICS, 'Cards') // empty_scope
    expect(ui.activeTopic).toBe('')
    ui.setTopic('Ghost') // 不在清单里
    expect(ui.activeTopic).toBe('')
    const chat = useChatStore()
    const mocked = vi
      .mocked(streamSse)
      .mockResolvedValue(new Response(null, { status: 200 }))
    await chat.send('q')
    const body = mocked.mock.calls[0][1] as Record<string, unknown>
    expect('topic' in body).toBe(false)
  })

  // ⑤ 起始问句:选中域后换成该域示例,点击只填输入框不直接发送
  it('fills the composer from a domain starter example', async () => {
    seed('demo', TOPICS, 'Loans')
    mockApi({ demo: TOPICS })
    const w = mountView(ChatView)
    await flushPromises()
    expect(w.find('.empty-examples-title').text()).toBe('从这里开始问')
    const chips = w.findAll('.empty-example-btn')
    expect(chips).toHaveLength(1)
    expect(chips[0].text()).toBe('Which district has the highest average loan?')
    await chips[0].trigger('click')
    await flushPromises()
    expect(
      (w.find('.empty-center textarea').element as HTMLTextAreaElement).value,
    ).toBe('Which district has the highest average loan?')
  })

  // ⑥ 切数据源:选择立即重置、清单按新源重拉、旧的域不再出现
  it('resets the selection and refetches topics on datasource switch', async () => {
    seed('demo', TOPICS, 'Loans', ['other'])
    mockApi({ demo: TOPICS, other: [] })
    const ui = useUiStore()
    const w = mountView(ChatView)
    await flushPromises()
    expect(w.find('.chat-topic-select').exists()).toBe(true)

    ui.setDatasource('other')
    expect(ui.topic).toBe('')
    expect(ui.activeTopic).toBe('')
    await flushPromises()
    expect(
      vi.mocked(apiGet).mock.calls.some((c) => String(c[0]).includes('datasource=other')),
    ).toBe(true)
    expect(w.find('.chat-topic-select').exists()).toBe(false)
  })

  // ⑥' 同源重复加载(会话切回同源)命中缓存:不重复拉取,但按清单校正选择
  it('revalidates the selection against a cached list without refetching', async () => {
    const ui = useUiStore()
    ui.datasourceList = [{ name: 'demo', type: 'sqlite', default: true }]
    ui.datasourcesLoaded = true
    ui.setDatasource('demo')
    mockApi({ demo: TOPICS })
    await ui.loadTopics('demo')
    const calls = vi.mocked(apiGet).mock.calls.length
    expect(calls).toBe(1)

    ui.setTopic('Loans')
    await ui.loadTopics('demo')
    expect(vi.mocked(apiGet).mock.calls.length).toBe(calls)
    expect(ui.activeTopic).toBe('Loans')

    ui.setTopic('Cards') // 清单里是 empty_scope 的域
    await ui.loadTopics('demo')
    expect(ui.topic).toBe('')
    expect(ui.activeTopic).toBe('')
  })
})
