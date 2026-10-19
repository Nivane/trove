/**
 * 覆盖率体检 API 客户端（/v1/admin/question-coverage）。
 *
 * 形状与 `trove/services/question_coverage/` 的返回**逐键对齐** —— 这里
 * 只是类型搬运，不做任何口径计算：覆盖率、分母、告警全由后端算好后
 * 原样渲染。「无样本」在类型里就是 `rate: number | null`，页面不得把
 * null 洗成 0（与后端 `ratio()` 的纪律同源）。
 */
import { apiGet, apiPost, apiPostForm } from './http'

export type CoverageOutcome =
  | 'certified'
  | 'compiled'
  | 'partial'
  | 'soft_miss'
  | 'refused'
  | 'unmatched'
  | 'not_query'
  | 'unplanned'
  | 'error'
  | 'skipped'

export type CoverageRunStatus =
  | 'queued'
  | 'running'
  | 'done'
  | 'error'
  | 'cancelled'
  | 'interrupted'

export type GapKind =
  | 'metric'
  | 'field'
  | 'value'
  | 'entity'
  | 'relation'
  | 'dataset'
  | 'other'

export interface CoverageRatio {
  num: number
  den: number
  /** 分母为 0 时为 null —— 页面显示「无法计算」，绝不显示 0%。 */
  rate: number | null
}

export interface CoverageMetricSet {
  deterministic_coverage: CoverageRatio
  governed_coverage: CoverageRatio
  gap_rate: CoverageRatio
  refusal_rate: CoverageRatio
  unmatched_rate: CoverageRatio
  passthrough_rate: CoverageRatio
}

export interface CoverageSummary {
  taxonomy_version: number
  question_total: number
  evaluated: number
  unmeasured: number
  counts: Record<CoverageOutcome, number>
  by_outcome: { outcome: CoverageOutcome; count: number; evaluated: boolean }[]
  by_miss_reason: { reason: string; count: number }[]
  by_miss_class: { hard: number; soft: number }
  metrics: CoverageMetricSet
  warnings: string[]
  source_label?: string
  llm_calls_total?: number
  limits?: Record<string, unknown>
}

export interface CoverageBlocker {
  code: string
  message: string
  href?: string
}

export interface CoveragePreflight {
  datasource: string
  question_count: number
  llm_question_count: number
  not_query: number
  certified: number
  unmatched: number
  needs_plan: number
  failed: number
  model: string
  /** `node_models` / `model_fast` / `target` —— 与生产同一解析顺序。 */
  model_source: string
  estimated_calls: { typical: number; cap: number }
  hard_call_cap: number
  semantic_digest: string
  blockers: CoverageBlocker[]
  warnings: string[]
  active_run_id: string
}

export interface CoverageParsed {
  questions: string[]
  question_count: number
  source_label: string
  header: string[] | null
  encoding: string
  rows_seen: number
  duplicates: number
  skipped_blank: number
  skipped_long: number
  truncated: boolean
  warnings: string[]
  issues: { row?: number; reason?: string; message?: string }[]
  issues_truncated: boolean
}

export interface CoverageRunCreated {
  run_id: string
  task_id: string
  status: CoverageRunStatus
  datasource: string
  question_count: number
  model: string
  estimated_calls: { typical: number; cap: number }
  hard_call_cap: number
  semantic_digest: string
}

export interface CoverageRunMeta {
  run_id: string
  datasource: string
  status: CoverageRunStatus
  created_by: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  cancel_requested: boolean
  question_count: number
  processed_count: number
  model: string
  semantic_digest: string
  taxonomy_version: number
  /** 历史列表里运行中的 run 可以是空对象。 */
  summary?: CoverageSummary
  error: string
  task_id: string
  questions?: string[]
  previous_done_run_id?: string
}

export interface CoverageStatus {
  run_id: string
  datasource: string
  status: CoverageRunStatus
  created_by: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  cancel_requested: boolean
  question_count: number
  processed_count: number
  model: string
  semantic_digest: string
  taxonomy_version: number
  error: string
  counts: Record<CoverageOutcome, number>
  llm_calls_total: number
  task: {
    status?: string
    stage?: string
    progress?: number
    detail?: string
    error?: string
  } | null
  summary?: CoverageSummary
}

export interface CoverageGap {
  kind: GapKind
  reason: string
  component: string
  label?: { zh?: string; en?: string }
  draftable: boolean
  specs?: { kind: string; name: string; reason?: string }[]
  workbench?: string
  value?: string
}

export interface CoverageResult {
  seq: number
  question: string
  outcome: CoverageOutcome
  outcome_reason: string
  intent: string
  plan_typed: boolean
  miss_class: string
  miss_reason: string
  miss_component: string
  template_name: string
  llm_calls: number
  elapsed_ms: number
  gaps: CoverageGap[]
  detail: {
    matched_tables?: string[]
    plan_preview?: string
    sql_preview?: string
    plan_typed?: boolean
    compile_meta?: Record<string, unknown>
    plan_validation?: { status?: string; errors?: string[] }
    refusal_reason?: string
    no_model?: boolean
    llm_models?: string[]
    configured_model?: string
    model_source?: string
    last_call_error?: boolean
    error?: string
  }
}

