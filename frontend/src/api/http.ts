// fetch wrapper: Authorization header, JSON parsing, 401 → logout+redirect.
// Stale-session retry and SSE streaming live in sse.ts / the chat store.

import { useAuthStore } from '../stores/auth'
import { router } from '../router'

export class ApiError extends Error {
  status: number
  /** 429 的 `Retry-After`（秒）；后端没给数值头时为 undefined（P6 §2.4）。 */
  retryAfter?: number
  constructor(status: number, message: string, retryAfter?: number) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

/** `Retry-After` 只取数值秒（HTTP-date 形式极少用且各家不一 —— 读不出就当没给，
 *  登录页退化成不带秒数的「稍后重试」，绝不编造等待时长）。 */
function parseRetryAfter(resp: Response): number | undefined {
  const raw = resp.headers?.get?.('retry-after')
  if (!raw) return undefined
  const seconds = Number(raw)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : undefined
}

async function onUnauthorized() {
  const auth = useAuthStore()
  auth.clear()
  const current = router.currentRoute.value
  if (current.name === 'login') return
  // 会话过期要说明来路（P6 §2.4 / §3 全局态）：带 reason 给登录页一次性信息条，
  // 带 next 让用户登录后回到被踢出前的那一页。静默跳转会让用户以为是自己点错了。
  const query: Record<string, string> = { reason: 'expired' }
  if (current.name && current.fullPath && current.fullPath !== '/') {
    query.next = current.fullPath
  }
  await router.push({ name: 'login', query })
}

/** Extract a human error message from a failed response: the backend's
 *  FastAPI errors are `{"detail": "..."}` — parsing that beats dumping the
 *  raw JSON string into the UI (silent failure in admin panel). */
async function apiError(resp: Response): Promise<ApiError> {
  const raw = await resp.text().catch(() => '')
  let message = raw || resp.statusText
  if (raw) {
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed.detail === 'string') {
        message = parsed.detail
      } else if (parsed && Array.isArray(parsed.detail)) {
        message = parsed.detail
          .map((d: { msg?: string }) => d?.msg ?? '')
          .filter(Boolean)
          .join('; ')
      } else if (parsed && parsed.detail && typeof parsed.detail === 'object') {
        // 结构化 detail({code,message},如语义变更评审的 409 stale_change /
        // 422 change_invalid):取 message 逐字展示,缺 message 退 code ——
        // 绝不把原始 JSON 丢进 toast。字符串/数组两支的行为不变。
        const detail = parsed.detail as { code?: string; message?: string }
        if (typeof detail.message === 'string' && detail.message) {
          message = detail.message
        } else if (typeof detail.code === 'string' && detail.code) {
          message = detail.code
        }
      }
    } catch {
      /* keep raw text */
    }
  }
  return new ApiError(resp.status, message, parseRetryAfter(resp))
}

export async function apiFetch(
  path: string,
  options: RequestInit = {},
  { noAuth = false } = {},
): Promise<Response> {
  const auth = useAuthStore()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }
  if (!noAuth && auth.token) {
    headers['Authorization'] = `Bearer ${auth.token}`
  }
  const resp = await fetch(path, { ...options, headers })
  if (resp.status === 401 && !noAuth) {
    await onUnauthorized()
  }
  return resp
}

export async function apiGet<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const resp = await apiFetch(path, { ...options, method: 'GET' })
  if (!resp.ok) {
    throw await apiError(resp)
  }
  return resp.json()
}

export async function apiPost<T = any>(
  path: string,
  body?: unknown,
  options: { noAuth?: boolean } = {},
): Promise<T> {
  const resp = await apiFetch(
    path,
    {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    options,
  )
  if (!resp.ok) {
    throw await apiError(resp)
  }
  return resp.json()
}

export async function apiPatch<T = any>(
  path: string,
  body?: unknown,
): Promise<T> {
  const resp = await apiFetch(path, {
    method: 'PATCH',
    body: JSON.stringify(body ?? {}),
  })
  if (!resp.ok) throw await apiError(resp)
  return resp.json()
}

export async function apiDelete<T = any>(path: string): Promise<T> {
  const resp = await apiFetch(path, { method: 'DELETE' })
  if (!resp.ok) throw await apiError(resp)
  if (resp.status === 204 || resp.status === 205)
    return undefined as unknown as T
  return resp.json()
}

export async function apiPut<T = any>(
  path: string,
  body?: unknown,
): Promise<T> {
  const resp = await apiFetch(path, {
    method: 'PUT',
    body: JSON.stringify(body ?? {}),
  })
  if (!resp.ok) throw await apiError(resp)
  return resp.json()
}
