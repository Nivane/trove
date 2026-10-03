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
  /** 规则在两次判定之间被改过 —— 它解释了其余所有变化。 */
  rule_digest_changed: boolean
  prev_rule_digest: string
  rule_digest: string
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
