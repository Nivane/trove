/**
 * 判定历史(GET /v1/admin/decisions/{rule_id}/verdicts、/verdicts/{id})——
 * 类型与调用一起放这里,与 api/ops.ts 同一手法。
 *
 * 两条口径纪律:
 *   · 判定是**不可编辑的事实记录**,页面只读、不给任何编辑/回滚入口;
 *   · ``diff: null`` = 窗口边界(前面没有可比的一条),不是"没有变化" ——
 *     页面必须把两者渲染成不同的样子,否则一次真实变化会被读成平静。
 */

import { apiGet } from './http'

/** 相邻两判之间、单个分组的变化。 */
export interface VerdictDiffGroup {
  dim: string
  /** appeared | vanished | fired | cleared | jump */
  change: string
  /** [前, 后];分组新增/消失时对应端为 null。 */
  triggered: [boolean | null, boolean | null]
  /** 仅 change === 'jump' 时存在。 */
  delta_pct?: [number | null, number | null]
}

export interface VerdictDiff {
  prev_id: number | null
  /** 整份 decisions.yml 在两次判定之间变过(编辑**别的**规则也会亮)。 */
  rule_digest_changed: boolean
  prev_rule_digest: string
  rule_digest: string
  /**
   * **本条规则**的内容版本变过(N2 起的第一把尺)。与 digest 的区别:
   * digest 对整份文件敏感 —— 编辑 B 规则会让 A 的相邻两条也显示"规则已
   * 修改";rev 只在这条规则真的被改过时才变。两者都缺 rev(B2 之前的行)
   * 时前端退回 digest。
   */
  rule_rev_changed: boolean
  prev_rule_rev: string
  rule_rev: string
  status_change: [string, string] | null
  /** fired | cleared | null */
  trigger: string | null
  groups: VerdictDiffGroup[]
}

export interface VerdictBrief {
  id: number
  datasource: string
  rule_id: string
  rule_digest: string
  run_id: number | null
  job_id: string
  /** ok | alert | error —— 与 run 行同一口径。 */
  status: string
  triggered: boolean
  severity: string
  priority: number
  message: string
  error: string
  row_count: number
  evidence_truncated: boolean
  anchor_date: string
  evaluated_at: string
  created_at: string
  /** null = 窗口边界(没有前一条可比),不是"无变化"。 */
  diff: VerdictDiff | null
}

/** 完整判定记录(含证据);``/verdicts/{id}`` 返回它。 */
export interface VerdictRecord extends Omit<VerdictBrief, 'diff'> {
  evidence: Record<string, unknown>
}

export interface VerdictDetail {
  verdict: VerdictRecord
  diff: VerdictDiff | null
}

export interface VerdictList {
  datasource: string
  rule_id: string
  count: number
  verdicts: VerdictBrief[]
}

export async function fetchVerdicts(
  datasource: string,
  ruleId: string,
  opts: { limit?: number; since?: string } = {},
): Promise<VerdictList> {
  const params = new URLSearchParams({ datasource })
  if (opts.limit) params.set('limit', String(opts.limit))
  if (opts.since) params.set('since', opts.since)
  return apiGet<VerdictList>(
    `/v1/admin/decisions/${encodeURIComponent(ruleId)}/verdicts?${params}`,
  )
}

export async function fetchVerdict(id: number): Promise<VerdictDetail> {
  return apiGet<VerdictDetail>(`/v1/admin/decisions/verdicts/${id}`)
}

/** 判定状态 → pill 类名(与页面同一处口径)。 */
export function verdictStatusClass(status: string): string {
  if (status === 'error') return 'pill-danger'
  if (status === 'alert') return 'pill-warn'
  return 'pill-ok'
}

/* ── 判定质量回评(GET /v1/admin/decisions/quality)─────────────────────
 *
 * 分桶键是 ``(rule_id, rule_rev)``,不是整份文件的 digest —— 编辑别的
 * 规则不动这条规则的桶。三个口径都是页面要照实渲染的:
 *
 *   · ``rule_rev_current`` 三态:true=还是这条规则现在的版本 / false=这段
 *     历史判的是旧版本 / null=规则已删或版本无从谈起 —— null 与 false
 *     必须长得不一样(「不知道」不是「旧的」);
 *   · ``effective_rate`` 只在分母够时非 null,读不出比率时看
 *     ``insufficient`` 说原因 —— 页面不给"近似比率";
 *   · ``degraded`` 非空 = 行动层未装配,效果列是空的而不是"没有效果"。
 */

export interface QualityEffectCounts {
  measured: number
  effective: number
  no_effect: number
  unverifiable: number
  errors: number
}

export interface QualityBucket {
  /** ``"<rule_id>@<rule_rev|rev_unknown>"``。 */
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
  effects: QualityEffectCounts
  /** effective + no_effect —— 有效率的真实分母。 */
  decided: number
  triggered_rate: number | null
  effective_rate: number | null
  /** few_verdicts | few_effects | no_effects。 */
  insufficient: string[]
  rule_declared: boolean
  rule_rev_current: boolean | null
}

export interface QualitySummary {
  buckets: number
  total: number
  ok: number
  alert: number
  error: number
  triggered: number
  triggered_rate: number | null
  effects: QualityEffectCounts
  decided: number
  effective_rate: number | null
}

export interface DecisionQuality {
  datasource: string
  digest: string
  limit: number
  verdicts_read: number
  effects_read: number
  buckets: QualityBucket[]
  summary: QualitySummary
  /** [{stage, reason}] —— 非空即某条腿缺席,别把缺数据读成没效果。 */
  degraded: { stage: string; reason: string }[]
}

export async function fetchDecisionQuality(
  datasource: string,
): Promise<DecisionQuality> {
  return apiGet<DecisionQuality>(
    `/v1/admin/decisions/quality?datasource=${encodeURIComponent(datasource)}`,
  )
}
