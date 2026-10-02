// Shared wire types for the SSE event stream and API payloads.

export interface ChartSpec {
  type: string
  title?: string
  dimension?: string
  categories?: string[]
  series?: { name?: string; data?: (number | string | null)[] }[]
  measures?: string[]
}

export interface ErrorInfo {
  /** 失败归类:gave_up / too_complex / permission / datasource / model /
   *  query / mismatch / unclear / unknown —— 决定卡片文案与是否可重试。 */
  kind?: string
  title?: string
  explanation?: string
  suggestion?: string
  retryable?: boolean
  detail?: {
    /** 原始错误文本(内部措辞),仅管理员折叠区展示。 */
    raw?: string
    node?: string
    error_class?: string
    domain?: string
    [k: string]: unknown
  }
}

import type { MaskingReport } from '../utils/masking'

export interface DoneSummary {
  session_id?: string
  run_id?: string
  question?: string
  /** 意图层改写痕迹:省略式追问/纯反馈替换上一问时的原始问题。 */
  rewritten_question?: string
  /** 回答所用数据源名。 */
  datasource?: string
  sql?: string
  row_count?: number
  verdict?: string
  reason?: string
  error?: string
  /** 错误呈现层产物:用户可见的标题/解释/建议 + 机器细节。前端渲染错误卡片,
   *  不解析错误 markdown、不重猜类别。 */
  error_info?: ErrorInfo
  final_response?: string
  columns?: string[]
  /** 完整查询结果(受后端 result_max_rows 约束;下载用,不放回答案表格)。 */
  rows?: unknown[][]
  chart?: ChartSpec | null
  chart_option?: Record<string, unknown> | null
  insights?: unknown[]
  hitl_status?: string
  batched?: boolean
  total_elapsed_ms?: number
  /** 缓存键真报才在(缺席 = 该次运行未测量缓存,不要当 0 命中渲染)。 */
  token_usage?: {
    prompt?: number
    completion?: number
    total?: number
    cache_read_input_tokens?: number
    cache_creation_input_tokens?: number
    cached_tokens?: number
  }
  cached?: boolean
  /** 字段级脱敏报告(设计 §6.2):`{fields: {字段名: 模式}, bypass}`。
   *  `null`/缺省 = 脱敏节点没跑(该部署没配);`bypass` = 本次以原文返回。 */
  masking_applied?: MaskingReport | null
  // ── 答案可信层(P1):后端一直在发、前端此前没接的字段 ──
  /** 来源档位:certified / reused / compiled / generated;"" = 没有可披露
   *  的答案(元数据/反问/错误路径)—— 空串与缺席都不是第五档。 */
  answer_source?: string
  /** SQL 置信度;0 = 没有可披露的答案,不是「置信度 0」。 */
  sql_confidence?: number
  /** 答案级置信度(≤ sql_confidence);0 同上。 */
  confidence?: number
  /** 置信度构成证据(why 已按运行语言本地化)。 */
  confidence_evidence?: ConfidenceEvidenceItem[]
  kb_hits?: KbHitItem[]
  matched_tables?: string[]
  semantics?: unknown
  conclusion?: string
  /** 结果预览行(≤ ROWS_PREVIEW,事件侧用);完整结果在 `rows`。 */
  rows_preview?: unknown[][]
  /** 读取主体(authz wire 形状):「读取身份」行的原料。 */
  principal?: PrincipalWire
  /** gen_sql 实际使用的模型;""/缺席 = 这条答案没经过生成(快径/KB 复用/
   *  无 SQL),不是「未知模型」。 */
  model?: string
  /** 执行可信度(数据截止/估算扫描/限额/降级):三态字段。 */
  execution_evidence?: ExecutionEvidence | null
}

/** 置信度证据项(`_state_summary.confidence_evidence`)。 */
export interface ConfidenceEvidenceItem {
  kind?: string // 'sql' | 'result'
  key?: string
  effect?: string
  why?: string
}

