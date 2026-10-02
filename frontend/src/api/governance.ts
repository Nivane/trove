/**
 * P5 治理中心的取数层(§4.1 三个新端点 + §4.2 复用端点)。
 *
 * 三条纪律(与 api/ops.ts / api/overview.ts 同一手法):
 *   · `null`(没取到)/ `0`(取到且为零)/ `[]`(空结果)三者可分 ——
 *     页面把「取不到」渲染成「—/未取到」,不是 0;
 *   · `degraded[]` 是一等返回:跨源扇出由调用方做(N 次请求,每源独立
 *     超时与降级,§4.4g),这里只保证单源的失败可被识别,绝不让整页失败;
 *   · `POST /admin/drift/check` 的 503 带完整报告(`detail.to_dict()` 里
 *     有 `skip_reason`)—— resolve 而不是 throw,页面必须能渲染
 *     「检测未能完成 · skip_reason」(§6-A:本页最重要的一条)。
 *
 * 写动作走各专业页的既有端点,零新语义(附录 A 规则一:同一套动作,
 * 两个入口)。
 */

import { apiFetch, apiGet, apiPost, ApiError } from './http'
import type {
  GovernanceCoverage,
  GovernanceDriftCheck,
  GovernanceDriftItem,
  GovernanceDriftRun,
  GovernanceTableLineage,
  GovernanceTodos,
  KbAsset,
  SemanticBatchResult,
  SemanticHistoryEntry,
} from './types'

/* ── 页面的键档(§3.2 的冻结列表)─────────────────────────── */

export const GOVERNANCE_TABS = ['inbox', 'coverage', 'drift', 'lineage'] as const
export type GovernanceTab = (typeof GOVERNANCE_TABS)[number]

export const GOVERNANCE_INBOX_SORTS = ['oldest', 'newest', 'confidence', 'severity'] as const
export type GovernanceInboxSort = (typeof GOVERNANCE_INBOX_SORTS)[number]

export const GOVERNANCE_DRIFT_STATUSES = ['open', 'waived', 'resolved', 'all'] as const
export const GOVERNANCE_DRIFT_LEVELS = ['L1', 'L2', 'L4'] as const

/** 收件箱六类(顺序即页面展示顺序)。 */
export const GOVERNANCE_TODO_KINDS = [
  'kb_lesson',
  'kb_example',
  'semantic_draft',
  'skill_draft',
  'memory_preference',
  'drift',
] as const

export const GOVERNANCE_TODOS_LIMIT = 50

/* ── ① 收件箱:GET /v1/admin/todos ─────────────────────────── */

export interface GovernanceTodosQuery {
  /** 逗号分隔(空 = 六类全要)。 */
  kind?: string
  ds?: string
  q?: string
  sort?: string
  limit?: number
  offset?: number
}

export async function fetchGovernanceTodos(
  q: GovernanceTodosQuery = {},
): Promise<GovernanceTodos> {
  const params = new URLSearchParams()
  if (q.kind) params.set('kind', q.kind)
  if (q.ds) params.set('ds', q.ds)
  if (q.q) params.set('q', q.q)
  if (q.sort) params.set('sort', q.sort)
  params.set('limit', String(q.limit ?? GOVERNANCE_TODOS_LIMIT))
  if (q.offset) params.set('offset', String(q.offset))
  return apiGet<GovernanceTodos>(`/v1/admin/todos?${params.toString()}`)
}

/* ── ② 覆盖体检:GET /v1/admin/coverage ───────────────────── */

export async function fetchGovernanceCoverage(
  opts: { ds?: string; window?: string } = {},
): Promise<GovernanceCoverage> {
  const params = new URLSearchParams()
  if (opts.ds) params.set('ds', opts.ds)
  if (opts.window) params.set('window', opts.window)
  const qs = params.toString()
  return apiGet<GovernanceCoverage>(`/v1/admin/coverage${qs ? `?${qs}` : ''}`)
}

