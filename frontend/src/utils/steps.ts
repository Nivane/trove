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
  /** query_sketch: 编译决策(compiled/partial/miss)与计划校验。 */
  compile?: {
    outcome?: string
    missReason?: string
    missComponent?: string
    partialReasons?: string[]
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
    masking: '脱敏',
    reflect: '反思',
    attribution: '归因分析',
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
    masking: 'Masking',
    reflect: 'Reflect',
    attribution: 'Attribution',
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
        // 原始信号键交给 label 层翻译(见 signalLabel) —— 这里不做文案。
        if (ev[key]) signals.push(key)
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
        partialReasons: Array.isArray(cm.partial_reasons)
          ? (cm.partial_reasons as string[])
          : undefined,
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
function pick(
  lang: string,
  zh: Record<string, string>,
  en: Record<string, string>,
  value: string,
): string {
  return (lang === 'zh' ? zh[value] : en[value]) ?? value
}

/** 检索方式用业务词说;底层实现(FTS5/RRF)留在 title 里给需要的人。 */
export function backendLabel(backend: string, lang: string): string {
  if (!backend) return ''
  return pick(lang, _BACKEND, _BACKEND_EN, backend)
}

/** 检索后端的技术名 —— 悬停提示,不占面板正文。 */
export function backendDetail(backend: string, lang: string): string {
  if (!backend) return ''
  return pick(lang, _BACKEND_DETAIL, _BACKEND_DETAIL_EN, backend)
}

const _BACKEND: Record<string, string> = {
  builtin: '关键词检索',
  pg_hybrid: '混合检索',
  hybrid: '混合检索',
  rag: '向量检索',
}
const _BACKEND_EN: Record<string, string> = {
  builtin: 'keyword search',
  pg_hybrid: 'hybrid search',
  hybrid: 'hybrid search',
  rag: 'vector search',
}
const _BACKEND_DETAIL: Record<string, string> = {
  builtin: '内置 FTS5 (SQLite) 全文检索',
  pg_hybrid: 'PG 混合检索 (FTS + 向量 + RRF)',
  hybrid: '关键词 + 向量混合检索',
  rag: '向量检索',
}
const _BACKEND_DETAIL_EN: Record<string, string> = {
  builtin: 'built-in FTS5 (SQLite) full-text search',
  pg_hybrid: 'PG hybrid (FTS + vector + RRF)',
  hybrid: 'keyword + vector hybrid',
  rag: 'vector search',
}

/** route_intent 证据信号:后端键名 → 人话。 */
const _SIGNAL: Record<string, string> = {
  strong_match: '强匹配',
  weak_signal: '弱匹配',
  data_signal: '数据问句',
  write_signal: '写操作',
  chitchat_signal: '闲聊',
  correction_signal: '纠错反馈',
  confirm_signal: '确认草稿',
  followup_signal: '追问',
  history_present: '有历史上下文',
}
const _SIGNAL_EN: Record<string, string> = {
  strong_match: 'strong match',
  weak_signal: 'weak match',
  data_signal: 'data question',
  write_signal: 'write op',
  chitchat_signal: 'chitchat',
  correction_signal: 'correction',
  confirm_signal: 'confirm draft',
  followup_signal: 'follow-up',
  history_present: 'history',
}

/** 信号标签;后端新增而前端未收录的信号退化成可读词,不露下划线键名。 */
export function signalLabel(signal: string, lang: string): string {
  if (!signal) return ''
  const known = lang === 'zh' ? _SIGNAL[signal] : _SIGNAL_EN[signal]
  if (known) return known
  return signal.replace(/_signal$/, '').replace(/_/g, ' ')
}

/** 复杂度分级(simple / standard / complex)。 */
export function complexityLabel(value: string, lang: string): string {
  return pick(
    lang,
    { simple: '简单', standard: '常规', complex: '复杂' },
    { simple: 'simple', standard: 'standard', complex: 'complex' },
    value,
  )
}

/** 语义编译结果(compiled / partial / miss)。 */
export function compileOutcomeLabel(value: string, lang: string): string {
  return pick(
    lang,
    { compiled: '已编译', partial: '部分编译', miss: '未编译' },
    { compiled: 'compiled', partial: 'partially compiled', miss: 'not compiled' },
    value,
  )
}

/** 计划校验状态(ok / dropped)。 */
export function planStatusLabel(value: string, lang: string): string {
  return pick(
    lang,
    { ok: '通过', dropped: '已丢弃' },
    { ok: 'passed', dropped: 'dropped' },
    value,
  )
}

/** 修复模式:fixer 定点修 / revisor 语义重写。 */
export function fixModeLabel(value: string, lang: string): string {
  return pick(
    lang,
    { fixer: '定点修复', revisor: '语义重写', none: '无需修复' },
    { fixer: 'targeted fix', revisor: 'semantic rewrite', none: 'no fix needed' },
    value,
  )
}

/** 回归进展(versions.py 的回归状态机)。 */
export function progressLabel(value: string, lang: string): string {
  return pick(
    lang,
    {
      first: '首次失败',
      invalid: '原地打转',
      none: '无进展',
      shift: '问题转移',
      improved: '有进展',
      'validator-conflict': '校验误报复现',
    },
    {
      first: 'first failure',
      invalid: 'same error again',
      none: 'no progress',
      shift: 'problem shifted',
      improved: 'progress',
      'validator-conflict': 'validator false alarm',
    },
    value,
  )
}

/** 编译未覆盖分因(compiler 的 CompileMiss.reason)。 */
const _MISS: Record<string, string> = {
  no_metric_match: '缺少指标声明',
  metric_anchor_unmatched: '指标口径对不上',
  unresolved_answer_column: '输出字段未声明',
  unresolved_filter_field: '筛选字段未声明',
  enum_value_unresolved: '筛选值没有对应字段',
  missing_filter_value: '筛选值没有对应字段',
  invalid_op: '比较口径未声明',
  having_metric_unknown: '筛选指标未声明',
  having_without_aggregation: '缺少分组口径',
  unknown_cardinality: '表关系基数未声明',
  fan_out: '联表会重复计数',
  unreachable_table: '表与模型不连通',
  table_not_allowed: '表超出数据源授权范围',
  ambiguous_join_path: '联表路径不唯一',
  derived_cycle: '派生指标循环定义',
  derived_depth: '派生指标嵌套过深',
  derived_unresolved: '派生指标表达式缺失',
  no_plan_or_matched: '没匹配到数据表',
  nothing_compilable: '没有可编译的表与字段',
  limit_without_order: '缺少排序口径',
  guardrail_rejected: '计算方式被护栏拦下',
  no_semantic_match: '概念未建模',
  uncovered: '概念未建模',
}
const _MISS_EN: Record<string, string> = {
  no_metric_match: 'no metric declared',
  metric_anchor_unmatched: 'metric anchor unmatched',
  unresolved_answer_column: 'output field undeclared',
  unresolved_filter_field: 'filter field undeclared',
  enum_value_unresolved: 'no field for filter value',
  missing_filter_value: 'no field for filter value',
  invalid_op: 'comparison not declared',
  having_metric_unknown: 'filter metric undeclared',
  having_without_aggregation: 'no grouping declared',
  unknown_cardinality: 'join cardinality undeclared',
  fan_out: 'join double-counts',
  unreachable_table: 'table not linked to the model',
  table_not_allowed: 'table outside the authorized scope',
  ambiguous_join_path: 'ambiguous join path',
  derived_cycle: 'circular derived metric',
  derived_depth: 'derived metric too deep',
  derived_unresolved: 'derived metric expression missing',
  no_plan_or_matched: 'no table matched',
  nothing_compilable: 'nothing to compile',
  limit_without_order: 'no ordering declared',
  guardrail_rejected: 'computation blocked by guardrail',
  no_semantic_match: 'concept not modeled',
  uncovered: 'concept not modeled',
}

/** 未覆盖分因;编译器新加的 slug 原样露出(管理端要拿去对日志),不编造。 */
export function missReasonLabel(reason: string, lang: string): string {
  return pick(lang, _MISS, _MISS_EN, reason)
}

/** 上下文预算块(context_budget.py 的块名)。 */
export function blockLabel(block: string, lang: string): string {
  const known =
    lang === 'zh' ? _BLOCK[block] : _BLOCK_EN[block]
  return known ?? block.replace(/_/g, ' ')
}
const _BLOCK: Record<string, string> = {
  few_shots: '示例',
  rules: '规则',
  term_notes: '术语备注',
  metrics: '指标',
  entities: '实体',
  lessons: '经验',
  episodes: '历史问答',
  plan: '计划',
  history: '对话历史',
  user_facts: '用户偏好',
  profile: '准确率画像',
}
const _BLOCK_EN: Record<string, string> = {
  few_shots: 'examples',
  rules: 'rules',
  term_notes: 'term notes',
  metrics: 'metrics',
  entities: 'entities',
  lessons: 'lessons',
  episodes: 'episodes',
  plan: 'plan',
  history: 'history',
  user_facts: 'user facts',
  profile: 'profile',
}

/** 校验规则号 → 规则族(F1 形状 / F2 过滤 / F3 取值 / F4 排序)。 */
export function ruleLabel(rule: string, lang: string): string {
  // 规则号有 `F1-b` 与 `F1_shape` 两种写法(历史遗留),都按族取前缀
  const family = rule.split(/[-_]/)[0]
  const known =
    lang === 'zh' ? _RULE_FAMILY[family] : _RULE_FAMILY_EN[family]
  return known ?? rule
}
const _RULE_FAMILY: Record<string, string> = {
  F1: '形状',
  F2: '过滤条件',
  F3: '取值',
  F4: '排序',
  count: '计数形状',
  answer: '输出列',
  extra: '多余列',
}
const _RULE_FAMILY_EN: Record<string, string> = {
  F1: 'shape',
  F2: 'filters',
  F3: 'values',
  F4: 'ordering',
  count: 'count shape',
  answer: 'answer columns',
  extra: 'extra columns',
}

/** i18n key for the episodic-memory channel label (lexical / hybrid). */
export function memoryLabelKey(
  backend: string,
): 'memoryHybrid' | 'memoryLexical' {
  return backend === 'hybrid' ? 'memoryHybrid' : 'memoryLexical'
}

/** Display a context-budget token count compactly. */
export function fmtTokens(tokens: number | null | undefined): string {
  if (tokens === null || tokens === undefined) return ''
  if (tokens < 1000) return `${tokens} tokens`
  return `${(tokens / 1000).toFixed(1)}k tokens`
}

/** Display duration in a compact form. */
export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

/* ── 验证条 / 分析面板分组（2026-10 答案卡视觉升级）──────────────
   答案卡的 6 段验证条（骨架走到哪一段）与分析面板的 4 组工序共用一张
   节点→阶段映射。两者都是纯映射：不认识的节点归 null / 'other' ——
   绝不硬塞进某一段（宁少亮一段，不虚报工序走过）。
   阶段名/组名沿用 stepLabel 的做法在本地写死 zh/en（管线术语，不进
   产品文案 i18n 表）。 */

export type VerifyStage = 'route' | 'link' | 'plan' | 'gen' | 'exec' | 'verify'

/** 验证条六段的固定顺序。 */
export const VERIFY_STAGE_ORDER: readonly VerifyStage[] = [
  'route',
  'link',
  'plan',
  'gen',
  'exec',
  'verify',
]

const _STAGE_OF_NODE: Record<string, VerifyStage> = {
  route_intent: 'route',
  parse_date: 'route',
  schema_linking: 'link',
  query_sketch: 'plan',
  fast_match: 'gen',
  gen_sql: 'gen',
  gen_retrieve: 'gen',
  gen_assemble: 'gen',
  gen_generate: 'gen',
  select: 'gen',
  semantics: 'gen',
  execute_sql: 'exec',
  validate: 'verify',
  masking: 'verify',
  reflect: 'verify',
  analyze_error: 'verify',
  restore: 'verify',
}

export function stageOf(node: string): VerifyStage | null {
  return _STAGE_OF_NODE[node] ?? null
}

const _STAGE_LABEL: Record<VerifyStage, string> = {
  route: '路由',
  link: '关联',
  plan: '计划',
  gen: '生成',
  exec: '执行',
  verify: '校验',
}
const _STAGE_LABEL_EN: Record<VerifyStage, string> = {
  route: 'Route',
  link: 'Link',
  plan: 'Plan',
  gen: 'Generate',
  exec: 'Execute',
  verify: 'Verify',
}

export function stageLabel(stage: VerifyStage, lang: string): string {
  return (lang === 'zh' ? _STAGE_LABEL : _STAGE_LABEL_EN)[stage]
}

/** 这轮步骤走到了哪些阶段（按步骤序列取集合）。 */
export function verifyStages(steps: { node: string }[]): Set<VerifyStage> {
  const out = new Set<VerifyStage>()
  for (const s of steps) {
    const st = stageOf(s.node)
    if (st) out.add(st)
  }
  return out
}

/** 验证条只在真正走数的一轮出现：生成/执行/校验至少亮一段
 *  （元数据、拒绝、闲聊等轮不套这条骨架）。 */
export function isDataRound(steps: { node: string }[]): boolean {
  const lit = verifyStages(steps)
  return lit.has('gen') || lit.has('exec') || lit.has('verify')
}

/** 修正轮数 = 各反思步骤里 retry_count 的最大值（生成回合被回退重来的
 *  次数，工作流自己的口径）。没有反思步骤 → null（印章不写这句）。 */
export function correctionRounds(
  steps: { node: string; payload?: Record<string, unknown> }[],
): number | null {
  let seen = false
  let max = 0
  for (const s of steps) {
    if (s.node !== 'reflect') continue
    seen = true
    const rc = s.payload?.retry_count
    if (typeof rc === 'number' && rc > max) max = rc
  }
  return seen ? max : null
}

export type StepGroup = 'understand' | 'generate' | 'verify' | 'deliver' | 'other'

export const STEP_GROUP_ORDER: readonly StepGroup[] = [
  'understand',
  'generate',
  'verify',
  'deliver',
  'other',
]

const _GROUP_OF_NODE: Record<string, StepGroup> = {
  route_intent: 'understand',
  parse_date: 'understand',
  schema_linking: 'understand',
  query_sketch: 'understand',
  fast_match: 'generate',
  gen_sql: 'generate',
  gen_retrieve: 'generate',
  gen_assemble: 'generate',
  gen_generate: 'generate',
  select: 'generate',
  semantics: 'generate',
  // 人工确认是执行前的闸门，归执行段（它卡在 semantics 与 execute 之间，
  // 组内保持原顺序即可还原真实时序）。
  hitl: 'verify',
  execute_sql: 'verify',
  validate: 'verify',
  masking: 'verify',
  reflect: 'verify',
  analyze_error: 'verify',
  restore: 'verify',
  attribution: 'deliver',
  insights: 'deliver',
  chart: 'deliver',
  conclusion: 'deliver',
  output: 'deliver',
  answer_metadata: 'deliver',
  metadata_check: 'deliver',
}

export function groupOf(node: string): StepGroup {
  return _GROUP_OF_NODE[node] ?? 'other'
}

const _GROUP_LABEL: Record<StepGroup, string> = {
  understand: '理解',
  generate: '生成',
  verify: '执行与验证',
  deliver: '交付',
  other: '其他',
}
const _GROUP_LABEL_EN: Record<StepGroup, string> = {
  understand: 'Understand',
  generate: 'Generate',
  verify: 'Execute & verify',
  deliver: 'Deliver',
  other: 'Other',
}

export function stepGroupLabel(group: StepGroup, lang: string): string {
  return (lang === 'zh' ? _GROUP_LABEL : _GROUP_LABEL_EN)[group]
}

export interface StepGroupBucket<T> {
  group: StepGroup
  label: string
  items: { step: T; index: number }[]
}

/** 步骤按工段分组：**连续同段**并入同组，时序不丢 —— 回退重来会再开
 *  一个「生成」组（「生成→执行与验证→生成→…」正是重试环本身的形状，
 *  按段全量归桶会把重试的交错抹平）。'other' 兜底段照常参与。 */
export function groupSteps<T extends { node: string }>(
  steps: T[],
  lang: string,
): StepGroupBucket<T>[] {
  const out: StepGroupBucket<T>[] = []
  steps.forEach((step, index) => {
    const g = groupOf(step.node)
    const last = out[out.length - 1]
    if (last && last.group === g) last.items.push({ step, index })
    else out.push({ group: g, label: stepGroupLabel(g, lang), items: [{ step, index }] })
  })
  return out
}

/** 组内耗时合计（只计带 elapsed_ms 的步骤；一个都没有 → null，组头不写）。 */
export function groupElapsedMs(
  steps: { payload?: Record<string, unknown> }[],
): number | null {
  let total = 0
  let seen = false
  for (const s of steps) {
    const ms = s.payload?.elapsed_ms
    if (typeof ms === 'number' && ms >= 0) {
      total += ms
      seen = true
    }
  }
  return seen ? total : null
}

/** 计时条宽（px）：与耗时成正比，最短 3px（极短步骤也看得见）。
 *  没有可依据的刻度（maxMs 为 0/缺失）→ null，不画。 */
export function barWidthPx(
  ms: unknown,
  maxMs: number,
  slotPx = 44,
): number | null {
  if (typeof ms !== 'number' || ms < 0) return null
  if (!(maxMs > 0)) return null
  const w = Math.round((ms / maxMs) * slotPx)
  return Math.max(3, Math.min(slotPx, w))
}
