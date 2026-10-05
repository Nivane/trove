/**
 * 行动支柱的取数层(GET/POST /v1/admin/actions/*)。
 *
 * 与 api/decisions.ts 同一手法:类型与调用一起放这里。三条口径纪律:
 *   · 提案是**已冻结的事实**:payload 在创建时定稿,批准之后外送的就是
 *     它 —— 页面只读展示,绝不提供"改一改再发";
 *   · `open`(未闭环)是三个状态(pending/approved/failed)的并集,而端点
 *     只认单值 —— 这里扇出三次再归并,不用"取全部再前端过滤"糊过去
 *     (列表上限会静默切掉最老的那条);
 *   · `enabled: false` 不是错误:行动层未启用时提案与外送都会被拒,但
 *     模板管理照常可用 —— 页面按"半可用"渲染,不是整页失败。
 */

import { apiGet, apiPost } from './http'

export const ACTION_TABS = ['templates', 'proposals'] as const
export type ActionTab = (typeof ACTION_TABS)[number]

export const ACTION_TEMPLATE_STATUSES = ['pending', 'confirmed'] as const

export const ACTION_PROPOSAL_STATUSES = [
  'pending',
  'approved',
  'failed',
  'dispatched',
  'delivered',
  'rejected',
  'expired',
  'cancelled',
] as const

/** 与后端 models.OPEN_STATUSES 同一口径:三个还在等人(审批/外送/重试)的状态。 */
export const ACTION_PROPOSAL_OPEN_STATUSES = ['pending', 'approved', 'failed'] as const

/**
 * 筛选键的两个虚拟值(都不是真实状态):
 *   · `open` —— 未闭环并集(扇出见 fetchOpenActionProposals);
 *   · `all`  —— 不加筛选。**必须是个可写进 URL 的值**:用空串表示"全部"
 *     的话 useListQuery 会把它当成"回默认",刷新一次就悄悄变回未闭环 ——
 *     筛选器自己说谎是列表页最难查的一类 bug。
 */
export const ACTION_PROPOSALS_ALL = 'all'

/** 提案生命周期动词(闭集,与路由 _DECISIONS 一致)。 */
export const ACTION_DECISIONS = [
  'approve',
  'reject',
  'cancel',
  'dispatch',
  'retry',
  'dry_run',
  'ack',
] as const
export type ActionDecision = (typeof ACTION_DECISIONS)[number]

/**
 * 可批量执行的动词(闭集,与后端 `service.BATCH_DECISIONS` 一致)—— 只有
 * 这两个:dispatch 有外部副作用(批量 = 一键群发),cancel/retry 是纠错。
 */
export const ACTION_BATCH_DECISIONS = ['approve', 'reject'] as const
export type ActionBatchDecision = (typeof ACTION_BATCH_DECISIONS)[number]

export const ACTION_PROPOSALS_LIMIT = 200

/* ── 形状 ─────────────────────────────────────────────── */

export interface ActionTemplateEntry {
  name: string
  title: string
  description: string
  /** pending | confirmed —— 未确认的模板规则不能引用。 */
  status: string
  /** notify | webhook。 */
  action_type: string
  /** {channel, resource};channel 走部署配置解析,模板从不写裸 URL。 */
  target: { channel?: string; resource?: string }
  risk: string
  approvals_required: number
  payload_template: string
  source: string
  created_at: string
  updated_at: string
  digest: string
  /** 文件损坏(手改坏了)时存在 —— 条目仍列出,但不可确认。 */
  error?: string
  /** 创建/确认时返回:注入扫描命中(只报告,不阻断)。 */
  injection_hits?: string[]
}

export interface ActionTemplatesPayload {
  templates: ActionTemplateEntry[]
  enabled: boolean
  /** 本部署配置的通道名 —— 模板引用不在其中的通道 = 开火时才炸。 */
  channels: string[]
  /** 闭集变量名(后端 TEMPLATE_VARIABLES),编辑模板时作提示用。 */
  sample_variables: string[]
}

export interface ActionProposal {
  id: string
  datasource: string
  rule_id: string
  rule_digest: string
  run_id: number | null
  job_id: string
  /** verdict(规则触发) | manual(后续期)。 */
  origin_kind: string
  action_type: string
  template: string
  template_digest: string
  target: Record<string, unknown>
  /** 创建时冻结的载荷 —— 批准后外送的就是这一份,不再重渲染。 */
  payload: Record<string, unknown>
  rationale: string
  evidence_refs: Record<string, unknown>
  severity: string
  priority: number
  risk: string
  status: string
  idempotency_key: string
  created_by: string
  created_at: string
  decided_at: string
  expires_at: string
  dispatched_at: string
  attempts: number
  error: string
}