/* ── ③ 血缘:GET /v1/lineage/tables/{name} ─────────────────── */

export async function fetchTableLineage(
  name: string,
  opts: { datasource: string; column?: string },
): Promise<GovernanceTableLineage> {
  const params = new URLSearchParams({ datasource: opts.datasource })
  if (opts.column) params.set('column', opts.column)
  return apiGet<GovernanceTableLineage>(
    `/v1/lineage/tables/${encodeURIComponent(name)}?${params.toString()}`,
  )
}

/* ── 表详情抽屉的四个既有读端点(§4.2 复用,catalog / KB / lineage)── */

export interface CatalogColumn {
  name: string
  type: string
  nullable?: boolean
  primary_key?: boolean
  foreign_key?: unknown
}

export interface CatalogTableDetail {
  name: string
  schema?: string
  row_count?: number | null
  columns: CatalogColumn[]
}

/** GET /v1/catalog/tables/{name}(表结构:字段/类型/主外键)。 */
export async function fetchCatalogTable(
  name: string,
  ds: string,
): Promise<CatalogTableDetail> {
  return apiGet<CatalogTableDetail>(
    `/v1/catalog/tables/${encodeURIComponent(name)}?datasource=${encodeURIComponent(ds)}`,
  )
}

/** GET /v1/catalog/tables/{name}/ddl。 */
export async function fetchCatalogDdl(name: string, ds: string): Promise<string> {
  const body = await apiGet<{ ddl: string }>(
    `/v1/catalog/tables/${encodeURIComponent(name)}/ddl?datasource=${encodeURIComponent(ds)}`,
  )
  return body.ddl ?? ''
}

/** GET /v1/catalog/tables(catalog 一览:字段数 + 估算行数)。 */
export async function fetchCatalogTables(
  ds: string,
): Promise<{ name: string; schema?: string; columns: number; row_count?: number | null }[]> {
  const body = await apiGet<{
    tables: { name: string; schema?: string; columns: number; row_count?: number | null }[]
  }>(`/v1/catalog/tables?datasource=${encodeURIComponent(ds)}`)
  return body.tables ?? []
}

/** GET /v1/catalog/search(表名/字段名模糊搜索)。 */
export async function searchCatalogTables(
  q: string,
  ds: string,
  limit = 20,
): Promise<{ name: string; schema?: string; columns: number; row_count?: number | null }[]> {
  const params = new URLSearchParams({ q, datasource: ds, limit: String(limit) })
  const body = await apiGet<{
    tables: { name: string; schema?: string; columns: number; row_count?: number | null }[]
  }>(`/v1/catalog/search?${params.toString()}`)
  return body.tables ?? []
}

/** GET /v1/kb/tables/{name}/notes(schema_notes.yml 的 asdict;404 = 该表没有注释)。 */
export interface KbTableNotes {
  description: string
  columns: Record<string, string>
  metrics: Record<string, string>
  enums: Record<string, string>
  stats: Record<string, Record<string, unknown>>
  row_count: number | null
}