/** KB 命中项(term / example / template 三种形状共用)。 */
export interface KbHitItem {
  kind?: string // 'term' | 'example' | 'template'
  term?: string
  mapping?: string
  definition?: string
  tables?: string[]
  question?: string
  sql?: string
  tags?: string[]
  source?: string
  /** 模板治理状态(certified / draft / …);缺 = 查不到背书,不是「已认证」。 */
  status?: string
}

/** 执行可信度(预算/画像轨)。三态:`""` = 没查过,`'unknown'` = 查过但未知。 */
export interface ExecutionEvidence {
  estimated_rows?: number | null
  estimated_bytes?: number | null
  source?: string
  degraded?: boolean
  verdict?: string
  reason?: string
  limit_applied?: number | null
  scanned_rows?: number | null
  data_as_of?: string
  as_of_basis?: string
  terminated?: boolean
  kill?: string
  budget?: Record<string, unknown>
}

/** 读取主体(`principal_to_wire` 形状)。 */
export interface PrincipalWire {
  subject?: string
  role?: string
  scopes?: string[]
  /** None / [] / [...] 三态 —— 不得合并成「没有授权记录」。 */
  grants?: string[] | null
  on_behalf_of?: string
}

/** `GET /v1/runs/{run_id}` 的只读回放(节点时间线 / LLM / 工具 / 终态)。 */
export interface RunReplayStep {
  name: string
  seq?: number
  depth?: number
  elapsed_ms?: number | null
  tokens?: TokenBucket | null
  /** running = 有栈无收(崩在中途/未跑完)。 */
  status?: 'ok' | 'running'
}
export interface TokenBucket {
  prompt?: number
  completion?: number
  total?: number
}
export interface RunReplayLlmCall {
  node?: string
  model?: string
  elapsed_ms?: number | null
  tokens?: TokenBucket | null
}
export interface RunReplayTool {
  name?: string
  node?: string
}
export interface RunReplay {
  run_id: string
  /** trace = 机器轨迹(有界,可能被裁剪);session = 仅终态摘要。 */
  source: 'trace' | 'session'
  session_id?: string
  question?: string
  /** 运行开始时间(ISO);仅 trace 源可得,拿不到为 null。 */
  started_at?: string | null
  /** trace 源:是否录到终态(finish 事件);session 源恒 true。 */
  complete?: boolean
  model?: string | null
  summary?: DoneSummary
  timeline?: RunReplayStep[]
  llm_calls?: RunReplayLlmCall[]
  tools?: RunReplayTool[]
}

export interface StepPayload {
  node?: string
  label?: string
  content?: string
  sql?: string
  row_count?: number
  execution_time_ms?: number
  [k: string]: unknown
}

export interface TaskItem {
  task_id: string
  title: string
  status: 'pending' | 'in_progress' | 'done' | 'failed'
  position: number
}

export interface HitlPayload {
  payload?: {
    task_context?: { total?: number }
    [k: string]: unknown
  }
  [k: string]: unknown
}

export interface SseEvent {
  type: string
  data: Record<string, unknown>
}

export interface DatasourceInfo {
  name: string
  type: string
  default?: boolean
  status?: string
  kb_initialized?: boolean
  kb_items?: Record<string, number>
}

/** GET /v1/admin/users 列表条目(管理端用户;`datasources` 为服务端内联的授权名)。 */
export interface AdminUser {
  id: number
  username: string
  role: string
  display_name?: string
  disabled?: boolean
  created_at?: string
  updated_at?: string
  datasources?: string[]
}

// ── KB 管理页(W3-K)──────────────────────────────────────────

/** 语义条目(GET /v1/kb/entries):metric / entity / table 三类各带 kind,
 *  `key` 为镜像 item_key(统一名字列)。 */
export interface KbSemanticEntry {
  kind: 'metric' | 'entity' | 'table'
  key: string
  /** metric: 指标名;entity: 字段名;table: 表名(与 key 同源)。 */
  name?: string
  aliases?: string[]
  synonyms?: string[]
  /** metric 的表达式(可能多方言)。 */
  expression?: unknown
  datasets?: string[]
  dataset?: string
  field?: string
  role?: string
  definition?: string
  description?: string
  enum_values?: string[]
  label?: string
  datatype?: string
  columns?: { name?: string; description?: string }[]
  row_count?: number | null
  [k: string]: unknown
}

