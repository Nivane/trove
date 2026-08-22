// fetch wrapper: Authorization header, JSON parsing, 401 → logout+redirect.
// Stale-session retry and SSE streaming live in sse.ts / the chat store.

import { useAuthStore } from '../stores/auth'
import { router } from '../router'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function onUnauthorized() {
  const auth = useAuthStore()
  auth.clear()
  if (router.currentRoute.value.name !== 'login') {
    await router.push({ name: 'login' })
  }
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
    throw new ApiError(
      resp.status,
      await resp.text().catch(() => resp.statusText),
    )
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
    throw new ApiError(
      resp.status,
      await resp.text().catch(() => resp.statusText),
    )
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
  if (!resp.ok)
    throw new ApiError(
      resp.status,
      await resp.text().catch(() => resp.statusText),
    )
  return resp.json()
}

export async function apiDelete<T = any>(path: string): Promise<T> {
  const resp = await apiFetch(path, { method: 'DELETE' })
  if (!resp.ok)
    throw new ApiError(
      resp.status,
      await resp.text().catch(() => resp.statusText),
    )
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
  if (!resp.ok)
    throw new ApiError(
      resp.status,
      await resp.text().catch(() => resp.statusText),
    )
  return resp.json()
}