export interface CoverageGapGroup {
  kind: GapKind
  reason: string
  component: string
  label: { zh?: string; en?: string }
  count: number
  draftable: boolean
  specs: { kind: string; name: string; reason?: string }[]
  workbench: string
  samples: string[]
  seqs: number[]
}

export interface CoverageReport {
  run: CoverageRunMeta
  summary: CoverageSummary
  results: CoverageResult[]
  gap_groups: CoverageGapGroup[]
}

export interface CoverageRunList {
  datasource: string
  runs: CoverageRunMeta[]
  limit: number
  offset: number
  has_more: boolean
}

export type CoverageChange =
  | 'improved'
  | 'regressed'
  | 'changed'
  | 'same'
  | 'not_comparable'

export interface CoverageDiffRow {
  seq: number
  base_seq: number
  question: string
  base_outcome: CoverageOutcome
  outcome: CoverageOutcome
  base_reason: string
  reason: string
  change: CoverageChange
}

export interface CoverageDiff {
  run_id: string
  base_run_id: string
  reason: string
  base_created_at?: string
  base_semantic_digest?: string
  semantic_digest?: string
  semantic_changed: boolean
  base_taxonomy_version?: number
  taxonomy_version?: number
  taxonomy_changed: boolean
  matched: number
  base_only: number
  current_only: number
  changes: Record<CoverageChange, number>
  questions: CoverageDiffRow[]
  summary_delta: Record<
    string,
    {
      base: Partial<CoverageRatio>
      current: Partial<CoverageRatio>
      /** 比率差（小数；两侧任一无样本 → null）。 */
      delta: number | null
    }
  >
}

export type DraftStatus =
  | 'created'
  | 'exists'
  | 'invalid'
  | 'not_draftable'
  | 'stale'

export interface CoverageDraftRow {
  seq: number
  question: string
  kind: string
  name: string
  reason: string
  label?: { zh?: string; en?: string }
  status: DraftStatus
  draft_id: string
  errors: string[]
}

export interface CoverageDraftsResult {
  run_id: string
  datasource: string
  selected_questions: number
  requested: number
  truncated: boolean
  truncated_assets: number
  counts: Record<DraftStatus, number>
  results: CoverageDraftRow[]
}

// ── calls ───────────────────────────────────────────────────────────────

export function preflightCoverage(body: {
  datasource: string
  questions: string[]
  source_label?: string
}): Promise<CoveragePreflight> {
  return apiPost('/v1/admin/question-coverage/preflight', body)
}

export function parseCoverageCsv(
  file: File,
  questionColumn = '',
): Promise<CoverageParsed> {
  const form = new FormData()
  form.append('file', file)
  if (questionColumn) form.append('question_column', questionColumn)
  return apiPostForm('/v1/admin/question-coverage/parse-csv', form)
}

export function createCoverageRun(body: {
  datasource: string
  questions: string[]
  source_label?: string
  confirm_cost: boolean
}): Promise<CoverageRunCreated> {
  return apiPost('/v1/admin/question-coverage/runs', body)
}

export function listCoverageRuns(
  datasource: string,
  params: { status?: string; limit?: number; offset?: number } = {},
): Promise<CoverageRunList> {
  const q = new URLSearchParams({ datasource })
  if (params.status) q.set('status', params.status)
  if (params.limit != null) q.set('limit', String(params.limit))
  if (params.offset != null) q.set('offset', String(params.offset))
  return apiGet(`/v1/admin/question-coverage/runs?${q.toString()}`)
}

export function coverageRunStatus(runId: string): Promise<CoverageStatus> {
  return apiGet(
    `/v1/admin/question-coverage/runs/${encodeURIComponent(runId)}/status`,
  )
}

export function fetchCoverageReport(runId: string): Promise<CoverageReport> {
  return apiGet(`/v1/admin/question-coverage/runs/${encodeURIComponent(runId)}`)
}

export function fetchCoverageDiff(
  runId: string,
  baseRunId = '',
): Promise<CoverageDiff> {
  const q = baseRunId
    ? `?base_run_id=${encodeURIComponent(baseRunId)}`
    : ''
  return apiGet(
    `/v1/admin/question-coverage/runs/${encodeURIComponent(runId)}/diff${q}`,
  )
}

export function draftCoverageGaps(
  runId: string,
  body: { seqs?: number[]; all?: boolean },
): Promise<CoverageDraftsResult> {
  return apiPost(
    `/v1/admin/question-coverage/runs/${encodeURIComponent(runId)}/drafts`,
    body,
  )
}

export function cancelCoverageRun(
  runId: string,
): Promise<{ run_id: string; status: string }> {
  return apiPost(
    `/v1/admin/question-coverage/runs/${encodeURIComponent(runId)}/cancel`,
    {},
  )
}
