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
  stageOf,
  stageLabel,
  verifyStages,
  isDataRound,
  correctionRounds,
  groupOf,
  groupSteps,
  groupElapsedMs,
  barWidthPx,
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
    // 走数链全节点都得有中文名（裸英文名是漏映射）
    expect(stepLabel('masking', 'zh')).toBe('脱敏')
    expect(stepLabel('attribution', 'zh')).toBe('归因分析')
    expect(stepLabel('attribution', 'en')).toBe('Attribution')
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

/* ── 验证条 / 工段分组 / 计时条（答案卡视觉升级）────────────── */

const step = (
  node: string,
  ms?: number,
  extra: Record<string, unknown> = {},
): { node: string; payload: Record<string, unknown> } => ({
  node,
  payload: { node, ...(ms != null ? { elapsed_ms: ms } : {}), ...extra },
})

describe('验证条（六段骨架）', () => {
  it('stageOf：认识的节点映射到六段，不认识（hitl / answer_*）归 null', () => {
    expect(stageOf('route_intent')).toBe('route')
    expect(stageOf('parse_date')).toBe('route')
    expect(stageOf('schema_linking')).toBe('link')
    expect(stageOf('query_sketch')).toBe('plan')
    expect(stageOf('gen_generate')).toBe('gen')
    expect(stageOf('semantics')).toBe('gen')
    expect(stageOf('execute_sql')).toBe('exec')
    expect(stageOf('reflect')).toBe('verify')
    expect(stageOf('hitl')).toBeNull()
    expect(stageOf('answer_chitchat')).toBeNull()
  })

  it('stageLabel 中英双写', () => {
    expect(stageLabel('verify', 'zh')).toBe('校验')
    expect(stageLabel('verify', 'en')).toBe('Verify')
  })

  it('verifyStages 取集合；isDataRound 只认走数链（生成/执行/校验亮过）', () => {
    const lit = verifyStages([
      step('route_intent'),
      step('gen_sql'),
      step('validate'),
    ])
    expect([...lit].sort()).toEqual(['gen', 'route', 'verify'])
    expect(isDataRound([step('route_intent'), step('gen_sql')])).toBe(true)
    // 元数据 / 拒绝 / 闲聊轮不套这条骨架
    expect(isDataRound([step('answer_metadata'), step('metadata_check')])).toBe(false)
    expect(isDataRound([step('refuse')])).toBe(false)
    expect(isDataRound([])).toBe(false)
  })

  it('correctionRounds：取反思步骤 retry_count 的最大值；没跑过反思 → null', () => {
    expect(correctionRounds([step('gen_sql', 10)])).toBeNull()
    expect(correctionRounds([step('reflect', 5, { retry_count: 0 })])).toBe(0)
    expect(
      correctionRounds([
        step('reflect', 5, { retry_count: 0 }),
        step('reflect', 5, { retry_count: 1 }),
      ]),
    ).toBe(1)
  })
})

describe('工段分组（分析面板）', () => {
  it('连续同段并组；回退重来再开一个「生成」组 —— 时序不丢', () => {
    const steps = [
      step('route_intent'),
      step('schema_linking'),
      step('gen_sql'),
      step('execute_sql'),
      step('validate'),
      step('reflect'),
      // 反思判定重来 → 第二轮生成 / 执行
      step('gen_sql'),
      step('execute_sql'),
      step('output'),
    ]
    const groups = groupSteps(steps, 'zh')
    expect(groups.map((g) => g.group)).toEqual([
      'understand',
      'generate',
      'verify',
      'generate',
      'verify',
      'deliver',
    ])
    // 组内保留全局下标（attempt 计数与计时条取值都靠它）
    expect(groups[2].items.map((it) => it.index)).toEqual([3, 4, 5])
    expect(groups[0].label).toBe('理解')
    expect(groups[0].items.map((it) => it.index)).toEqual([0, 1])
  })

  it('组名中英双写；未映射节点归 other（不硬塞进四段）', () => {
    expect(groupOf('answer_chitchat')).toBe('other')
    expect(groupOf('hitl')).toBe('verify')
    // 归因是交付段（时序在反思与洞察之间），不进「其他」
    expect(groupOf('attribution')).toBe('deliver')
    expect(groupSteps([step('output')], 'en')[0].label).toBe('Deliver')
  })

  it('groupElapsedMs：只计带 elapsed_ms 的步骤；一个都没有 → null（组头不写）', () => {
    expect(groupElapsedMs([step('gen_sql', 1200), step('validate', 300)])).toBe(1500)
    expect(groupElapsedMs([step('gen_sql'), step('validate')])).toBeNull()
    expect(groupElapsedMs([])).toBeNull()
  })
})

describe('计时条（barWidthPx）', () => {
  it('长 ∝ 耗时：最长占满槽，最短 3px，超过槽被夹住', () => {
    expect(barWidthPx(1000, 1000, 44)).toBe(44)
    expect(barWidthPx(500, 1000, 44)).toBe(22)
    expect(barWidthPx(1, 1000, 44)).toBe(3)
    expect(barWidthPx(5000, 1000, 44)).toBe(44)
  })

  it('没有可依据的刻度 / 耗时 → null（不画条）', () => {
    expect(barWidthPx(undefined, 1000)).toBeNull()
    expect(barWidthPx('120' as unknown, 1000)).toBeNull()
    expect(barWidthPx(100, 0)).toBeNull()
    expect(barWidthPx(-5, 1000)).toBeNull()
  })
})
