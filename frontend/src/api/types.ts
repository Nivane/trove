// Shared wire types for the SSE event stream and API payloads.

/** GET /v1/sessions 列表条目(会话侧栏一行)。 */
export interface SessionInfo {
  session_id: string
  created_at?: string
  updated_at?: string
  message_count?: number
  /** 自定义标题;空则后端回退首问(前端仍按空处理,不冒名)。 */
  title?: string
  /** 置顶标记 —— 排序 = 置顶在前 + updated_at desc,由存储层查询保证
   *  (分页切片发生在排序之后,前端不做本地重排)。 */
  pinned?: boolean
}

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
  /** 这条答案在哪个主题域范围内算出('' = 未限定);恢复会话时据此还原选择。 */
  topic?: string
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
  /** 分析柱结构化结果(贡献/效应/驱动器树/证据);null/缺席 = 这一轮没有
   *  归因分析(不是「分析为空」)。与答案 markdown 里的分析区块同源。 */
  analysis?: AnalysisPayload | null
}

/** 分析柱一件证据(query log):SQL + 取回的样例行(≤10 行,前端抽屉展示)。 */
export interface AnalysisQueryEvidence {
  id?: number
  purpose?: string // overall | probe | drilldown | driver_tree
  sql?: string
  columns?: string[]
  row_count?: number
  rows?: unknown[][]
  truncated?: boolean
  period?: string // current | base
  filter?: string
}

/** 驱动器树节点:指标按表达式分解(值 + 诚实残差)。 */
export interface AnalysisTreeNode {
  name?: string
  kind?: string // leaf | derived | ratio
  op?: string
  expression?: string
  candidate?: string
  decomposable?: boolean
  note?: string
  current?: number
  base?: number
  delta?: number
  executed?: boolean
  informational?: boolean
  value_source?: string // hop0 | tree_query | none
  residual?: { value?: number | null; exact?: boolean; reason?: string }
  children?: AnalysisTreeNode[]
}

/** 贡献表一行(加性:base/current/delta/contribution;比率:率与权重 + 三效应)。 */
export interface AnalysisContributionRow {
  dim?: string
  base?: number
  current?: number
  delta?: number
  contribution?: number
  base_rate?: number
  current_rate?: number
  base_weight?: number
  current_weight?: number
  within?: number
  composition?: number
  interaction?: number
}

/** 噪声带(块序列的稳健分布;lo/hi 为 null = 带不可用,别画)。 */
export interface AnalysisSeriesBand {
  center?: number | null
  scale?: number | null
  lo?: number | null
  hi?: number | null
  n?: number | null
  method?: string // robust
  degraded?: string[] // insufficient_n | no_data | zero_scale | …
}

/** 历史块序列(v2;B1 起可选,v2 起统计节齐备)。 */
export interface AnalysisSeries {
  grain?: string // day | week | month
  mode?: string // trailing | same_phase
  lookback?: number
  span?: string[]
  labels?: string[]
  values?: number[]
  band?: AnalysisSeriesBand | null
  /** 本期值(被测点);null = 缺,别编数。 */
  current?: number | null
  /** 稳健 z((x−med)/(1.4826·MAD));null = 算不出。 */
  z?: number | null
  /** 是否超出带;null = 判不了(undefined 同理 —— 三态)。 */
  outside?: boolean | null
  /** 样本不足(< LOW_N):结论照给、标照挂。 */
  low_n?: boolean
  /** 带宽(稳健 z 单位,默认 3.5)。 */
  k?: number | null
  /** 位置分数 = (|z|−k)/k 截断 [0,1];**不是概率**(口径同判定侧 gate)。 */
  confidence?: number | null
}

/** state.analysis(分析柱;形状定义见后端 analysis_payload)。
 *
 * v2(B8)只增不减:series / evidence.budget 与节点层 hypotheses 全可选。
 * 兼容机制 = 缺席容忍 + **拿不到不整节渲染** —— 判据是键在不在,
 * 不是版本号(v1 也可能带 series;B1 起它就可选)。 */
