import { describe, it, expect } from 'vitest'
import { stepLabel, extractStep } from '../src/utils/steps'

describe('step payload extraction (backend `step` events carry detail.{...})', () => {
  it('extracts sql from the nested detail structure (backend shape)', () => {
    const card = {
      node: 'gen_sql',
      payload: {
        node: 'gen_sql',
        seq: 1,
        detail: { sql: 'SELECT * FROM t', attempts: 2 },
      },
    }
    const s = extractStep(card.payload as never)
    expect(s.sql).toBe('SELECT * FROM t')
  })

  it('extracts row_count / execution time from detail', () => {
    const card = {
      node: 'execute_sql',
      payload: {
        node: 'execute_sql',
        detail: { row_count: 5, execution_time_ms: 30 },
      },
    }
    const s = extractStep(card.payload as never)
    expect(s.rowCount).toBe(5)
    expect(s.timeMs).toBe(30)
  })

  it('extracts plan text from planner detail', () => {
    const card = {
      node: 'planner',
      payload: {
        node: 'planner',
        detail: { plan: '**Plan**: filter by region' },
      },
    }
    const s = extractStep(card.payload as never)
    expect(s.text).toContain('filter by region')
  })

  it('extracts intent + evidence from route_intent', () => {
    const card = {
      node: 'route_intent',
      payload: { node: 'route_intent', detail: { intent: 'query', llm: true } },
    }
    const s = extractStep(card.payload as never)
    expect(s.text).toContain('query')
  })

  it('extracts matched tables from schema_linking', () => {
    const card = {
      node: 'schema_linking',
      payload: {
        node: 'schema_linking',
        detail: { matched_tables: ['loan', 'account'], kb_terms: 2 },
      },
    }
    const s = extractStep(card.payload as never)
    expect(s.text).toContain('loan')
    expect(s.text).toContain('account')
  })

  it('builds link view (matching + sources) from link_detail + terms', () => {
    const card = {
      node: 'schema_linking',
      payload: {
        node: 'schema_linking',
        detail: {
          matched_tables: ['loan', 'account', 'district'],
          kb_terms: ['number of loan records'],
          link_detail: {
            notes_tables: ['loan', 'account'],
            value_hits: ["'Prague' → district.A3"],
            field_hits: ["'region' → district.A3"],
            relations: true,
            context: 'Table: district\nColumns: A3 (TEXT)',
          },
        },
      },
    }
    const s = extractStep(card.payload as never)
    expect(s.link?.tables).toEqual(['loan', 'account', 'district'])
    expect(s.link?.terms).toEqual(['number of loan records'])
    expect(s.link?.notesTables).toEqual(['loan', 'account'])
    expect(s.link?.relations).toBe(true)
    expect(s.link?.fieldHits).toContain("'region' → district.A3")
    // 上下文片段(执行日志)落在 text
    expect(s.text).toContain('Table: district')
  })

  it('falls back to legacy flat fields (old format)', () => {
    const card = {
      node: 'gen_sql',
      payload: { node: 'gen_sql', sql: 'SELECT 1', content: 'x' },
    }
    const s = extractStep(card.payload as never)
    expect(s.sql).toBe('SELECT 1')
  })

  it('maps node names to human labels', () => {
    expect(stepLabel('route_intent', 'zh')).toContain('意图')
    expect(stepLabel('schema_linking', 'zh')).toContain('关联')
    expect(stepLabel('planner', 'zh')).toContain('计划')
    expect(stepLabel('gen_sql', 'zh')).toContain('SQL')
    expect(stepLabel('execute_sql', 'zh')).toContain('执行')
    expect(stepLabel('reflect', 'en')).toContain('Reflect')
    expect(stepLabel('output', 'zh')).toContain('最终回答')
  })
})
