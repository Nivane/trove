import { describe, it, expect } from 'vitest'
import {
  stepLabel,
  extractStep,
  signalLabel,
  backendLabel,
  backendDetail,
  complexityLabel,
  compileOutcomeLabel,
  missReasonLabel,
  planStatusLabel,
  fixModeLabel,
  progressLabel,
  blockLabel,
  ruleLabel,
  fmtTokens,
} from '../src/utils/steps'

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

  it('extracts plan text from query_sketch detail', () => {
    const card = {
      node: 'query_sketch',
      payload: {
        node: 'query_sketch',
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
    expect(stepLabel('query_sketch', 'zh')).toContain('计划')
    expect(stepLabel('gen_sql', 'zh')).toContain('SQL')
    expect(stepLabel('execute_sql', 'zh')).toContain('执行')
    expect(stepLabel('reflect', 'en')).toContain('Reflect')
    expect(stepLabel('output', 'zh')).toContain('最终回答')
  })
})

// ── 分析面板文案:把机器 token 翻成人话,未知值优雅退化 ──────────
describe('analysis panel labels', () => {
  it('keeps raw signal keys in the view so the label layer can translate', () => {
    const card = {
      node: 'route_intent',
      payload: {
        node: 'route_intent',
        detail: {
          intent: 'query',
          intent_evidence: { strong_match: true, data_signal: true },
        },
      },
    }
    const s = extractStep(card.payload as never)
    expect(s.intentEvidence?.signals).toEqual(['strong_match', 'data_signal'])
  })

  it('humanises intent signals instead of leaking snake_case fragments', () => {
    expect(signalLabel('strong_match', 'zh')).toBe('强匹配')
    expect(signalLabel('data_signal', 'zh')).toBe('数据问句')
    expect(signalLabel('weak_signal', 'zh')).toBe('弱匹配')
    expect(signalLabel('history_present', 'zh')).toBe('有历史上下文')
    expect(signalLabel('strong_match', 'en')).toBe('strong match')
    // 新信号(后端加而前端未教)退化成可读词,而不是下划线原文
    expect(signalLabel('shiny_new_signal', 'zh')).toBe('shiny new')
    expect(signalLabel('', 'zh')).toBe('')
  })

  it('names retrieval backends in plain language, tech detail in the tooltip', () => {
    expect(backendLabel('builtin', 'zh')).toBe('关键词检索')
    expect(backendLabel('pg_hybrid', 'zh')).toBe('混合检索')
    expect(backendLabel('rag', 'zh')).toBe('向量检索')
    expect(backendDetail('builtin', 'zh')).toContain('FTS5')
    expect(backendDetail('pg_hybrid', 'zh')).toContain('RRF')
    // 未知后端照原样显示,不吞掉信息
    expect(backendLabel('weird', 'zh')).toBe('weird')
    expect(backendLabel('', 'zh')).toBe('')
  })

  it('labels complexity tiers', () => {
    expect(complexityLabel('simple', 'zh')).toBe('简单')
    expect(complexityLabel('complex', 'zh')).toBe('复杂')
    expect(complexityLabel('standard', 'en')).toBe('standard')
    expect(complexityLabel('mystery', 'zh')).toBe('mystery')
  })

  it('labels the compile outcome and miss reason', () => {
    expect(compileOutcomeLabel('compiled', 'zh')).toBe('已编译')
    expect(compileOutcomeLabel('partial', 'zh')).toBe('部分编译')
    expect(compileOutcomeLabel('miss', 'zh')).toBe('未编译')
    expect(missReasonLabel('no_metric_match', 'zh')).toBe('缺少指标声明')
    expect(missReasonLabel('fan_out', 'zh')).toBe('联表会重复计数')
    // 编译器新增的分因前端不认得 —— 露出 slug(管理端要拿去对日志),不编造
    expect(missReasonLabel('brand_new_reason', 'zh')).toBe('brand_new_reason')
  })

  it('labels plan check, fix mode and regression progress', () => {
    expect(planStatusLabel('ok', 'zh')).toBe('通过')
    expect(planStatusLabel('dropped', 'zh')).toBe('已丢弃')
    expect(fixModeLabel('fixer', 'zh')).toBe('定点修复')
    expect(fixModeLabel('revisor', 'zh')).toBe('语义重写')
    expect(progressLabel('improved', 'zh')).toBe('有进展')
    expect(progressLabel('none', 'zh')).toBe('无进展')
    expect(progressLabel('invalid', 'zh')).toBe('原地打转')
    expect(progressLabel('first', 'zh')).toBe('首次失败')
  })

  it('labels context budget blocks', () => {
    expect(blockLabel('few_shots', 'zh')).toBe('示例')
    expect(blockLabel('term_notes', 'zh')).toBe('术语备注')
    expect(blockLabel('unknown_block', 'zh')).toBe('unknown block')
    expect(fmtTokens(820)).toBe('820 tokens')
    expect(fmtTokens(1200)).toBe('1.2k tokens')
    expect(fmtTokens(undefined)).toBe('')
  })

  it('maps rule ids to their family in plain language', () => {
    expect(ruleLabel('F1-b', 'zh')).toBe('形状')
    expect(ruleLabel('F2-a', 'zh')).toBe('过滤条件')
    expect(ruleLabel('F4-a', 'zh')).toBe('排序')
    expect(ruleLabel('count-multirow', 'zh')).toBe('计数形状')
    expect(ruleLabel('weird-rule', 'zh')).toBe('weird-rule')
  })
})