export interface AnalysisPayload {
  version?: number
  kind?: string // combined | driver_tree | attribution
  metric?: string
  metric_kind?: string // additive | ratio
  labels?: {
    question?: string
    baseline?: string
    baseline_label?: string
    primary_dimension?: string
    dimensions?: string[]
  }
  total_delta?: number
  table?: AnalysisContributionRow[]
  effects?: Record<string, number> | null
  drilldown?: { dimension?: string; table?: AnalysisContributionRow[] } | null
  tree?: AnalysisTreeNode | null
  charts?: ChartSpec[]
  /** 块序列 + 噪声带(v2 统计节;缺席 = 未启用/降级,整节不渲染)。 */
  series?: AnalysisSeries | null
  /** 交互式假设轮(节点层附加;分析包本体永远零 LLM)。 */
  hypotheses?: Record<string, unknown> | null
  evidence?: {
    datasource?: string
    queries?: AnalysisQueryEvidence[]
    truncated?: boolean
    degraded?: { stage?: string; reason?: string }[]
    /** 查询预算账本(v2;total_query_budget 设置时才有)。 */
    budget?: Record<string, unknown> | null
  }
  /** 降级过(组件截断/预算用尽/不可解析):卡片要如实标,不许静默。 */
  partial?: boolean
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

// ── 治理中心(P5)—— §4.1 三个新端点 + §4.2 复用形状 ────────────
//
// 线 0 一次性放出全部 Governance* 类型(§5.2),其余车道只消费。
// 三条纪律(与 api/overview.ts / api/ops.ts 同一份):
//   · null(没取到)与 0(取到且为零)是两条信息,页面不许压成同一个样子;
//   · degraded[] 是一等返回 —— 某条腿挂了只影响那一块;
//   · 503 带完整「跳过的报告」时必须能被渲染成「检测未能完成」,
//     绝不落 catch → rows=[](那是最严重的一类实现错误)。

/** 收件箱八类 = 概览十类待办减两件运维待办(§2.3;行动两类随 P3 并入)。 */
export type GovernanceTodoKind =
  | 'kb_lesson'
  | 'kb_example'
  | 'semantic_draft'
  | 'skill_draft'
  | 'memory_preference'
  | 'drift'
  | 'action_template'
  | 'action_proposal'

/** 每条目允许的动作;`edit_url` 空 = 没有「编辑后批准」落点(不做禁用按钮)。 */
export interface GovernanceTodoActionable {
  confirm: boolean
  reject: boolean
  batch: boolean
  edit_url?: string | null
}

/** 差的一行(与语义页 manage._draft_diff 同形:{f,before,after,changed})。 */
export interface GovernanceDiffRow {
  f: string
  before: string
  after: string
  changed: boolean
}

/**
 * 服务端算好的差(§4.1① `diff?:{before,after,fields[]}`)。
 * 落地的形态比设计稿写的宽:KB 待审教训的 before 恒为 null、after 是整段
 * 提议文本(字符串,不是记录);语义草稿的 before/after 是实体的原始视图;
 * `action` / `error` 由 manage.py:_draft_diff 透传(error 是「干跑失败」的
 * 唯一表达 —— 裁掉它,前端就分不清「没变化」与「算不出来」)。
 */
export interface GovernanceTodoDiff {
  before: unknown
  after: unknown
  fields: GovernanceDiffRow[]
  action?: string | null
  error?: string | null
}

export interface GovernanceTodoItem {
  kind: GovernanceTodoKind | string
  /** 稳定且可寻址 —— 批量结果与单条重试都靠它(§4.1① id 契约)。 */
  id: string
  ds: string
  title: string
  summary: string
  severity?: string | null
  confidence?: number | null
  created_at?: string | null
  href: string
  actionable: GovernanceTodoActionable
  diff?: GovernanceTodoDiff | null
  source: string
}

/** todos 腿的降级条目(比 coverage 多一个 kind)。ds null = 不知道是哪个源。 */
export interface GovernanceTodosDegraded {
  kind: string
  ds: string | null
  /** 只有异常类型名(Timeout / RuntimeError …),不回传驱动原文。 */
  error: string
  at: string
}

export interface GovernanceTodos {
  items: GovernanceTodoItem[]
  /** 任一类没数出来 → null(下限不可合成,不是 0)。 */
  total: number | null
  /** 逐类计数;某类取不到 → 该类 null(与 0 区分,§4.1① 空态)。 */
  counts: Record<string, number | null>
  generated_at: string
  degraded: GovernanceTodosDegraded[]
}

/* ── 覆盖体检(§4.1②)──────────────────────────────────────── */

export interface GovernanceCoverageModel {
  /** false = 该源没有语义模型(结构事实);解析失败则整块 null + degraded。 */
  enabled?: boolean
  datasets: number
  metrics: number
  /** 无语义模型 → [] —— 页面显示「未建模」,不是「覆盖率 0%」。 */
  declared_tables: string[]
}

/** catalog 不可达 → 整块 null + degraded 条目;绝不把不可达算成「无未建模表」。 */
export interface GovernanceCoveragePhysical {
  tables: number | null
  source: 'catalog' | string | null
}

export interface GovernanceAskedUnmodeled {
  table: string
  queries: number
  last_asked_at: string | null
}

export interface GovernanceRefusedFile {
  file: string
  reason: string
}

export interface GovernanceCoverageSource {
  ds: string
  /** 语义模型读不到(文件读不懂 / 腿超时)→ null,与「没有模型」区分。 */
  model: GovernanceCoverageModel | null
  physical: GovernanceCoveragePhysical | null
  /** null = 差不可得(两侧必须都在场才算;「少了一边」不算「全都没建模」)。 */
  uncovered_tables: string[] | null
  /** lineage 查询历史 − 已声明表;「去建模」深链到语义页对应位置(§6-5)。 */
  asked_unmodeled: GovernanceAskedUnmodeled[] | null
  refused: { count: number; files: GovernanceRefusedFile[] } | null
}

export interface GovernanceCoverageDegraded {
  /** null = 源都列不出来(全都没体检成)。 */
  ds: string | null
  error: string
  at: string
}

export interface GovernanceCoverage {
  generated_at: string
  window: string
  /** null = registry 列不出来源 —— 不是「没有源」。 */
  sources: GovernanceCoverageSource[] | null
  degraded: GovernanceCoverageDegraded[]
}

/* ── 血缘表详情(§4.1③)────────────────────────────────────── */

/** 血缘边。§4.1 只写了 `[Edge]` 未给字段 —— 落地形状是 LineageService 的原样
 *  条目:定义边有 name/kind/sql,查询边只有 kind='query' + sql/last_seen/runs
 *  (name 为空串)。渲染层容忍缺项(doc 缺口,已在交付说明里标注)。 */
export interface GovernanceLineageEdge {
  name?: string
  kind?: string | null
  sql?: string
  dialect?: string
  last_seen?: string
  runs?: number
  updated_at?: string
  sources?: unknown[]
}

export interface GovernanceLineageColumn {
  column: string
  /** 同 Edge:落地是生产者/消费者对象列表,不是表名串。 */
  upstream: GovernanceLineageEdge[]
  downstream: GovernanceLineageEdge[]
}

export interface GovernanceLineageDefinition {
  kind: 'view' | 'ctas' | string
  sql: string
}

export interface GovernanceTableLineage {
  table: string
  datasource: string
  upstream: GovernanceLineageEdge[]
  downstream: GovernanceLineageEdge[]
  columns: GovernanceLineageColumn[]
  definitions: GovernanceLineageDefinition[]
  /** 冷启动(表从未被查过)→ count:0;文案是「还没有查询历史」而非「无依赖」。 */
  query_log: { count: number; last_at: string | null }
}

/* ── 漂移(§4.2 复用 drift.py,形状照 _row_payload)────────── */

export interface GovernanceDriftItem {
  id: number
  datasource: string
  /** L1 结构 / L2 语义 / L4 外部声明。 */
  level: string
  kind: string
  /** 规范化主体标识(表 = `<table>`,列 = `<table>.<column>`)。 */
  subject: string
  severity: string
  status: string
  /** detector | external。 */
  source: string
  detail: Record<string, unknown>
  affected: Record<string, string[]>
  first_seen_at?: string | null
  last_seen_at?: string | null
  seen_count?: number | null
  resolved_at?: string | null
  resolved_by?: string | null
  resolve_reason?: string | null
}

/** GET /v1/admin/drift/runs 的一条运行记录(含 skipped —— 未检测也要看得见)。 */
export interface GovernanceDriftRun {
  id: number
  datasource: string
  started_at: string
  finished_at?: string | null
  /** ok | skipped —— skipped 与「检出 0 条」是两件事。 */
  status: string
  skip_reason?: string | null
  detected: number
  new_count: number
}

/** POST /v1/admin/drift/check 的报告,503 时就是响应体的 detail(不吞)。 */
export interface GovernanceDriftCheck {
  datasource: string
  status: string
  skip_reason?: string | null
  generated_at: string
  detected: number
  new_count: number
  /** 本次检测中**验证通过**的级别;skipped 恒为空集。 */
  levels_verified: string[]
  items: unknown[]
}

/** DiffCard 的三条可选带子(props 冻结面的一部分,见组件内注释)。 */
export interface GovernanceDiffValidation {
  ok: boolean
  errors: string[]
  warnings: string[]
}
