/**
 * 验收集(evalset)管理端取数层 —— 全部挂在
 * `/v1/admin/datasources/{name}/evalset` 下。
 *
 * 三条口径纪律(与后端 ``api/routers/evalset.py`` 的注释同源):
 *
 *   · `latest_verdict` 为 `null` = **未测**,不是 "0" 也不是失败 —— 页面
 *     必须把两者渲染成不同的字样;
 *   · readiness 的 `available:false` 表示**读不出**(资产坏 / run 存储坏),
 *     此时 `counts/by_tier/coverage/evalset_rev` 是 `null`;补成 0 会把
 *     「没读到」洗成「测到了且为零」;
 *   · 试跑的成本确认门是一条 **400**:`detail` 里带 `n_items/tier/model`,
 *     页面读 `ApiError.detail`(R31)原样展示,不解析 message。
 */

import { apiGet, apiPatch, apiPost, ApiError } from './http'

/* ── 条目 ─────────────────────────────────────────────────────────── */

/** 最近一次判定在读时投影;`null` = 该题还没跑过(未测)。 */
export interface EvalsetVerdict {
  run_id: string
  tier: string
  verdict: string | null
  at: string
  reference_error?: unknown
}

export interface EvalsetItem {
  qid: string
  question: string
  reference_sql: string
  source: string
  reference_origin: string
  status: string
  /** 读时派生(verified/semantic/synthetic/benchmark/unknown)。 */
  tier: string
  match?: string
  tags: Record<string, unknown>
  needs_reference?: boolean
  duplicate_of?: string
  leakage?: boolean
  leakage_hit?: string
  note?: string
  created_at: string
  created_by?: string
  confirmed_at?: string
  confirmed_by?: string
  vouched_at?: string
  vouched_by?: string
  rejection_reason?: string
  retired_at?: string
  /**
   * 列表端点注入的最近判定;`GET /{qid}`(单条详情)不注入 → 可能缺席。
   * 缺席与 `null` 都表示「这里没有判定」,读端按未测处理。
   */
  latest_verdict?: EvalsetVerdict | null
}

export interface EvalsetIssue {
  path?: string
  code: string
  message: string
}

export interface EvalsetDegraded {
  code: string
  message: string
  [key: string]: unknown
}

export interface EvalsetCounts {
  pending: number
  confirmed: number
  rejected: number
  retired: number
  total: number
}

export interface EvalsetTierCounts {
  confirmed: number
  pending: number
}

export interface EvalsetListResponse {
  datasource: string
  items: EvalsetItem[]
  counts: EvalsetCounts
  by_tier?: Record<string, EvalsetTierCounts>
  issues?: EvalsetIssue[]
  total?: number
  degraded: EvalsetDegraded[]
}

/* ── 就绪 / 覆盖 ──────────────────────────────────────────────────── */

export interface CoverageCounts {
  covered: number
  total: number
  missing: string[]
}

export interface CoverageLists {
  covered: string[]
  missing: string[]
}

export interface EvalsetCoverage {
  metrics: CoverageCounts
  dimensions: CoverageCounts
  time_grains: CoverageLists
  patterns: CoverageLists
  untagged: number
}

/** run 摘要(``latest.summary.json`` / 历史列表同一形状)。 */
export interface RunSummary {
  run_id?: string
  datasource?: string
  tier?: string
  at?: string
  n?: number
  ex?: number
  ex_hit?: number
  ex_judged?: number
  compile_hit?: number
  completion?: number
  avg_tokens?: number | null
  reference_errors?: number
  leakage_suspect?: number
  skipped?: number
  reference_missing?: number
  evalset_rev?: string
  kb_rev?: string
  kb_rev_end?: string | null
  kb_rev_changed_mid_run?: boolean
  degraded?: EvalsetDegraded[]
}

export interface EvalsetReadiness {
  datasource: string
  /** false = 读不出(资产坏 / run 存储坏);相关字段全为 null。 */
  available: boolean
  counts: EvalsetCounts | null
  by_tier: Record<string, EvalsetTierCounts> | null
  coverage: EvalsetCoverage | null
  /** `<tier> = null` = 该 tier 从没跑过(未测)。 */
  latest_runs: Record<string, RunSummary | null>
  degraded: EvalsetDegraded[]
  evalset_rev: string | null
}

export interface EvalsetRunsResponse {
  datasource: string
  runs: RunSummary[]
  degraded: EvalsetDegraded[]
}

/* ── 预览 / 写操作 ────────────────────────────────────────────────── */

