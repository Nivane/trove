/**
 * GET /v1/admin/overview — the P3 dashboard's single round trip.
 *
 * The endpoint's shape is fixed and always complete: a block that could not
 * be fetched is `null`, a count that could not be counted is `null` with
 * `count_exact: false` — 0 and null are two different pieces of information
 * and the page must not collapse them (设计稿 §3「总数慎合」/ §5).
 * `degraded[]` is a first-class return, not an error side channel.
 *
 * One deliberate step past `apiGet`: an HTTP 503 whose body is still the
 * full payload (the storage-level failure, 设计稿 §3「错误 + 重试」) is
 * *resolved* rather than thrown — the page renders that state from the
 * payload itself (`health.status === 'unavailable'`) instead of guessing
 * from a status code.
 */

import { apiFetch, ApiError } from './http'

/** 只读自检结论(三值):`null` = 不知道,basis 说明是哪种(设计稿「只读自检」三态)。 */
export interface OverviewReadonly {
  verified: boolean | null
  /** grants | unverifiable | probe_failed | not_probed */
  basis: string
}

/** 单个数据源的存活探测(错误只报类型名,与 /v1/health 同一纪律)。 */
export interface OverviewDsPing {
  ok: boolean
  error?: string
  readonly: OverviewReadonly
}

export interface OverviewHealth {
  /** ok / degraded / unavailable —— unavailable 是整页级失败(仅存储挂)。 */
  status: 'ok' | 'degraded' | 'unavailable'
  storage: { ok: boolean; error?: string; skipped?: string }
  /** LLM 只报事实:mock / target / providers —— 不下「能不能用」的结论。 */
  llm: { mock: boolean; target: string; providers: number }
  /** 逐源探测;datasources 腿降级时为 null(不等于「没有源」)。 */
  datasources: Record<string, OverviewDsPing> | null
}

export interface OverviewFailureClass {
  class: string
  domain: string
  count: number
}

export interface OverviewUsage {
  /** false = 审计面不存在(没有 auth store);此时各计数为 null,不是 0。 */
  available: boolean
  questions: number | null
  ok: number | null
  success_rate: number | null
  failures: number | null
  failures_by_class: OverviewFailureClass[]
  /** 过渡期来源:审计 details.error 文本粗桶,整块标 approximate。 */
  failures_source: string
  failures_approximate: boolean
  sample_size: number | null
  sample_capped: boolean
  count_exact: boolean
}

/** 待办队列的全部 kinds —— 与后端 overview `_TODO_HREFS` 一一对应（11 类：
 * 审批八类 + 运维三类）。少一个,队列里那一行就没有标签与图标（渲染空行）,
 * 所以这张联合类型是**契约**而非视图偏好。 */
export type OverviewTodoKind =
  | 'kb_lesson'
  | 'kb_example'
  | 'semantic_draft'
  | 'skill_draft'
  | 'memory_preference'
  | 'drift'
  | 'action_template'
  | 'action_proposal'
  | 'job_failed'
  | 'user_nogrant'
  | 'datasource_uninitialized'

export interface OverviewTodoItem {
  kind: OverviewTodoKind
  /** null = 没数出来(降级);0 是「数出来是 0」——两条不同的信息。 */
  count: number | null
  /** false → 前端渲染「≥ N」;降级或计数饱和都会置 false。 */
  count_exact: boolean
  /** false = 本类来源未装配或降级(与 count 0 区分)。 */
  available: boolean
  samples: string[]
  /** 深链由服务端给出;null = 尚无落地页(如记忆偏好草稿,P4 前无界面)。 */
  href: string | null
  /** degraded | capped | '' —— 计数不精确的原因。 */
  note: string
}

export interface OverviewTodos {
  total: number
  count_exact: boolean
  items: OverviewTodoItem[]
}

export interface OverviewDatasourceRow {
  name: string
  /** connected / disconnected(「连不上」与「KB 没建」是两件事)。 */
  status: string
  kb_initialized: boolean | null
  kb_items: Record<string, number> | null
  refused: number | null
  drift_open: number | null
  drift_count_exact: boolean
  readonly: OverviewReadonly
}

/** 接入向导三步:注册 / KB init / 授权 —— 全部读既有字段,无新状态。 */
export interface OverviewWizard {
  registered: number | null
  kb_initialized: number | null
  users_without_grant: number | null
}

export interface OverviewEvent {
  ts: string
  action: string
  username: string
  status: number
  href: string
}

export interface OverviewDegradedEntry {
  block: string
  source: string
  /** 只报类型名(Timeout / RuntimeError …),不回传驱动原文。 */
  error: string
  at: string
}

export interface OverviewPayload {
  generated_at: string
  elapsed_ms: number
  window: string
  health: OverviewHealth | null
  usage: OverviewUsage | null
  todos: OverviewTodos | null
  datasources: OverviewDatasourceRow[] | null
  wizard: OverviewWizard | null
  recent_events: OverviewEvent[] | null
  degraded: OverviewDegradedEntry[]
}

export const OVERVIEW_WINDOWS = ['24h', '7d', '30d'] as const
export type OverviewWindow = (typeof OVERVIEW_WINDOWS)[number]

function isPayload(v: unknown): v is OverviewPayload {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  return (
    typeof o.generated_at === 'string' &&
    typeof o.window === 'string' &&
    'degraded' in o &&
    'health' in o
  )
}

export async function fetchOverview(win: string): Promise<OverviewPayload> {
  const resp = await apiFetch(
    `/v1/admin/overview?window=${encodeURIComponent(win)}`,
  )
  let body: unknown
  try {
    body = await resp.json()
  } catch {
    body = null // 非 JSON 响应按「不是 payload」处理,错误信息退到 statusText
  }
  // 503 也带完整形状(storage 挂)→ 交给页面从 health.status 渲染,不在这里抛。
  if (isPayload(body)) return body
  const detail =
    body && typeof body === 'object' && typeof (body as { detail?: unknown }).detail === 'string'
      ? (body as { detail: string }).detail
      : ''
  throw new ApiError(resp.status, detail || resp.statusText || 'overview request failed')
}
