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
    // 旧会话没有 steps metadata → 步骤仍为空(面板走「本轮无步骤记录」空态)
    expect(turns[0].steps).toHaveLength(0)
  })

  it('① 重建落盘的分析步骤(与直播同一映射),steps_truncated 透出', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      {
        role: 'assistant',
        content: 'a1',
        metadata: {
          summary: { final_response: 'a1' },
          steps: [
            { node: 'schema_linking', seq: 1, detail: { tables: ['card'] } },
            { node: 'gen_sql', seq: 2, label: 'SQL 生成' },
          ],
          steps_truncated: 7,
        },
      },
    ])
    expect(turns[0].steps.map((s) => s.node)).toEqual([
      'schema_linking',
      'gen_sql',
    ])
    // label 沿用直播口径:取 payload.label,缺席回落 node
    expect(turns[0].steps[1].label).toBe('SQL 生成')
    expect(turns[0].steps[0].payload).toMatchObject({ seq: 1 })
    expect(turns[0].stepsTruncated).toBe(7)
    expect(turns[0].unfinished).toBeUndefined()
  })

  it('① 中断-恢复同轮:书签步骤 + 恢复后步骤并为一条时间线', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      {
        role: 'assistant',
        content: '确认执行?',
        metadata: {
          hitl: { status: 'pending', run_id: 'r1', workflow: 'reflection' },
          steps: [{ node: 'gen_sql', seq: 1 }],
          summary: { hitl_status: 'pending' },
        },
      },
      {
        role: 'assistant',
        content: 'a1',
        metadata: {
          summary: { final_response: 'a1' },
          steps: [{ node: 'execute_sql', seq: 2 }],
        },
      },
    ])
    expect(turns).toHaveLength(1)
    expect(turns[0].steps.map((s) => s.node)).toEqual(['gen_sql', 'execute_sql'])
    // 书签不在末条(其后已落答案)→ 不是待确认态,而是已完成轮次
    expect(turns[0].status).toBe('done')
    expect(turns[0].answer).toBe('a1')
    expect(turns[0].unfinished).toBeUndefined()
  })

  it('② 末条 pending 书签 → 待确认态(确认文案不进答案气泡)', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      {
        role: 'assistant',
        content: '确认执行这条 SQL 吗?',
        metadata: {
          hitl: {
            status: 'pending',
            run_id: 'r1',
            workflow: 'reflection',
            datasource: 'demo',
            batch: true,
          },
          steps: [{ node: 'gen_sql', seq: 1 }],
          summary: { hitl_status: 'pending', sql: 'SELECT 1' },
        },
      },
    ])
    expect(turns).toHaveLength(1)
    expect(turns[0].status).toBe('hitl')
    expect(turns[0].hitlBatch).toBe(true)
    expect(turns[0].hitlWorkflow).toBe('reflection')
    expect(turns[0].hitlActionsShown).toBe(false)
    // 书签 = 中断现场,不是答案:确认文案不落进答案气泡(HitlCard 呈现)
    expect(turns[0].answer).toBe('')
    // 待确认 ≠ 未完成(它有一条明确的下一步:确认或否决)
    expect(turns[0].unfinished).toBe(false)
    // 中断前已收集的步骤照常可见
    expect(turns[0].steps.map((s) => s.node)).toEqual(['gen_sql'])
  })

  it('② 非 pending 书签不恢复待确认态', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      {
        role: 'assistant',
        content: 'x',
        metadata: { hitl: { status: 'resolved', run_id: 'r1' } },
      },
    ])
    expect(turns[0].status).toBe('done')
    expect(turns[0].unfinished).toBe(true)
  })

  it('⑤ 末条停在 user 消息 → 该轮未完成(问了没答)', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'a1', metadata: { summary: { final_response: 'a1' } } },
      { role: 'user', content: 'q2' },
    ])
    expect(turns).toHaveLength(2)
    expect(turns[0].unfinished).toBeUndefined()
    expect(turns[1].unfinished).toBe(true)
  })

  it('⑤ 中断后被新提问丢弃书签的中间轮同样标未完成', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1' },
      {
        role: 'assistant',
        content: '确认?',
        metadata: { hitl: { status: 'pending', run_id: 'r1' } },
      },
      { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'a2', metadata: { summary: { final_response: 'a2' } } },
    ])
    expect(turns).toHaveLength(2)
    // q1 的书签已被 q2 作废(只认末条),且没有答案 → 未完成
    expect(turns[0].unfinished).toBe(true)
    expect(turns[0].status).toBe('done')
    expect(turns[1].unfinished).toBeUndefined()
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

  it('还原 assistant 消息的 timestamp 为 turn.at;缺失则不填', () => {
    const turns = restoreTurns([
      { role: 'user', content: 'q1', timestamp: '2026-10-01T10:00:00Z' },
      {
        role: 'assistant',
        content: 'a1',
        timestamp: '2026-10-01T10:00:05Z',
        metadata: {},
      },
      { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'a2', metadata: {} },
    ])
    // 溯源条的「生成时间」= 答案落盘时刻(assistant 消息 timestamp),
    // 不是用户提问时刻。
    expect(turns[0].at).toBe('2026-10-01T10:00:05Z')
    // 拿不到就不填 —— 渲染层省掉时间片段,而不是编一个。
    expect(turns[1].at).toBeUndefined()
  })
})