export interface ActionApproval {
  id?: number
  proposal_id: string
  user_id: string
  action: string
  comment: string
  created_at: string
}

export interface ActionDelivery {
  id?: number
  proposal_id: string
  channel: string
  /**
   * sent | failed | ack | dry_run。`dry_run` 是预演行:什么都没发出去,
   * response_excerpt 存的是"本来会发的那一份",不计入尝试次数。
   */
  status: string
  http_status: number | null
  response_excerpt: string
  error: string
  attempted_at: string
}

/**
 * 一次效果测量(B7 闭环验收;每提案至多一行)。
 *
 * ``outside_band`` 是**三态**且中间那档是要点:
 *   · ``true``  —— 行动后超出噪声带(有可辨识的变化);
 *   · ``false`` —— 行动后无可辨识变化:这是一档结论,不是失败,
 *     也不是"没测出来";
 *   · ``null``  —— 判不了(带不可用 / 对照组缺失),由 ``error`` 或
 *     ``observed`` 说原因。
 * 三态必须渲染成三种样子,把 null 与 false 混同就是在替数据说谎。
 */
export interface ActionOutcome {
  id?: number
  proposal_id: string
  measured_at: string
  window_start: string
  window_end: string
  metric: string
  /** 测量所依据的规则内容版本(质量回评分桶键的一半)。 */
  rule_rev: string
  delta: number | null
  pct: number | null
  outside_band: boolean | null
  z: number | null
  /** its | its+did —— 有对照腿时方法升级,数值始终来自 ITS。 */
  method: string
  confidence: number | null
  /** 完整测量记录(窗口/带/块/组/SQL)—— 可复算承诺的载体。 */
  observed: Record<string, unknown>
  /** 非空 = 测量失败(响亮、一次性,不重试)。 */
  error: string
}

export interface ActionProposalList {
  proposals: ActionProposal[]
  /** 状态 → 计数(status_counts 的真实 COUNT)。 */
  counts: Record<string, number>
  enabled: boolean
}

export interface ActionProposalDetail {
  proposal: ActionProposal
  approvals: ActionApproval[]
  deliveries: ActionDelivery[]
  /** 空列表 = 还没测(未配置测量 / 期没滚过行动日),不是失败。 */
  outcomes: ActionOutcome[]
  /** 已过有效期 —— 清收任务会把它置为 expired,审批闸自己也会挡。 */
  stale: boolean
}

/** 效果结论 → pill 类名:四档各不相同(尤其 null ≠ false)。 */
export function outcomeBandClass(o: ActionOutcome): string {
  if (o.error) return 'pill-danger'
  if (o.outside_band === true) return 'pill-accent'
  if (o.outside_band === false) return 'pill-neutral'
  return 'pill-warn'
}

/* ── 模板 ─────────────────────────────────────────────── */

export async function fetchActionTemplates(
  opts: { confirmedOnly?: boolean } = {},
): Promise<ActionTemplatesPayload> {
  const q = opts.confirmedOnly ? '?confirmed_only=true' : ''
  return apiGet<ActionTemplatesPayload>(`/v1/admin/actions/templates${q}`)
}

export interface ActionTemplateCreateBody {
  name: string
  title: string
  description?: string
  action_type?: string
  target: { channel: string; resource?: string }
  risk?: string
  payload_template: string
}

export async function createActionTemplate(
  body: ActionTemplateCreateBody,
): Promise<{ name: string; status: string; injection_hits: string[]; template: ActionTemplateEntry }> {
  return apiPost('/v1/admin/actions/templates', body)
}

export async function confirmActionTemplate(
  name: string,
): Promise<{ name: string; status: string; injection_hits: string[] }> {
  return apiPost(`/v1/admin/actions/templates/${encodeURIComponent(name)}/confirm`)
}

export async function rejectActionTemplate(
  name: string,
): Promise<{ name: string; status: string }> {
  return apiPost(`/v1/admin/actions/templates/${encodeURIComponent(name)}/reject`)
}

/* ── 提案 ─────────────────────────────────────────────── */

export async function fetchActionProposals(
  opts: { status?: string; datasource?: string; limit?: number } = {},
): Promise<ActionProposalList> {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.datasource) params.set('datasource', opts.datasource)
  params.set('limit', String(opts.limit ?? ACTION_PROPOSALS_LIMIT))
  return apiGet<ActionProposalList>(`/v1/admin/actions/proposals?${params.toString()}`)
}

