import { vi } from 'vitest'
// Mock streamSse but delegate to the real implementation by default so the
// existing resume test (which stubs fetch and parses a real SSE body) keeps
// working; the datasource tests below override it per-test.
vi.mock('../src/api/sse', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sse')>()
  return { ...actual, streamSse: vi.fn(actual.streamSse) }
})
import { streamSse } from '../src/api/sse'
import { useUiStore } from '../src/stores/ui'
import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useChatStore } from '../src/stores/chat'

describe('chat store — SSE event state machine', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('starts a streaming turn on send placeholder state', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    expect(chat.currentTurn?.status).toBe('streaming')
  })

  it('append-only answer chunks for intermediate done events in a batch', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    chat.batchRunning = true

    // per-task done inside a batch → only appends the answer chunk
    chat.onEvent({
      type: 'done',
      data: { summary: { final_response: 'first chunk', sql: 'SELECT 1' } },
    })
    const t = chat.currentTurn!
    expect(t.status).toBe('streaming')
    expect(t.answer).toContain('first chunk')
    expect(t.steps).toHaveLength(1)
    expect(t.steps[0].node).toBe('gen_sql')

    // terminal batched done → finalize the turn
    chat.onEvent({
      type: 'done',
      data: { summary: { batched: true, final_response: 'last' } },
    })
    expect(t.status).toBe('done')
    expect(t.summary?.batched).toBe(true)
  })

  it('preserves summary and finalizes on a plain single-query done', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })

    chat.onEvent({
      type: 'done',
      data: {
        content: 'final answer',
        summary: {
          final_response: 'final answer',
          sql: 'SELECT * FROM t',
          verdict: 'OK',
        },
      },
    })
    const t = chat.currentTurn!
    expect(t.status).toBe('done')
    expect(t.answer).toContain('final answer')
    expect(t.answer.startsWith('\n\n')).toBe(false)
    expect(t.summary?.verdict).toBe('OK')
  })

  it('records thoughts and steps from their events', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })

    chat.onEvent({ type: 'thought', data: { content: 'thinking…' } })
    chat.onEvent({
      type: 'step',
      data: { node: 'execute_sql', row_count: 10, execution_time_ms: 30 },
    })

    const t = chat.currentTurn!
    expect(t.thoughts).toContain('thinking…')
    expect(t.steps).toHaveLength(1)
    expect(t.steps[0].node).toBe('execute_sql')
    expect((t.steps[0].payload as any).row_count).toBe(10)
  })

  it('tracks live (in-flight) nodes from begin events and resolves them on step', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
      live: [],
      startedAt: Date.now(),
    })

    chat.onEvent({ type: 'begin', data: { node: 'gen_sql' } })
    const t = chat.currentTurn!
    expect(t.live).toHaveLength(1)
    expect(t.live![0].node).toBe('gen_sql')
    expect(t.live![0].startedAt).toBeGreaterThan(0)

    // a re-fire of the same node (LangGraph node chain) must not double-count
    chat.onEvent({ type: 'begin', data: { node: 'gen_sql' } })
    expect(t.live).toHaveLength(1)

    // a step resolves the whole pending node chain
    chat.onEvent({
      type: 'step',
      data: { node: 'gen_sql', detail: { sql: 'SELECT 1' } },
    })
    expect(t.live).toHaveLength(0)
    expect(t.steps).toHaveLength(1)
  })

  it('tracks distinct live nodes while a parent node is pending', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
      live: [],
    })

    chat.onEvent({ type: 'begin', data: { node: 'gen_sql' } })
    chat.onEvent({ type: 'begin', data: { node: 'validate' } })
    const t = chat.currentTurn!
    expect(t.live!.map((s) => s.node)).toEqual(['gen_sql', 'validate'])
  })

  it('clears live nodes on a terminal done / error event', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
      live: [],
    })
    const t = chat.currentTurn!
    chat.onEvent({ type: 'begin', data: { node: 'gen_sql' } })
    expect(t.live).toHaveLength(1)

    chat.onEvent({
      type: 'error',
      data: { error: 'boom', summary: { error: 'boom' } },
    })
    expect(t.live).toHaveLength(0)
    expect(t.status).toBe('error')
    expect(t.error).toBe('boom')
  })

  it('fails the turn on an error event', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })

    chat.onEvent({ type: 'error', data: { error: 'boom' } })
    const t = chat.currentTurn!
    expect(t.status).toBe('error')
    expect(t.error).toBe('boom')
  })

  it('enters hitl state with batch flag from task context', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })

    chat.onEvent({
      type: 'hitl',
      data: { payload: { task_context: { total: 3 } } },
    })
    const t = chat.currentTurn!
    expect(t.status).toBe('hitl')
    expect(t.hitlBatch).toBe(true)
    expect(chat.pendingHitl?.batch).toBe(true)
  })

  it('batched done stores synthesis separately from per-task answers', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    chat.batchRunning = true

    chat.onEvent({
      type: 'done',
      data: { summary: { final_response: 'task1 answer' } },
    })
    chat.onEvent({
      type: 'done',
      data: { summary: { final_response: 'task2 answer' } },
    })
    // terminal batched done → synthesis block text, NOT mixed into per-task answers
    chat.onEvent({
      type: 'done',
      data: { summary: { batched: true, final_response: '综合回答' } },
    })
    const t = chat.currentTurn!
    expect(t.status).toBe('done')
    expect(t.synthesis).toBe('综合回答')
    expect(t.answer).toContain('task1 answer')
    expect(t.answer).toContain('task2 answer')
    expect(t.answer).not.toContain('综合回答')
  })

  it('resume approve_all keeps synthesis out of the per-task answer', async () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    chat.onEvent({
      type: 'hitl',
      data: { payload: { task_context: { total: 2 } } },
    })

    const body = [
      { type: 'task', data: { task_id: 't1', title: '任务A', status: 'done' } },
      { type: 'done', data: { summary: { final_response: 'task1 answer' } } },
      { type: 'task', data: { task_id: 't2', title: '任务B', status: 'done' } },
      { type: 'done', data: { summary: { final_response: 'task2 answer' } } },
      {
        type: 'done',
        data: { summary: { batched: true, final_response: '综合回答' } },
      },
    ]
      .map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e.data)}\n\n`)
      .join('')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(body, { status: 200 })),
    )
    try {
      await chat.resume('approve_all')
    } finally {
      vi.unstubAllGlobals()
    }

    const t = chat.currentTurn!
    expect(t.status).toBe('done')
    expect(t.synthesis).toBe('综合回答')
    expect(t.answer).toContain('task1 answer')
    expect(t.answer).toContain('task2 answer')
    expect(t.answer).not.toContain('综合回答')
  })

  it('includes the selected datasource in the chat body', async () => {
    const ui = useUiStore()
    ui.setDatasource('financial')
    const chat = useChatStore()
    const mocked = vi
      .mocked(streamSse)
      .mockClear()
      .mockResolvedValue(new Response(null, { status: 200 }))
    // send() tail-calls listSessions → apiGet → fetch; keep it off the network
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(new Response('{"sessions": []}', { status: 200 })),
    )
    try {
      await chat.send('q')
    } finally {
      vi.unstubAllGlobals()
    }
    const body = mocked.mock.calls[0][1] as Record<string, unknown>
    expect(body.datasource).toBe('financial')
    expect(body.question).toBe('q')
  })

  it('omits datasource when none is selected', async () => {
    const chat = useChatStore()
    const mocked = vi
      .mocked(streamSse)
      .mockClear()
      .mockResolvedValue(new Response(null, { status: 200 }))
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(new Response('{"sessions": []}', { status: 200 })),
    )
    try {
      await chat.send('q')
    } finally {
      vi.unstubAllGlobals()
    }
    const body = mocked.mock.calls[0][1] as Record<string, unknown>
    expect('datasource' in body).toBe(false)
  })

  // ── 方案 ②:末条书签 → 待确认态 + pendingHitl 回填 ──────────────
  function stubSessionFetch(body: Record<string, unknown>) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).endsWith('/tasks')) {
          return new Response('{"tasks": []}', { status: 200 })
        }
        return new Response(JSON.stringify(body), { status: 200 })
      }),
    )
  }

  it('loadSession 把末条 pending 书签回填为 pendingHitl(确认卡即插即用)', async () => {
    const chat = useChatStore()
    chat.pendingHitl = { sessionId: 'old', workflow: 'reflection', batch: false }
    stubSessionFetch({
      messages: [
        { role: 'user', content: 'q1' },
        {
          role: 'assistant',
          content: '确认执行吗?',
          metadata: {
            hitl: {
              status: 'pending',
              run_id: 'r1',
              workflow: 'reflection',
              batch: true,
            },
            steps: [{ node: 'gen_sql', seq: 1 }],
          },
        },
      ],
    })
    try {
      await chat.loadSession('s1')
    } finally {
      vi.unstubAllGlobals()
    }
    expect(chat.currentTurn?.status).toBe('hitl')
    expect(chat.currentTurn?.steps.map((s) => s.node)).toEqual(['gen_sql'])
    expect(chat.pendingHitl).toEqual({
      sessionId: 's1',
      workflow: 'reflection',
      batch: true,
    })
  })

  it('loadSession 换到无书签会话时清掉残留 pendingHitl', async () => {
    const chat = useChatStore()
    chat.pendingHitl = { sessionId: 'old', workflow: 'reflection', batch: false }
    stubSessionFetch({
      messages: [
        { role: 'user', content: 'q1' },
        {
          role: 'assistant',
          content: 'a1',
          metadata: { summary: { final_response: 'a1' } },
        },
      ],
    })
    try {
      await chat.loadSession('s2')
    } finally {
      vi.unstubAllGlobals()
    }
    expect(chat.pendingHitl).toBeNull()
    expect(chat.currentTurn?.status).toBe('done')
  })
})

// ── 失败轮次的呈现接线:结构化错误随事件进来,卡片据此渲染 ──────────
describe('chat store — failed turn carries structured error', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  function streamingTurn(chat: ReturnType<typeof useChatStore>) {
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    return chat.currentTurn!
  }

  const INFO = {
    kind: 'gave_up',
    title: '这次没能给出可靠结果',
    explanation: '系统自动修正了 3 轮,仍然没能算稳。',
    retryable: true,
    detail: { raw: '回退目标 schema_linking 连续失败', node: 'schema_linking' },
  }

  it('captures error_info from an error event alongside the raw message', () => {
    const chat = useChatStore()
    const turn = streamingTurn(chat)
    chat.onEvent({
      type: 'error',
      data: { error: 'boom', summary: { error: 'boom', error_info: INFO } },
    })
    expect(turn.status).toBe('error')
    expect(turn.errorInfo?.title).toBe(INFO.title)
    // 原始串照旧保留给诊断,不被结构化文案顶掉
    expect(turn.error).toBe('boom')
  })

  it('captures error_info from a done event so the card still renders', () => {
    const chat = useChatStore()
    const turn = streamingTurn(chat)
    chat.onEvent({
      type: 'done',
      data: {
        summary: {
          final_response: '**错误**: 回退目标 schema_linking 连续失败',
          error: '回退目标 schema_linking 连续失败',
          error_info: INFO,
        },
      },
    })
    expect(turn.errorInfo?.title).toBe(INFO.title)
  })

  it('restores error_info with a persisted session', async () => {
    const { restoreTurns } = await import('../src/stores/chat')
    const turns = restoreTurns([
      { role: 'user', content: 'q' },
      {
        role: 'assistant',
        content: '**错误**: boom',
        metadata: { summary: { error: 'boom', error_info: INFO, final_response: '' } },
      },
    ] as never)
    expect(turns[0].errorInfo?.title).toBe(INFO.title)
  })
})
