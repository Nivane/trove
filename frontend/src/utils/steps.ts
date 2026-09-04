// Normalize backend `step` events for rendering.
//
// The backend emits structured steps as:
//   { node, seq, elapsed_ms, lang, detail: { ...node-specific... } }
// while older/legacy events used flat fields (content/sql/row_count). This
// module maps BOTH shapes into a small render contract used by StepCard.

import type { ChartSpec, StepPayload } from '../api/types'

export interface StepView {
  label: string
  sql?: string
  rowCount?: number | null
  timeMs?: number | null
  text?: string
  /** 该步骤使用的 KB 检索后端名(builtin / pg_hybrid / hybrid / rag)。 */
  backend?: string
  /** gen_sql: 情景记忆(episodes)检索通道(lexical / hybrid)。 */
  memoryBackend?: string
  /** 复杂度分级 / 快径标记 / KB 精确命中。 */
  complexity?: string
  fastPath?: boolean
  kbExact?: boolean
  /** schema_linking: 匹配与来源结构化摘要(右侧分析面板渲染)。 */
  link?: {
    tables: string[]
    terms: string[]
    notesTables: string[]
    valueHits: string[]
    fieldHits: string[]
    relations: boolean
  }
  /** route_intent: 意图判定的证据链(信号命中 + LLM 判定)。 */
  intentEvidence?: {
    signals: string[]
    llmVerdict?: string
    llmError?: string
    termHit?: boolean
    mentionedTable?: boolean
    rewritten?: boolean
    substituted?: boolean
  }
  /** query_sketch: 编译决策(compiled/miss)与计划校验。 */
  compile?: {
    outcome?: string
    missReason?: string
    missComponent?: string
  }
  planValidation?: {
    status?: string
    errors?: string[]
  }
  /** gen_sql: 上下文预算块占用。 */
  contextUsage?: { block?: string; tokens?: number }[]
  /** select: 候选投票归因 + 置信度。 */
  selection?: {
    votes?: Record<string, number>
    adopted?: boolean
    winner?: string
    degraded?: string
    confidence?: number
  }
  confidence?: number
  /** validate: 确定性规则链全过 / 断言命中。 */
  rulesPassed?: boolean
  validationHits?: { rule?: string; reason?: string }[]
  /** reflect: 预算耗尽强制通过 / 重试计数。 */
  forced?: boolean
  retryCount?: number
  semanticRetries?: number
  /** analyze_error: 修复模式 / 回归进展 / 失败版本链。 */
  fixMode?: string
  lastProgress?: string
  noProgressRounds?: number
  sqlVersions?: { round?: number; issues?: string[]; error?: string }[]
  /** refuse / clarify / answer_* / confirm_draft: 拒绝原因或直接回答。 */
  refusal?: Record<string, unknown> | null
  intentAnswer?: string
  /** chart: 图表判定结果(是否画图 / 图型 / 维度 / 度量)——不是渲染预览。 */
  chartDecision?: {
    chartable: boolean
    type?: string
    dimension?: string
    measures?: string[]
    source?: string
  }
}

function get(payload: StepPayload, key: string): unknown {
  const d = payload as Record<string, unknown> & {
    detail?: Record<string, unknown>
  }
  if (d.detail && key in d.detail) return d.detail[key]
  if (key in d) return d[key]
  return undefined
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v ? v : undefined
}

function bool(v: unknown): boolean | undefined {
  return typeof v === 'boolean' ? v : undefined
}

