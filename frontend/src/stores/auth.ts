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
  }),
  getters: {
    isAuthed: (s) => !!s.token && !!s.user,
    isAdmin: (s) => s.user?.role === 'admin',
  },
  actions: {
    async login(username: string, password: string) {
      const body = await apiPost('/v1/auth/login', { username, password }, { noAuth: true })
      this.token = body.token
      this.user = body.user
      localStorage.setItem(TOKEN_KEY, body.token)
    },
    async fetchMe() {
      const body = await apiGet('/v1/auth/me')
      this.user = body.user
      return body.user
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
