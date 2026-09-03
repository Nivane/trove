// Normalize backend `step` events for rendering.
//
// The backend emits structured steps as:
//   { node, seq, elapsed_ms, lang, detail: { ...node-specific... } }
// while older/legacy events used flat fields (content/sql/row_count). This
// module maps BOTH shapes into a small render contract used by StepCard.

import type { StepPayload } from '../api/types'

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
  /** schema_linking: 匹配与来源结构化摘要(右侧分析面板渲染)。 */
  link?: {
    tables: string[]
    terms: string[]
    notesTables: string[]
    valueHits: string[]
    fieldHits: string[]
    relations: boolean
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

  if (node === 'gen_sql') {
    const sql = str(get(payload, 'sql'))
    view.label = 'SQL'
    if (sql) view.sql = sql
    view.backend = str(get(payload, 'retrieval_backend'))
    view.memoryBackend = str(get(payload, 'memory_backend'))
    const attempts = Number(get(payload, 'attempts') ?? 1)
    const reason = str(get(payload, 'reason'))
    const extras: string[] = []
    if (attempts > 1) extras.push(`${attempts} attempts`)
    if (reason) extras.push(reason)
    if (extras.length) view.text = extras.join(' · ')
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
    return view
  }

  if (node === 'route_intent') {
    const intent = str(get(payload, 'intent'))
    if (intent) view.text = intent
    return view
  }

  if (node === 'analyze_error') {
    const reason = str(get(payload, 'reason')) ?? str(get(payload, 'error'))
    const analysis = str(get(payload, 'analysis'))
    if (analysis) view.text = analysis
    else if (reason) view.text = reason
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
