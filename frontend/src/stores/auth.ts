import { defineStore } from 'pinia'
import { apiPost, apiGet } from '../api/http'

/** Backend USER_ROLES = ("admin", "analyst", "user") — the frontend type
 *  must not be narrower than what /v1/auth/me can return (P6 D9). */
export type Role = 'admin' | 'analyst' | 'user'

export interface UserInfo {
  id: number | string
  username: string
  role: Role
  display_name?: string
  disabled?: boolean
}

const TOKEN_KEY = 'trove_auth_token'

/** Server-sanitized relative path from the SSO fragment; client re-checks. */
function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//')) return ''
  // 登录页本身不是登录后的去处,否则守卫会把它当成目标再跳回来。
  if (raw === '/login' || raw.startsWith('/login?') || raw.startsWith('/login#')) return ''
  return raw
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null as UserInfo | null,
    bootPromise: null as Promise<boolean> | null,
    /** Closed error code from a failed SSO callback (`#error=...`). */
    ssoError: '',
    /** Post-login target delivered with `#token=...&next=...` (one-shot). */
    ssoNext: '',
  }),
  getters: {
    isAuthed: (s) => !!s.token && !!s.user,
    isAdmin: (s) => s.user?.role === 'admin',
    /** W5 阶段二已启用 analyst 只读面（navModel/canEnterConsole 为判定处）。 */
    isAnalyst: (s) => s.user?.role === 'analyst',
  },
  actions: {
    /** Consume `#token=...&next=...` / `#error=<code>` left by the OIDC
     *  callback redirect. Returns true when a fragment was handled.
     *
     *  The fragment is cleared **first** (history.replaceState): a token in
     *  the URL survives in browser history, screenshots and copied links.
     *  `next` is re-sanitized here — the backend already did, but a crafted
     *  fragment must never become an open redirect. */
    consumeSsoFragment(): boolean {
      const hash = typeof window !== 'undefined' ? window.location.hash : ''
      if (!hash || hash.length < 2) return false
      const params = new URLSearchParams(hash.slice(1))
      if (!params.has('token') && !params.has('error')) return false
      const { pathname, search } = window.location
      window.history.replaceState(window.history.state, '', pathname + search)
      const token = params.get('token') || ''
      if (token) {
        this.token = token
        localStorage.setItem(TOKEN_KEY, token)
      }
      const error = params.get('error') || ''
      if (error) this.ssoError = error
      this.ssoNext = safeNext(params.get('next'))
      return true
    },
    /** Take the one-shot post-login target (cleared after use). */
    takeSsoNext(): string {
      const next = this.ssoNext
      this.ssoNext = ''
      return next
    },
    async login(username: string, password: string) {
      const body = await apiPost(
        '/v1/auth/login',
        { username, password },
        { noAuth: true },
      )
      this.token = body.token
      this.user = body.user
      localStorage.setItem(TOKEN_KEY, body.token)
    },
    async fetchMe() {
      const body = await apiGet('/v1/auth/me')
      this.user = body.user
      return body.user
    },
    /** Restore the session from the persisted token on app load. Returns true
     * when a session remains active. 401 inside apiGet triggers onUnauthorized.
     * Cached so concurrent callers (App.vue + router guard) share one attempt. */
    bootstrap(): Promise<boolean> {
      // SSO 回调落在任何页面(默认 `/`)——先收 fragment 再决定有没有会话。
      this.consumeSsoFragment()
      if (this.bootPromise) return this.bootPromise
      if (!this.token) {
        this.user = null
        this.bootPromise = Promise.resolve(false)
        return this.bootPromise
      }
      this.bootPromise = (async () => {
        try {
          await this.fetchMe()
          return !!this.user
        } catch {
          this.clear()
          return false
        }
      })()
      return this.bootPromise
    },
    async logout() {
      try {
        await apiPost('/v1/auth/logout')
      } catch {
        // token may already be invalid — clear locally regardless
      }
      this.clear()
    },
    clear() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
    },
  },
})