export interface EvalsetPreview {
  qid: string
  columns: string[]
  rows: unknown[][]
  /** "off" 或脱敏报告对象;403 时响应里没有行。 */
  masking: unknown
}

export interface EvalsetItemWrite {
  question?: string
  reference_sql?: string
  tags?: Record<string, unknown>
  note?: string
  force?: boolean
}

export interface EvalsetWriteResult extends EvalsetItem {
  evalset_rev: string
}

export interface EvalsetBatchResultItem {
  qid: string
  ok: boolean
  error?: { code: string; message: string }
}

export interface EvalsetBatchResult {
  results: EvalsetBatchResultItem[]
  applied: number
  failed: number
  evalset_rev: string
}

/* ── 后台任务(生成 / 试跑) ──────────────────────────────────────── */

/**
 * 试跑任务的结局(`RunResult` 的 HTTP 投影,由 `/run/status` 的 `result`
 * 给出)。**缺失/null 不等于成功** —— 旧服务或坏摘要下读端只能报「已结束」。
 */
export interface EvalsetRunResult {
  exit_code: number
  run_id: string
  tier: string
  message: string
  skipped: number
}

/** `init_tasks` 的任务形状;status: running / done / error(idle 是端点兜底)。 */
export interface EvalsetTask {
  id?: string
  datasource?: string
  kind?: string
  status: string
  stage?: string
  progress?: number
  detail?: string
  summary?: string
  error?: string
  /** 仅 `/run/status`:解析后的试跑结局;running/error/坏摘要 → null。 */
  result?: EvalsetRunResult | null
  started_at?: number
  finished_at?: number | null
}

/** 从历史收割的完成报告(``services/evalset/generate.GenerateReport``)。 */
export interface GenerateReport {
  datasource: string
  /** invalid_asset | no_semantic_model | no_llm | empty | ok */
  status: string
  added: number
  skipped: number
  duplicates: number
  conflicts: number
  leakage: number
  by_source: Record<string, number>
  degraded: EvalsetDegraded[]
  issues: EvalsetIssue[]
  evalset_rev: string
}

export interface EvalsetGenerateStatus extends EvalsetTask {
  report?: GenerateReport | null
}

export interface EvalsetRunBody {
  tier: string
  limit?: number
  sample?: number
  compile_only?: boolean
  /** 测试不传:后端会给一条 400 confirmation_required(见下)。 */
  confirm?: boolean
}

export interface EvalsetGenerateBody {
  sources?: string[]
  limit?: number
  lang?: string
  since?: string
  rewrite?: boolean
}

/** 试跑成本确认门的 detail(400 ``confirmation_required``)。 */
export interface EvalsetRunConfirmation {
  code: string
  message: string
  /** detail 缺这个字段就是**未知**,不是「0 道题」—— 读端渲染 '—'。 */
  n_items: number | null
  tier: string
  model: string
}

/* ── 错误 detail 读取 ─────────────────────────────────────────────── */

export interface EvalsetErrorDetail {
  code?: string
  message?: string
  issues?: EvalsetIssue[]
  [key: string]: unknown
}

/** 结构化 `detail`(只认对象;字符串/数组形态没有 code 可读)。 */
export function evalsetErrorDetail(err: unknown): EvalsetErrorDetail | null {
  if (!(err instanceof ApiError)) return null
  const detail = err.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  return detail as EvalsetErrorDetail
}

export function evalsetErrorCode(err: unknown): string {
  const code = evalsetErrorDetail(err)?.code
  return typeof code === 'string' ? code : ''
}

/** 400 `confirmation_required` → 可直接渲染的成本提示;其他错误 → null。 */
export function runConfirmation(err: unknown): EvalsetRunConfirmation | null {
  const detail = evalsetErrorDetail(err)
  if (!detail || detail.code !== 'confirmation_required') return null
  return {
    code: 'confirmation_required',
    message: typeof detail.message === 'string' ? detail.message : '',
    // 缺失/非数字 → null(未知),绝不折成 0:那会被读成「没有题可跑」。
    n_items:
      typeof detail.n_items === 'number' && Number.isFinite(detail.n_items)
        ? detail.n_items
        : null,
    tier: typeof detail.tier === 'string' ? detail.tier : '',
    model: typeof detail.model === 'string' ? detail.model : '',
  }
}

/* ── 端点 ─────────────────────────────────────────────────────────── */

function base(datasource: string): string {
  return `/v1/admin/datasources/${encodeURIComponent(datasource)}/evalset`
}

function itemPath(datasource: string, qid: string): string {
  return `${base(datasource)}/${encodeURIComponent(qid)}`
}

