/**
 * 订阅面取数层(定时报告订阅:管理面 + 用户面)。
 *
 * 一条语义:一次定时运行产出一份「报告」,订阅 = 订阅者 × 任务;mode 决定
 * 每期都投(`always`)还是只在告警触发时投(`alert_only`),channel 留空 =
 * 沿用任务的 alert_channel(再退 console)。投递发生在 runner 里
 * (best-effort,幂等:同一 run 对同一订阅最多一条 deliveries 行),不在
 * HTTP 路径上 —— 所以这里的写操作只有订阅本身的增删改。
 *
 * 管理面(require_admin)看/管所有人的订阅;用户面严格只看自己的:别人的
 * id 一律 404(不是 403 —— 403 会把「这个 id 属于别人」变成存在性预言机),
 * 前端因此**不做存在性预判**,也不把 404 粉饰成"过期",如实呈现即可。
 */

import { apiDelete, apiGet, apiPatch, apiPost } from './http'

export const SUBSCRIPTION_MODES = ['always', 'alert_only'] as const
export type SubscriptionMode = (typeof SUBSCRIPTION_MODES)[number]

/** 投递记录的状态闭集(后端 deliveries.status)。 */
export const DELIVERY_STATUSES = ['sent', 'failed'] as const

export interface SubscriptionRow {
  id: string
  job_id: string
  /** 列表端富化;job 已删等边缘场景为空串。 */
  job_name: string
  subscriber: string
  /** "" = 继承任务的 alert_channel;显式值 `console` | `webhook:<url>`。 */
  channel: string
  mode: string
  enabled: boolean
  created_by: string
  created_at: string
  updated_at: string
}

export interface SubscriptionDelivery {
  id: number
  subscription_id: string
  job_id: string
  run_id: number
  subscriber: string
  channel: string
  /** sent | failed。 */
  status: string
  error: string
  /** 报告摘要(一行,后端截 512)—— 投递了什么,这里就是什么。 */
  excerpt: string
  created_at: string
}

export interface SubscriptionList {
  subscriptions: SubscriptionRow[]
  total: number
}

export interface DeliveryList {
  deliveries: SubscriptionDelivery[]
  total: number
}

export interface SubscriptionCreateBody {
  subscriber: string
  /** 留空 = 沿用任务通道。 */
  channel?: string
  mode?: SubscriptionMode
}

export interface SubscriptionPatchBody {
  channel?: string
  mode?: SubscriptionMode
  enabled?: boolean
}

/* ── 管理面 ───────────────────────────────────────────── */

export async function fetchSubscriptions(
  opts: { job_id?: string; subscriber?: string } = {},
): Promise<SubscriptionList> {
  const params = new URLSearchParams()
  if (opts.job_id) params.set('job_id', opts.job_id)
  if (opts.subscriber) params.set('subscriber', opts.subscriber)
  const q = params.toString()
  return apiGet<SubscriptionList>(`/v1/admin/subscriptions${q ? `?${q}` : ''}`)
}

export async function createSubscription(
  jobId: string,
  body: SubscriptionCreateBody,
): Promise<{ subscription: SubscriptionRow }> {
  return apiPost(
    `/v1/admin/jobs/${encodeURIComponent(jobId)}/subscriptions`,
    { channel: '', mode: 'always', ...body },
  )
}

export async function patchSubscription(
  id: string,
  body: SubscriptionPatchBody,
): Promise<{ subscription: SubscriptionRow }> {
  return apiPatch(`/v1/admin/subscriptions/${encodeURIComponent(id)}`, body)
}

export async function deleteSubscription(id: string): Promise<void> {
  return apiDelete(`/v1/admin/subscriptions/${encodeURIComponent(id)}`)
}

export async function fetchDeliveries(
  opts: {
    job_id?: string
    subscriber?: string
    subscription_id?: string
    limit?: number
  } = {},
): Promise<DeliveryList> {
  const params = new URLSearchParams()
  if (opts.job_id) params.set('job_id', opts.job_id)
  if (opts.subscriber) params.set('subscriber', opts.subscriber)
  if (opts.subscription_id) params.set('subscription_id', opts.subscription_id)
  params.set('limit', String(opts.limit ?? 50))
  return apiGet<DeliveryList>(`/v1/admin/deliveries?${params.toString()}`)
}

/* ── 用户面(自己的订阅;别人的 id 后端一律 404)───────── */

export async function fetchMySubscriptions(): Promise<SubscriptionList> {
  return apiGet<SubscriptionList>('/v1/subscriptions')
}

export async function patchMySubscription(
  id: string,
  body: SubscriptionPatchBody,
): Promise<{ subscription: SubscriptionRow }> {
  return apiPatch(`/v1/subscriptions/${encodeURIComponent(id)}`, body)
}

export async function deleteMySubscription(id: string): Promise<void> {
  return apiDelete(`/v1/subscriptions/${encodeURIComponent(id)}`)
}

export async function fetchMyDeliveries(
  id: string,
  limit = 50,
): Promise<DeliveryList> {
  return apiGet<DeliveryList>(
    `/v1/subscriptions/${encodeURIComponent(id)}/deliveries?limit=${limit}`,
  )
}

/* ── 展示口径(与页面同一处)──────────────────────────── */

/** 模式 → i18n 键(每期 / 仅告警)。返回字面量联合,`t()` 才收得下。 */
export function modeLabelKey(mode: string): 'subsModeAlways' | 'subsModeAlertOnly' {
  return mode === 'alert_only' ? 'subsModeAlertOnly' : 'subsModeAlways'
}

export function modeClass(mode: string): string {
  return mode === 'alert_only' ? 'pill-warn' : 'pill-neutral'
}

export function deliveryStatusClass(status: string): string {
  return status === 'failed' ? 'pill-danger' : 'pill-ok'
}
