import { defineStore } from 'pinia'
import { apiPost, apiGet } from '../api/http'

export interface UserInfo {
  id: number | string
  username: string
  role: 'admin' | 'user'
  display_name?: string
  disabled?: boolean
}

const TOKEN_KEY = 'trove_auth_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null as UserInfo | null,
    bootPromise: null as Promise<boolean> | null,
  }),
  getters: {
    isAuthed: (s) => !!s.token && !!s.user,
    isAdmin: (s) => s.user?.role === 'admin',
  },
  actions: {
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