export interface EvalsetListFilters {
  status?: string
  tier?: string
  source?: string
  q?: string
}

export async function fetchEvalsetItems(
  datasource: string,
  filters: EvalsetListFilters = {},
): Promise<EvalsetListResponse> {
  const params = new URLSearchParams()
  for (const key of ['status', 'tier', 'source', 'q'] as const) {
    const value = String(filters[key] ?? '').trim()
    if (value) params.set(key, value)
  }
  const qs = params.toString()
  return apiGet<EvalsetListResponse>(`${base(datasource)}${qs ? `?${qs}` : ''}`)
}

export async function fetchEvalsetReadiness(
  datasource: string,
): Promise<EvalsetReadiness> {
  return apiGet<EvalsetReadiness>(`${base(datasource)}/readiness`)
}

export async function fetchEvalsetRuns(
  datasource: string,
): Promise<EvalsetRunsResponse> {
  return apiGet<EvalsetRunsResponse>(`${base(datasource)}/runs`)
}

/** 单条详情(路由内部从同一份列表投影取,字段与列表行一致)。 */
export async function fetchEvalsetItem(
  datasource: string,
  qid: string,
): Promise<EvalsetItem> {
  return apiGet<EvalsetItem>(itemPath(datasource, qid))
}

/** R30:预览只走 GET。403 ``evalset.masking_refused`` 响应里没有行。 */
export async function previewEvalsetItem(
  datasource: string,
  qid: string,
): Promise<EvalsetPreview> {
  return apiGet<EvalsetPreview>(`${itemPath(datasource, qid)}/preview`)
}

export async function createEvalsetItem(
  datasource: string,
  body: EvalsetItemWrite & { question: string; reference_sql: string },
): Promise<EvalsetWriteResult> {
  return apiPost<EvalsetWriteResult>(`${base(datasource)}/items`, body)
}

export async function patchEvalsetItem(
  datasource: string,
  qid: string,
  body: EvalsetItemWrite,
): Promise<EvalsetWriteResult> {
  return apiPatch<EvalsetWriteResult>(itemPath(datasource, qid), body)
}

export async function confirmEvalsetItem(
  datasource: string,
  qid: string,
  force = false,
): Promise<EvalsetWriteResult> {
  return apiPost<EvalsetWriteResult>(`${itemPath(datasource, qid)}/confirm`, { force })
}

export async function rejectEvalsetItem(
  datasource: string,
  qid: string,
  reason = '',
): Promise<EvalsetWriteResult> {
  return apiPost<EvalsetWriteResult>(`${itemPath(datasource, qid)}/reject`, { reason })
}

export async function vouchEvalsetItem(
  datasource: string,
  qid: string,
  note: string,
): Promise<EvalsetWriteResult> {
  return apiPost<EvalsetWriteResult>(`${itemPath(datasource, qid)}/vouch`, { note })
}

export async function retireEvalsetItem(
  datasource: string,
  qid: string,
  reason = '',
): Promise<EvalsetWriteResult> {
  return apiPost<EvalsetWriteResult>(`${itemPath(datasource, qid)}/retire`, { reason })
}

export async function confirmEvalsetBatch(
  datasource: string,
  qids: string[],
  force = false,
): Promise<EvalsetBatchResult> {
  return apiPost<EvalsetBatchResult>(`${base(datasource)}/confirm`, { qids, force })
}

export async function generateEvalset(
  datasource: string,
  body: EvalsetGenerateBody = {},
): Promise<{ task_id: string; status: string }> {
  return apiPost<{ task_id: string; status: string }>(`${base(datasource)}/generate`, body)
}

export async function fetchGenerateStatus(
  datasource: string,
): Promise<EvalsetGenerateStatus> {
  return apiGet<EvalsetGenerateStatus>(`${base(datasource)}/generate/status`)
}

/**
 * 发起试跑。不传 `confirm` 时后端会给 400 `confirmation_required` ——
 * 这是**预期的一步**,调用方用 :func:`runConfirmation` 取成本提示,
 * 确认后带 `confirm:true` 再调一次。`compile_only:true` 零 LLM,直接调。
 */
export async function runEvalset(
  datasource: string,
  body: EvalsetRunBody,
): Promise<{ task_id: string; status: string }> {
  return apiPost<{ task_id: string; status: string }>(`${base(datasource)}/run`, body)
}

export async function fetchRunStatus(
  datasource: string,
): Promise<EvalsetTask> {
  return apiGet<EvalsetTask>(`${base(datasource)}/run/status`)
}