export async function fetchKbTableNotes(
  name: string,
  ds: string,
): Promise<KbTableNotes | null> {
  try {
    return await apiGet<KbTableNotes>(
      `/v1/kb/tables/${encodeURIComponent(name)}/notes?datasource=${encodeURIComponent(ds)}`,
    )
  } catch (e) {
    // 404 = 没写过注释(空态,不是失败);其余照抛。
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

/* ── 漂移面(§4.2:drift.py 的 7 条,零改动复用)────────────── */

export interface GovernanceDriftList {
  datasource: string
  count: number
  items: GovernanceDriftItem[]
}

export interface GovernanceDriftRuns {
  datasource: string
  runs: GovernanceDriftRun[]
}

export interface GovernanceDriftDetail {
  drift: GovernanceDriftItem
  /** ImpactSet.to_dict():四个具名组;检测器不采集 → 全空快照(不是缺失)。 */
  impact: { metrics: string[]; examples: string[]; rules: string[]; lessons: string[] }
}

export async function fetchDriftList(
  ds: string,
  opts: { status?: string; level?: string; includeWaived?: boolean } = {},
): Promise<GovernanceDriftList> {
  const params = new URLSearchParams({ datasource: ds })
  if (opts.status) params.set('status', opts.status)
  if (opts.level) params.set('level', opts.level)
  if (opts.includeWaived) params.set('include_waived', 'true')
  return apiGet<GovernanceDriftList>(`/v1/admin/drift?${params.toString()}`)
}

export async function fetchDriftRuns(ds: string): Promise<GovernanceDriftRuns> {
  return apiGet<GovernanceDriftRuns>(
    `/v1/admin/drift/runs?datasource=${encodeURIComponent(ds)}`,
  )
}

export async function fetchDriftDetail(
  ds: string,
  id: number,
): Promise<GovernanceDriftDetail> {
  return apiGet<GovernanceDriftDetail>(
    `/v1/admin/drift/${id}?datasource=${encodeURIComponent(ds)}`,
  )
}

/**
 * 跑一次检测。**未完成时后端回 503、响应体的 detail 就是完整报告** ——
 * 这里把它当数据 resolve 出来,调用方据此渲染「检测未能完成 · skip_reason」。
 * 其余非 2xx(400 未知级别 / 404 源不存在)照常抛。
 */
export async function runDriftCheck(ds: string): Promise<GovernanceDriftCheck> {
  const resp = await apiFetch(
    `/v1/admin/drift/check?datasource=${encodeURIComponent(ds)}`,
    { method: 'POST' },
  )
  const body = await readJson(resp)
  if (isDriftCheck(body)) return body
  const detail =
    body && typeof body === 'object' && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null
  if (resp.status === 503 && isDriftCheck(detail)) return detail
  throw toApiError(resp, body)
}

/** reason 必填(后端 min_length=1)—— 无理由的裁定等于删记录。 */
export async function resolveDrift(ds: string, id: number, reason: string) {
  return apiPost<{ drift: GovernanceDriftItem }>(
    `/v1/admin/drift/${id}/resolve?datasource=${encodeURIComponent(ds)}`,
    { reason },
  )
}

export async function waiveDrift(ds: string, id: number, reason: string) {
  return apiPost<{ drift: GovernanceDriftItem }>(
    `/v1/admin/drift/${id}/waive?datasource=${encodeURIComponent(ds)}`,
    { reason },
  )
}

export interface GovernanceExternalDeclare {
  datasource: string
  subject: string
  kind?: string
  detail?: Record<string, unknown>
  severity?: 'info' | 'warning' | 'critical'
  author?: string
}

/** L4 口径漂移没有检测器,只有人的告知(承接并留痕)。 */
export async function declareExternalDrift(body: GovernanceExternalDeclare) {
  return apiPost<{ drift: GovernanceDriftItem }>('/v1/admin/drift/external', body)
}

/* ── KB 资产体检(§4.2;回滚的受影响文件清单也从这里取)────── */

export interface KbAssetsPayload {
  datasource: string
  assets: KbAsset[]
  /** 相对路径 → 拒绝原因;非空 = 磁盘文件没被镜像采纳(唯一的区分点)。 */
  refused: Record<string, string>
}

export async function fetchKbAssets(ds: string): Promise<KbAssetsPayload> {
  return apiGet<KbAssetsPayload>(
    `/v1/kb/assets?datasource=${encodeURIComponent(ds)}`,
  )
}

/* ── 版本与回滚(§4.2;语义页端点)────────────────────────── */

export async function fetchSemanticHistory(
  ds: string,
  limit = 50,
): Promise<{ datasource: string; history: SemanticHistoryEntry[] }> {
  return apiGet<{ datasource: string; history: SemanticHistoryEntry[] }>(
    `/v1/admin/semantic/${encodeURIComponent(ds)}/history?limit=${limit}`,
  )
}

/**
 * 回滚整个 KB 目录到某个 commit(端点名只写 semantic,恢复的是
 * `ds_dir.glob("*.yml")` —— 爆炸半径见 RollbackDialog 的运行时清单)。
 */
export async function rollbackSemantic(ds: string, sha: string, message = '') {
  return apiPost<{ rolled_back: boolean; datasource: string; sha: string }>(
    `/v1/admin/semantic/${encodeURIComponent(ds)}/rollback`,
    { sha, message: message || undefined },
  )
}

/* ── 收件箱的写动作(各专业页既有端点,§4.2)──────────────── */

export async function confirmLesson(ds: string, key: string, note?: string) {
  return apiPost('/v1/kb/lessons/confirm-one', { datasource: ds, key, note })
}

export async function rejectLesson(ds: string, key: string) {
  return apiPost('/v1/kb/lessons/reject-one', { datasource: ds, key })
}

export async function confirmExample(ds: string, question: string, sql: string) {
  return apiPost('/v1/kb/examples/confirm-one', { datasource: ds, question, sql })
}

export async function rejectExample(ds: string, question: string, sql: string) {
  return apiPost('/v1/kb/examples/reject-one', { datasource: ds, question, sql })
}

export async function confirmSemanticDraft(ds: string, draftId: string) {
  return apiPost(`/v1/admin/semantic/${encodeURIComponent(ds)}/drafts/${encodeURIComponent(draftId)}/confirm`)
}

export async function rejectSemanticDraft(ds: string, draftId: string) {
  return apiPost(`/v1/admin/semantic/${encodeURIComponent(ds)}/drafts/${encodeURIComponent(draftId)}/reject`)
}

/** 逐条独立(一条失败不影响其余),成败都在 results[] 里显式给出。 */
export async function batchSemanticDrafts(
  ds: string,
  action: 'confirm' | 'reject',
  ids: string[],
  note?: string,
): Promise<SemanticBatchResult> {
  return apiPost<SemanticBatchResult>(
    `/v1/admin/semantic/${encodeURIComponent(ds)}/drafts/batch`,
    { action, ids, note },
  )
}

export async function confirmSkill(name: string) {
  return apiPost(`/v1/admin/skills/${encodeURIComponent(name)}/confirm`)
}

export async function rejectSkill(name: string) {
  return apiPost(`/v1/admin/skills/${encodeURIComponent(name)}/reject`)
}

export async function confirmPreferenceDraft(draftId: number | string) {
  return apiPost(`/v1/admin/memory/preferences/${encodeURIComponent(String(draftId))}/confirm`)
}

export async function rejectPreferenceDraft(draftId: number | string) {
  return apiPost(`/v1/admin/memory/preferences/${encodeURIComponent(String(draftId))}/reject`)
}

/* ── 小工具 ───────────────────────────────────────────────── */

async function readJson(resp: Response): Promise<unknown> {
  try {
    return await resp.json()
  } catch {
    return null // 非 JSON 响应按「不是 payload」处理,错误信息退到 statusText
  }
}

function isDriftCheck(v: unknown): v is GovernanceDriftCheck {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  return (
    typeof o.status === 'string' &&
    typeof o.datasource === 'string' &&
    Array.isArray(o.items)
  )
}

/** 与 http.ts 的 apiError 同一行为(它没导出;body 已经解析过就用它)。 */
function toApiError(resp: Response, body: unknown): ApiError {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string' && detail) {
      return new ApiError(resp.status, detail)
    }
    if (Array.isArray(detail)) {
      const msg = detail
        .map((d: { msg?: string }) => d?.msg ?? '')
        .filter(Boolean)
        .join('; ')
      if (msg) return new ApiError(resp.status, msg)
    }
  }
  return new ApiError(resp.status, resp.statusText || 'request failed')
}
