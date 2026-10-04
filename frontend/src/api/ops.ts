/**
 * P4 质量与成本运营台的两个读端点(GET /v1/admin/quality/overview、
 * GET /v1/admin/usage/overview)—— 类型与调用一起放这里,与 P3 的
 * api/overview.ts 同一手法(共享的 types.ts 留给跨页复用的类型)。
 *
 * 三条口径纪律(设计稿 §4.1/§4.2):
 *   · ``null`` = 没测到/不可用,``0`` = 测到且为零,``[]`` = 空结果 —— 页面
 *     不许把三者渲染成同一个样子;
 *   · ``degraded[]`` 是一等返回:某条腿挂了,该块为 null,页面其余部分照常,
 *     受影响的地方显示原因(错误只报异常类型名);
 *   · usage 的整页级失败(内部存储探不通)是 503 **但仍带完整 payload** ——
 *     与 P3 相同,resolve 而不是 throw,页面从 payload 本身渲染该状态。
 */

import { apiFetch, ApiError } from './http'

/** 降级条目:{block, source, error, at};error 只有异常类型名。 */
export interface OpsDegradedEntry {
  block: string
  source: string
  error: string
  at: string
}

/* ── 质量域(§4.1) ───────────────────────────────────────────── */

/** 覆盖率:当前产物对基线 qid 的覆盖;算不出(任一侧无 qid)→ null。 */
export interface OpsCoverage {
  baseline_qids: number
  covered: number
  ratio: number
  /** 同一 qid 出现多次的清单 —— 失败清单不去重(裁决②)。 */
  duplicate_qids: string[]
}

export interface OpsArtifact {
  path: string
  /** scorecard | eval_bird | replay | unknown */
  kind: string
  n: number
  n_judged: number | null
  mtime: string | null
  /** 批次时刻(条目 run_id 尾段的 Unix 秒);解析不出 → null。 */
  batch_at: string | null
  metrics: Record<string, number> | null
  /** 只对「当前」有值;基线侧恒 null。 */
  coverage: OpsCoverage | null
}

export interface OpsGateMetric {
  metric: string
  baseline: number | null
  current: number | null
  delta: number
  direction: string
  tolerance: string
  ok: boolean
  note: string
}

export interface OpsGate {
  /** pass | regress | not_concluded —— 判不了是合法结论,不是错误。 */
  verdict: 'pass' | 'regress' | 'not_concluded'
  reason: string | null
  min_n: number
  tolerances: Record<string, string>
  metrics: OpsGateMetric[]
  unpaired: string[]
  denominator_notes: string[]
}

export interface OpsFailureItem {
  qid: string
  question: string
  verdict: string
  path: string | null
  error: string
  retries: number
  pred_sql: string
  gold_sql: string
  run_id: string
}

export interface OpsFailures {
  total: number
  by_verdict: Record<string, number>
  by_path: Record<string, number>
  items: OpsFailureItem[]
  truncated: boolean
}

export interface OpsFeedback {
  up: number
  down: number
  by_datasource: { datasource: string; up: number; down: number }[]
  pending_lessons: number
  confirmed_lessons: number
  pending_examples: number
  promotion_enabled: boolean
  promotion_threshold: number | null
  promotion_net_upvotes_min: number
  last_rated_at: string | null
}

export interface QualityOverview {
  available: boolean
  generated_at: string
  current: OpsArtifact | null
  baseline: OpsArtifact | null
  gate: OpsGate
  failures: OpsFailures | null
  feedback: OpsFeedback | null
  /** 判定质量全局面(B8);store 未装配/枚举失败 → null(不整节渲染)。 */
  decisions: OpsDecisions | null
  not_measured: string[]
  degraded: OpsDegradedEntry[]
}

/* ── 判定质量域(B8)───────────────────────────────────────────
 * 形状来自后端 eval/quality_report.py 的 fleet_report / build_report
 * (口径唯一权威在 decision/score.py,这里只做类型)。 */

export interface OpsEffectCounts {
  measured: number
  effective: number
  no_effect: number
  unverifiable: number
  errors: number
}

/** 一个 (rule_id, rule_rev) 桶;比率只在分母够时给,不足原因原样带出。 */
export interface OpsQualityBucket {
  key: string
  rule_id: string
  rule_rev: string
  total: number
  ok: number
  alert: number
  error: number
  triggered: number
  first_at: string
  last_at: string
  effects: OpsEffectCounts
  decided: number
  triggered_rate: number | null
  effective_rate: number | null
  /** few_verdicts | few_effects | no_effects(呈现层只翻译措辞)。 */
  insufficient: string[]
}

