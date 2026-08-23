import { describe, it, expect } from 'vitest'
import { restoreTurns } from '../src/stores/chat'

describe('restoreTurns — 历史会话还原', () => {
  it('user+assistant 消息还原为一轮 turn(含 summary)', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'How many cards?', metadata: { workflow: 'reflection' } },
      {
        role: 'assistant',
        content: '## Answer',
        metadata: {
          sql: 'SELECT COUNT(*) FROM card',
          summary: {
            sql: 'SELECT COUNT(*) FROM card',
            row_count: 1,
            verdict: 'OK',
            final_response: '## Answer\n\n**Question**: ...',
          },
        },
      },
    ])
    expect(turns).toHaveLength(1)
    expect(turns[0].question).toBe('How many cards?')
    expect(turns[0].status).toBe('done')
    expect(turns[0].answer).toBe('## Answer\n\n**Question**: ...')
    expect(turns[0].summary?.sql).toBe('SELECT COUNT(*) FROM card')
    // 历史会话不重建分析步骤:右侧面板无可展开的分析过程
    expect(turns[0].steps).toHaveLength(0)
  })

  it('旧会话(无 summary metadata)仍回退为纯文本 answer', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'legacy answer', metadata: {} },
    ])
    expect(turns).toHaveLength(1)
    expect(turns[0].answer).toBe('legacy answer')
    expect(turns[0].summary).toBeNull()
    expect(turns[0].steps).toHaveLength(0)
  })

  it('多轮交替消息正确分组', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'a1', metadata: {} },
      { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'a2', metadata: {} },
    ])
    expect(turns).toHaveLength(2)
    expect(turns.map((t) => t.question)).toEqual(['q1', 'q2'])
    expect(turns.map((t) => t.answer)).toEqual(['a1', 'a2'])
  })

  it('孤立 assistant 消息被丢弃(无对应问题)', () => {
    const turns = restoreTurns([{ role: 'assistant', content: 'orphan' }])
    expect(turns).toHaveLength(0)
  })
})