/** Human-readable label for a workflow node. */
export function stepLabel(node: string, lang: string): string {
  const zh: Record<string, string> = {
    route_intent: '意图路由',
    parse_date: '日期解析',
    schema_linking: '表关联',
    query_sketch: '查询计划',
    gen_sql: '生成 SQL',
    execute_sql: '执行 SQL',
    select: '结果选择',
    validate: '校验',
    reflect: '反思',
    analyze_error: '错误分析',
    output: '最终回答',
    answer_metadata: '元数据',
    metadata_check: '元数据校验',
    restore: '回滚',
    hitl: '人工确认',
    semantics: '语义',
    insights: '洞察',
    chart: '图表',
    conclusion: '结论',
    fast_match: '模板快径',
    refuse: '语义拒绝',
    clarify: '澄清',
    answer_reject: '写操作拒绝',
    answer_chitchat: '闲聊',
    answer_correction: '反馈引导',
    confirm_draft: '草稿确认',
  }
  const en: Record<string, string> = {
    route_intent: 'Route intent',
    parse_date: 'Parse date',
    schema_linking: 'Schema link',
    query_sketch: 'Plan',
    gen_sql: 'Gen SQL',
    execute_sql: 'Execute',
    select: 'Select',
    validate: 'Validate',
    reflect: 'Reflect',
    analyze_error: 'Analyze error',
    output: 'Answer',
    answer_metadata: 'Metadata',
    metadata_check: 'Metadata check',
    restore: 'Rollback',
    hitl: 'Human confirm',
    semantics: 'Semantics',
    insights: 'Insights',
    chart: 'Chart',
    conclusion: 'Conclusion',
    fast_match: 'Template fast path',
    refuse: 'Semantic refusal',
    clarify: 'Clarify',
    answer_reject: 'Write rejected',
    answer_chitchat: 'Chitchat',
    answer_correction: 'Feedback',
    confirm_draft: 'Draft confirm',
  }
  return lang === 'zh' ? (zh[node] ?? node) : (en[node] ?? node)
}

