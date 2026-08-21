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
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })
    expect(chat.currentTurn?.status).toBe('streaming')
  })

  it('append-only answer chunks for intermediate done events in a batch', () => {
    const chat = useChatStore()
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })
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
    chat.onEvent({ type: 'done', data: { summary: { batched: true, final_response: 'last' } } })
    expect(t.status).toBe('done')
    expect(t.summary?.batched).toBe(true)
  })

  it('preserves summary and finalizes on a plain single-query done', () => {
    const chat = useChatStore()
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })

    chat.onEvent({
      type: 'done',
      data: {
        content: 'final answer',
        summary: { final_response: 'final answer', sql: 'SELECT * FROM t', verdict: 'OK' },
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
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })

    chat.onEvent({ type: 'thought', data: { content: 'thinking…' } })
    chat.onEvent({ type: 'step', data: { node: 'execute_sql', row_count: 10, execution_time_ms: 30 } })

    const t = chat.currentTurn!
    expect(t.thoughts).toContain('thinking…')
    expect(t.steps).toHaveLength(1)
    expect(t.steps[0].node).toBe('execute_sql')
    expect((t.steps[0].payload as any).row_count).toBe(10)
  })

  it('fails the turn on an error event', () => {
    const chat = useChatStore()
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })

    chat.onEvent({ type: 'error', data: { error: 'boom' } })
    const t = chat.currentTurn!
    expect(t.status).toBe('error')
    expect(t.error).toBe('boom')
  })

  it('enters hitl state with batch flag from task context', () => {
    const chat = useChatStore()
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })

    chat.onEvent({ type: 'hitl', data: { payload: { task_context: { total: 3 } } } })
    const t = chat.currentTurn!
    expect(t.status).toBe('hitl')
    expect(t.hitlBatch).toBe(true)
    expect(chat.pendingHitl?.batch).toBe(true)
  })

  it('batched done stores synthesis separately from per-task answers', () => {
    const chat = useChatStore()
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })
    chat.batchRunning = true

    chat.onEvent({ type: 'done', data: { summary: { final_response: 'task1 answer' } } })
    chat.onEvent({ type: 'done', data: { summary: { final_response: 'task2 answer' } } })
    // terminal batched done → synthesis block text, NOT mixed into per-task answers
    chat.onEvent({ type: 'done', data: { summary: { batched: true, final_response: '综合回答' } } })
    const t = chat.currentTurn!
    expect(t.status).toBe('done')
    expect(t.synthesis).toBe('综合回答')
    expect(t.answer).toContain('task1 answer')
    expect(t.answer).toContain('task2 answer')
    expect(t.answer).not.toContain('综合回答')
  })

  it('resume approve_all keeps synthesis out of the per-task answer', async () => {
    const chat = useChatStore()
    chat.turns.push({ question: 'q', thoughts: [], steps: [], answer: '', summary: null, status: 'streaming' })
    chat.onEvent({ type: 'hitl', data: { payload: { task_context: { total: 2 } } } })

    const body = [
      { type: 'task', data: { task_id: 't1', title: '任务A', status: 'done' } },
      { type: 'done', data: { summary: { final_response: 'task1 answer' } } },
      { type: 'task', data: { task_id: 't2', title: '任务B', status: 'done' } },
      { type: 'done', data: { summary: { final_response: 'task2 answer' } } },
      { type: 'done', data: { summary: { batched: true, final_response: '综合回答' } } },
    ].map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e.data)}\n\n`).join('')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })))
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
})