/** Hint Bank 教训(镜像 payload;时间戳三态:append 路径有 created_at、
 *  rating 路径有 updated_at,缺失为空串 —— 显示「—」不造假)。 */
export interface KbLesson {
  pattern?: string
  question?: string
  note?: string
  sql_snippet?: string
  confirmed?: boolean
  upvotes?: number
  downvotes?: number
  confidence?: number
  source?: string
  evidence?: string
  created_at?: string
  updated_at?: string
}

/** 参考示例(已确认条目带治理块;pending 草稿不带,见 KbPendingExample)。 */
export interface KbExample {
  question?: string
  sql?: string
  tags?: string[]
  template?: boolean
  aggregate?: boolean
  date_range?: boolean
  /** 治理块(governance_of 推断,不写在文件里)。 */
  status?: string
  owner?: string
  approved_by?: string
  approved_at?: string
  source?: string
}

/** 待确认示例草稿(GET /v1/kb/examples/pending;pending 不参与检索)。 */
export interface KbPendingExample {
  question?: string
  sql?: string
  tags?: string[]
  note?: string
  pending?: boolean
  /** 草稿时间戳今天不存在 —— 显示「—」(时间列的源头不齐,不做假)。 */
  created_at?: string
}

/** 每份 KB 资产的来源与格式体检(GET /v1/kb/assets 或 detail.status)。
 *  `edited` 三态:true/false/null(= 无从判断,存量文件没有摘要)。 */
export interface KbAsset {
  file: string
  format?: number | null
  generator?: string
  trove?: string
  generated_at?: string
  edited?: boolean | null
  needs_migration?: boolean
  refused?: string | null
  has_baseline?: boolean
  error?: string
}

/** GET /v1/admin/datasources/{name}/kb 的聚合体(kb_detail)。 */
export interface KbDetail {
  status: {
    initialized?: boolean
    files?: string[]
    items?: Record<string, number>
    assets?: KbAsset[]
    refused_assets?: Record<string, string>
  }
  terms: { term?: string; aliases?: string[]; mapping?: string; tables?: string[]; definition?: string }[]
  examples: KbExample[]
  rules: string[]
  lessons: KbLesson[]
}

// ── 语义工作台(P2)—— /v1/admin/semantic/{ds} 契约 ─────────────
//
// 形状与后端 pydantic response_model 一一对应(trove/api/schemas.py 的
// Semantic* 系列 + manage._model_to_dict / _draft_diff / _drift_view)。

/** 问题条目定位到的实体(前端按 kind 分流跳转)。 */
export interface SemanticIssueTarget {
  /** metric | field | dataset | relationship | document | unknown */
  kind: string
  /** metric/关系名;field 为 dataset.field;dataset 为数据集名。 */
  name: string
}

/** 结构化 lint/校验条目(validate 端点与 detail.issue_items 共用)。 */
export interface SemanticIssueItem {
  severity: 'error' | 'warning'
  /** 稳定契约:前端按 code 分流 UI,不随措辞漂移。 */
  code: string
  target: SemanticIssueTarget
  message: string
  hint?: string
}

export interface SemanticFieldInfo {
  name: string
  expression?: string
  datatype?: string
  is_time?: boolean
  description?: string
  synonyms?: string[]
  semantic_role?: string
  enum_display?: Record<string, string>
  value_aliases?: Record<string, string[]>
  label?: string
  examples?: string[]
  custom_extensions?: unknown[]
  mask?: string
}

export interface SemanticDatasetInfo {
  name: string
  source?: string
  primary_key?: string[]
  unique_keys?: string[][]
  row_filter?: string
  description?: string
  synonyms?: string[]
  fields?: SemanticFieldInfo[]
  examples?: string[]
  custom_extensions?: unknown[]
}