/** Map a step payload into its render view (SQL, rows/time, prose text). */
export function extractStep(payload: StepPayload): StepView {
  const node = (payload as { node?: string }).node ?? ''
  const view: StepView = {
    label: '',
    rowCount: null,
    timeMs: null,
  }

  if (node === 'route_intent') {
    const intent = str(get(payload, 'intent'))
    if (intent) view.text = intent
    const ev = get(payload, 'intent_evidence') as
      | Record<string, unknown>
      | undefined
    if (ev && typeof ev === 'object') {
      const signals: string[] = []
      for (const key of [
        'strong_match',
        'data_signal',
        'write_signal',
        'chitchat_signal',
        'correction_signal',
        'confirm_signal',
        'followup_signal',
        'weak_signal',
        'history_present',
      ]) {
        if (ev[key]) signals.push(key.replace(/_signal$/, '').replace(/_/g, ' '))
      }
      view.intentEvidence = {
        signals,
        llmVerdict: str(ev.llm_verdict),
        llmError: str(ev.llm_error),
        termHit: bool(ev.term_hit),
        mentionedTable: bool(ev.mentioned_table),
        rewritten: bool(ev.rewritten),
        substituted: bool(ev.substituted),
      }
    }
    return view
  }

  if (node === 'parse_date') {
    const tc = str(get(payload, 'time_context'))
    if (tc) view.text = tc
    return view
  }

  if (node === 'schema_linking') {
    view.backend = str(get(payload, 'retrieval_backend'))
    const tables = get(payload, 'matched_tables')
    if (Array.isArray(tables) && tables.length) {
      view.text = tables.join(', ')
    }
    const ld = get(payload, 'link_detail') as
      | Record<string, unknown>
      | undefined
    const terms = get(payload, 'kb_terms')
    if (ld && typeof ld === 'object') {
      view.label = 'match'
      view.link = {
        tables: Array.isArray(tables) ? (tables as string[]) : [],
        terms: Array.isArray(terms) ? (terms as string[]) : [],
        notesTables: Array.isArray(ld.notes_tables)
          ? (ld.notes_tables as string[])
          : [],
        valueHits: Array.isArray(ld.value_hits)
          ? (ld.value_hits as string[])
          : [],
        fieldHits: Array.isArray(ld.field_hits)
          ? (ld.field_hits as string[])
          : [],
        relations: Boolean(ld.relations),
      }
      // 上下文片段(执行日志:模型实际看到的匹配信息来源)
      const ctx = typeof ld.context === 'string' ? ld.context : ''
      if (ctx) view.text = ctx
    }
    return view
  }

  if (node === 'query_sketch') {
    const plan = str(get(payload, 'plan'))
    if (plan) view.text = plan
    const cm = get(payload, 'compile_meta') as
      | Record<string, unknown>
      | undefined
    if (cm && typeof cm === 'object') {
      view.compile = {
        outcome: str(cm.outcome),
        missReason: str(cm.miss_reason),
        missComponent: str(cm.miss_component),
      }
    }
    const pv = get(payload, 'plan_validation') as
      | Record<string, unknown>
      | undefined
    if (pv && typeof pv === 'object') {
      view.planValidation = {
        status: str(pv.status),
        errors: Array.isArray(pv.errors) ? (pv.errors as string[]) : [],
      }
    }
    return view
  }

  if (node === 'fast_match') {
    const sql = str(get(payload, 'sql'))
    if (sql) view.sql = sql
    view.fastPath = bool(get(payload, 'fast_path'))
    view.complexity = str(get(payload, 'complexity'))
    return view
  }

  if (node === 'gen_sql') {
    const sql = str(get(payload, 'sql'))
    view.label = 'SQL'
    if (sql) view.sql = sql
    view.backend = str(get(payload, 'retrieval_backend'))
    view.memoryBackend = str(get(payload, 'memory_backend'))
    view.complexity = str(get(payload, 'complexity'))
    const attempts = Number(get(payload, 'attempts') ?? 1)
    const reason = str(get(payload, 'reason'))
    const extras: string[] = []
    if (attempts > 1) extras.push(`${attempts} attempts`)
    if (reason) extras.push(reason)
    if (extras.length) view.text = extras.join(' · ')
    const cu = get(payload, 'context_usage')
    if (Array.isArray(cu)) {
      view.contextUsage = (cu as { block?: string; tokens?: number }[]).map(
        (c) => ({ block: str(c.block), tokens: c.tokens }),
      )
    }
    return view
  }

  if (node === 'execute_sql') {
    view.label = 'result'
    const rc = get(payload, 'row_count')
    view.rowCount = typeof rc === 'number' ? rc : null
    const ms = get(payload, 'execution_time_ms')
    view.timeMs = typeof ms === 'number' ? ms : null
    const reason = str(get(payload, 'reason'))
    if (reason) view.text = reason
    return view
  }

  if (node === 'select') {
    const consensus = get(payload, 'consensus')
    if (consensus === false) view.text = 'disagreed'
    const sel = get(payload, 'selection') as
      | Record<string, unknown>
      | undefined
    if (sel && typeof sel === 'object') {
      const votes = sel.votes as Record<string, number> | undefined
      view.selection = {
        votes,
        adopted: bool(sel.adopted),
        winner: str(sel.winner),
        degraded: str(sel.degraded),
        confidence: typeof sel.confidence === 'number' ? sel.confidence : undefined,
      }
    }
    const conf = get(payload, 'confidence')
    if (typeof conf === 'number') view.confidence = conf
    return view
  }

  if (node === 'validate') {
    view.rulesPassed = bool(get(payload, 'rules_passed'))
    const hits = get(payload, 'validation_hits')
    if (Array.isArray(hits)) {
      view.validationHits = (hits as { rule?: string; name?: string; reason?: string }[]).map(
        (h) => ({ rule: str(h.rule) ?? str(h.name), reason: str(h.reason) }),
      )
    }
    const reason = str(get(payload, 'reason'))
    if (reason) view.text = reason
    return view
  }

  if (node === 'reflect') {
    const verdict = str(get(payload, 'verdict'))
    const reason = str(get(payload, 'reason'))
    const parts: string[] = []
    if (verdict) parts.push(verdict)
    if (reason) parts.push(reason)
    view.text = parts.join(' — ')
    view.forced = bool(get(payload, 'forced'))
    const rc = get(payload, 'retry_count')
    view.retryCount = typeof rc === 'number' ? rc : undefined
    const sr = get(payload, 'semantic_retries')
    view.semanticRetries = typeof sr === 'number' ? sr : undefined
    return view
  }

  if (node === 'analyze_error') {
    const reason = str(get(payload, 'reason')) ?? str(get(payload, 'error'))
    const analysis = str(get(payload, 'analysis'))
    if (analysis) view.text = analysis
    else if (reason) view.text = reason
    view.fixMode = str(get(payload, 'fix_mode'))
    view.lastProgress = str(get(payload, 'last_progress'))
    const npr = get(payload, 'no_progress_rounds')
    view.noProgressRounds = typeof npr === 'number' ? npr : undefined
    const versions = get(payload, 'sql_versions')
    if (Array.isArray(versions)) {
      view.sqlVersions = (versions as {
        round?: number
        issues?: string[]
        error?: string
      }[]).map((v) => ({
        round: v.round,
        issues: Array.isArray(v.issues) ? (v.issues as string[]) : [],
        error: v.error,
      }))
    }
    return view
  }

  if (node === 'refuse') {
    const r = get(payload, 'refusal') as Record<string, unknown> | undefined
    view.refusal = r ?? null
    const msg = str(get(payload, 'clarification_question'))
    if (msg) view.text = msg
    return view
  }

  if (node === 'clarify') {
    const msg = str(get(payload, 'clarification_question'))
    if (msg) view.text = msg
    return view
  }

  if (
    node === 'answer_reject' ||
    node === 'answer_chitchat' ||
    node === 'answer_correction' ||
    node === 'answer_metadata' ||
    node === 'confirm_draft'
  ) {
    view.intentAnswer = str(get(payload, 'intent_answer'))
    return view
  }

  if (node === 'restore') {
    const rb = str(get(payload, 'rollback'))
    if (rb) view.text = rb
    return view
  }

  if (node === 'chart') {
    const chart = get(payload, 'chart') as ChartSpec | null | undefined
    const source = str(get(payload, 'chart_source'))
    if (chart && typeof chart === 'object' && chart.type) {
      view.chartDecision = {
        chartable: true,
        type: chart.type,
        dimension: chart.dimension,
        measures: Array.isArray(chart.measures) ? (chart.measures as string[]) : [],
        source,
      }
    } else {
      view.chartDecision = { chartable: false, source }
    }
    return view
  }

  // Generic nodes with free-form detail.
  for (const key of [
    'content',
    'verdict',
    'reason',
    'semantics',
    'hitl_status',
  ]) {
    const v = str(get(payload, key))
    if (v) {
      view.text = v
      break
    }
  }

  return view
}

/** Human-readable label for the KB retrieval backend used by a step. */
export function backendLabel(backend: string, lang: string): string {
  if (!backend) return ''
  const zh: Record<string, string> = {
    builtin: '内置 FTS5 (SQLite)',
    pg_hybrid: 'PG 混合检索 (FTS+向量+RRF)',
    hybrid: '混合检索',
    rag: '向量检索',
  }
  const en: Record<string, string> = {
    builtin: 'builtin FTS5 (SQLite)',
    pg_hybrid: 'PG hybrid (FTS+vector+RRF)',
    hybrid: 'hybrid',
    rag: 'vector',
  }
  return lang === 'zh' ? (zh[backend] ?? backend) : (en[backend] ?? backend)
}

/** i18n key for the episodic-memory channel label (lexical / hybrid). */
export function memoryLabelKey(
  backend: string,
): 'memoryHybrid' | 'memoryLexical' {
  return backend === 'hybrid' ? 'memoryHybrid' : 'memoryLexical'
}

/** Display duration in a compact form. */
export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}