/**
 * 未闭环 = pending ∪ approved ∪ failed。端点只认单值状态,所以扇出三次
 * 再按创建时间归并 —— 三次都带 counts,取其一即可(同一份真实计数)。
 */
export async function fetchOpenActionProposals(
  datasource = '',
): Promise<ActionProposalList> {
  const pages = await Promise.all(
    ACTION_PROPOSAL_OPEN_STATUSES.map((status) =>
      fetchActionProposals({ status, datasource }),
    ),
  )
  const proposals = pages
    .flatMap((p) => p.proposals)
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
  return {
    proposals,
    counts: pages[0]?.counts ?? {},
    enabled: pages[0]?.enabled ?? false,
  }
}

export async function fetchActionProposal(id: string): Promise<ActionProposalDetail> {
  return apiGet<ActionProposalDetail>(
    `/v1/admin/actions/proposals/${encodeURIComponent(id)}`,
  )
}

/**
 * 施加一个生命周期动词。comment 是所有动词共用的备注;驳回时它是审批
 * 轨迹里唯一的理由,所以调用方应对 reject 要求非空。
 */
export async function decideActionProposal(
  id: string,
  decision: ActionDecision,
  comment = '',
): Promise<{ proposal: ActionProposal }> {
  return apiPost(
    `/v1/admin/actions/proposals/${encodeURIComponent(id)}/${decision}`,
    { comment },
  )
}

/** 批量结果条目:与入参 ids **逐位对应**。 */
export interface ActionBatchResultItem {
  id: string
  ok: boolean
  /** 契约口径:成功是 null,失败是原因文本。 */
  error: string | null
  /** 仅成功条目带 —— 落到的状态(approved / rejected)。 */
  status?: string
}

export interface ActionBatchResult {
  results: ActionBatchResultItem[]
  applied: number
  failed: number
}

/**
 * 批量审批(A6):逐条独立 —— 一条失败不影响其余,成败在 results 里与入参
 * ids 逐位对应。整批前置门(动词非 approve/reject、id 超后端上限)由后端
 * 400 整体拒绝(绝不半执行),所以这里"抛错"只意味着整批没跑;逐条结果
 * 永不抛。
 */
export async function batchDecideActionProposals(
  ids: string[],
  decision: ActionBatchDecision,
  comment = '',
): Promise<ActionBatchResult> {
  return apiPost('/v1/admin/actions/proposals/batch', {
    ids,
    decision,
    comment,
  })
}

/* ── 展示口径(与页面同一处)──────────────────────────── */

/**
 * 该状态下可用的动词(镜像后端状态机;详情抽屉与行内按钮都读这里)。
 * `dry_run` 三个未闭环状态都能用 —— 后端 `_DRY_RUNNABLE` 同口径;它不改
 * 状态,所以放在批准**前**(先看看真实载荷再决定)也成立。
 */
export function proposalVerbs(status: string): ActionDecision[] {
  if (status === 'pending') return ['approve', 'reject', 'cancel', 'dry_run']
  if (status === 'approved') return ['dispatch', 'cancel', 'ack', 'dry_run']
  if (status === 'failed') return ['retry', 'dry_run']
  if (status === 'dispatched' || status === 'delivered') return ['ack']
  return []
}

/** 未闭环(pending/approved/failed)的总数;取不到计数时 null,不是 0。 */
export function openProposalCount(
  counts: Record<string, number> | null,
): number | null {
  if (!counts) return null
  return ACTION_PROPOSAL_OPEN_STATUSES.reduce(
    (sum, s) => sum + (counts[s] ?? 0),
    0,
  )
}

export function proposalStatusClass(status: string): string {
  if (status === 'failed') return 'pill-danger'
  if (status === 'pending') return 'pill-warn'
  if (status === 'approved') return 'pill-accent'
  if (status === 'dispatched' || status === 'delivered') return 'pill-ok'
  return 'pill-neutral'
}

export function templateStatusClass(status: string): string {
  if (status === 'confirmed') return 'pill-ok'
  if (status === 'pending') return 'pill-warn'
  return 'pill-neutral'
}

export function riskClass(risk: string): string {
  if (risk === 'high') return 'pill-danger'
  if (risk === 'medium') return 'pill-warn'
  return 'pill-neutral'
}

/** 展示用:有效期已过且仍待人处理(清收任务还没跑到)。 */
export function isOverdue(p: ActionProposal, now = Date.now()): boolean {
  if (p.status !== 'pending' || !p.expires_at) return false
  const t = Date.parse(p.expires_at)
  return Number.isFinite(t) && t < now
}