export interface SemanticMetricInfo {
  name: string
  expression?: string
  synonyms?: string[]
  datasets?: string[]
  definition?: string
  metric_type?: string
  filter?: string
  agg_time_dimension?: string
  non_additive?: boolean
  datatype?: string
  examples?: string[]
  custom_extensions?: unknown[]
}

export interface SemanticRelationshipInfo {
  name: string
  from?: string
  to?: string
  from_columns?: string[]
  to_columns?: string[]
  cardinality?: string
  fan_out?: string
}

export interface SemanticTimeSpine {
  field?: string
  granularity?: string
  fill?: unknown
}

export interface SemanticModelInfo {
  name: string
  description?: string
  instructions?: string
  metrics: SemanticMetricInfo[]
  datasets: SemanticDatasetInfo[]
  relationships: SemanticRelationshipInfo[]
  version?: number
  examples?: string[]
  time_spine?: SemanticTimeSpine | null
  masking?: { default_policy?: string; bypass_scopes?: string[]; hash_salt_ref?: string }
}

/** DiffCard 的一行(服务端算好:carryover 语义只有服务端知道)。 */
export interface SemanticDiffRow {
  f: string
  before: string
  after: string
  changed: boolean
}

export interface SemanticDraftDiff {
  kind: string
  name: string
  action: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  fields: SemanticDiffRow[]
  /** 干跑失败原因(超契约补键):差不可得时只留原因。 */
  error?: string | null
}

export interface SemanticDraft {
  id: string
  kind: 'metric' | 'field' | 'dataset' | string
  action: 'upsert' | 'delete' | string
  name: string
  note?: string
  status?: 'pending' | 'applied' | 'rejected' | string
  created_at?: string
  payload?: Record<string, unknown> | null
  /** detail 端点逐条附上(create/confirm/reject 响应不带)。 */
  diff?: SemanticDraftDiff
}

/** 漂移影响面快照(发现时冻结;空组也是事实,不隐藏)。 */
export interface SemanticDriftImpact {
  metrics: string[]
  examples: string[]
  rules: string[]
  lessons: string[]
}

export interface SemanticDriftItem {
  /** L1 违反 KB 描述;L2 违反 semantics.yml 契约(本页只显示 L2)。 */
  level: string
  severity: string
  subject: string
  detail: Record<string, unknown>
  first_seen_at?: string | null
  seen_count?: number | null
  drift_id?: number | null
  impact?: SemanticDriftImpact
}

/** 条目形态漂移报告:skipped ≠ 无漂移(skip_reason 必给)。 */
export interface SemanticDrift {
  status: string
  skip_reason?: string | null
  checked_at?: string
  items: SemanticDriftItem[]
}

export interface SemanticDetail {
  enabled: boolean
  model: SemanticModelInfo | null
  /** 扁平串(兼容保留);结构化的在 issue_items。 */
  issues: string[]
  issue_items?: SemanticIssueItem[]
  drafts: {
    pending: SemanticDraft[]
    applied: SemanticDraft[]
    rejected: SemanticDraft[]
  }
  drift?: SemanticDrift
}

export interface SemanticValidateResult {
  /** 取「能不能过写盘门禁」语义:warning 同样拦 confirm,任一非空即 false。 */
  ok: boolean
  errors: SemanticIssueItem[]
  warnings: SemanticIssueItem[]
  normalized: { expression: string }
}

export interface SemanticPreviewResult {
  sql: string
  columns: string[]
  rows: unknown[][]
  row_count: number
  masking_applied?: Record<string, unknown> | null
  warnings?: SemanticIssueItem[]
}

export interface SemanticBatchResultItem {
  id: string
  ok: boolean
  error?: string | null
}

export interface SemanticBatchResult {
  results: SemanticBatchResultItem[]
  applied: number
  failed: number
}

/** GET /v1/admin/semantic/{ds}/history 条目(KB 文件的 git 提交)。 */
export interface SemanticHistoryEntry {
  sha: string
  author: string
  date: string
  subject: string
  trailers?: string
}