/** 总计行:计数相加、比率按同一口径重算;与桶同一道分母门。 */
export interface OpsQualitySummary {
  buckets: number
  total: number
  ok: number
  alert: number
  error: number
  triggered: number
  triggered_rate: number | null
  effects: OpsEffectCounts
  decided: number
  effective_rate: number | null
  insufficient: string[]
}

/** 单源报告(与决策页的判定质量卡同源,粒度更粗)。 */
export interface OpsQualityReport {
  datasource: string
  generated_at: string
  buckets: OpsQualityBucket[]
  summary: OpsQualitySummary
}

/** 全局面:跨源只给总量,per-rule 明细原样带在 reports 里。 */
export interface OpsDecisions {
  generated_at: string
  datasources: number
  summary: OpsQualitySummary
  reports: OpsQualityReport[]
}

/* ── 成本域(§4.2) ───────────────────────────────────────────── */

export interface OpsWindow {
  kind: string
  since: string
  until: string
  basis: string
}

export interface OpsCost {
  /** message_metadata —— 口径写在源码里,页面照实展示。 */
  source: string
  /** 采样条数;会话存储未装配 → null。 */
  sampled: number | null
  sample_capped: boolean
  sample_max: number
  /** 采样到顶或一条没测到 → null(截断过的和是错的,0 是"测到且为零")。 */
  tokens: { prompt: number; completion: number; total: number } | null
  cache_tokens: number | null
  unmeasured: {
    assistant_messages: number | null
    without_usage: number | null
    ratio: number | null
  }
  per_question: { mean_total: number; median_total: number } | null
  /** 本轮不测(读侧投影是下轮的事)。 */
  by_model: null
}

export interface OpsBudgetRow {
  datasource?: string
  source?: string
  verdict?: string
  result?: string
  count: number
}

export interface OpsBudget {
  source: string
  lifetime: string
  decisions: OpsBudgetRow[]
  degraded: OpsBudgetRow[]
  kills: OpsBudgetRow[]
}

export interface OpsCache {
  connector: { hits: number; lifetime: string } | null
  answer: null
  prompt: { cache_hit_rate: number; source: string; at: string | null } | null
}

export interface OpsLatency {
  basis: string
  n: number
  sample_capped: boolean
  p50_ms: number | null
  p95_ms: number | null
  series: { date: string; p50_ms: number | null; n: number }[]
  end_to_end: null
}

export interface UsageOverview {
  available: boolean
  window: OpsWindow
  cost: OpsCost | null
  budget: OpsBudget | null
  cache: OpsCache | null
  latency: OpsLatency | null
  not_measured: string[]
  degraded: OpsDegradedEntry[]
  generated_at: string
}

/** 页面窗口档位(端点本身接受 P3 的 1h..90d 全谱,页面只给这三档)。 */
export const OPS_WINDOWS = ['7d', '30d', '90d'] as const
export type OpsWindowKind = (typeof OPS_WINDOWS)[number]

export const OPS_FAILURES_LIMIT = 50

export async function fetchQualityOverview(
  failuresLimit: number = OPS_FAILURES_LIMIT,
): Promise<QualityOverview> {
  return apiGetJson<QualityOverview>(
    `/v1/admin/quality/overview?failures_limit=${failuresLimit}`,
  )
}

export async function fetchUsageOverview(
  window: string,
  opts: { tolerate503?: boolean } = {},
): Promise<UsageOverview> {
  const resp = await apiFetch(
    `/v1/admin/usage/overview?window=${encodeURIComponent(window)}`,
  )
  if (!resp.ok) {
    // 存储挂掉的 503 仍带完整 payload(所有块 null + degraded)——
    // 页面从 payload 渲染该状态;其余非 2xx 照常抛错。
    if (resp.status === 503 && opts.tolerate503 !== false) {
      return (await resp.json()) as UsageOverview
    }
    throw await toApiError(resp)
  }
  return (await resp.json()) as UsageOverview
}

async function apiGetJson<T>(path: string): Promise<T> {
  const resp = await apiFetch(path, { method: 'GET' })
  if (!resp.ok) throw await toApiError(resp)
  return (await resp.json()) as T
}

/** 与 http.ts 的 apiError 同一行为(它没导出,这里保持一模一样的降级)。 */
async function toApiError(resp: Response): Promise<ApiError> {
  const raw = await resp.text().catch(() => '')
  let message = raw || resp.statusText
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed.detail === 'string') message = parsed.detail
  } catch {
    /* keep raw text */
  }
  return new ApiError(resp.status, message)
}
