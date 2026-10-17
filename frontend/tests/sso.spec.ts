/**
 * SSO（OIDC）前端交接面：
 *
 *  - store.consumeSsoFragment：`#token=...&next=...` / `#error=<code>`
 *    一次性收下、清 fragment、token 落 localStorage；next 前端再验一次
 *    （服务端已消毒，但不让构造的 fragment 变成开放重定向）。
 *  - 路由守卫：fragment 必须在「有没有会话」判定之前被收下，否则首次
 *    导航先把人送去登录页、fragment 随之丢失；收下后按 next 落位。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { useAuthStore } from '../src/stores/auth'

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
    json: async () => body,
  }
}

let pinia: Pinia

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  localStorage.clear()
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

function setHash(hash: string) {
  window.history.replaceState(null, '', `/${hash}`)
}

describe('consumeSsoFragment：一次性、清 URL、next 前端复验', () => {
  it('收下 token → 存 store + localStorage，fragment 从地址栏抹掉', () => {
    setHash('#token=sso-tok&next=%2Fadmin%2Fkb')
    const auth = useAuthStore()

    expect(auth.consumeSsoFragment()).toBe(true)
    expect(auth.token).toBe('sso-tok')
    expect(localStorage.getItem('trove_auth_token')).toBe('sso-tok')
    expect(window.location.hash).toBe('')
    // next 是一次性的：取走后清空
    expect(auth.takeSsoNext()).toBe('/admin/kb')
    expect(auth.takeSsoNext()).toBe('')
  })

  it('收下错误码（无 token）→ store 里留一次性说明，不登录', () => {
    setHash('#error=state_invalid')
    const auth = useAuthStore()

    expect(auth.consumeSsoFragment()).toBe(true)
    expect(auth.ssoError).toBe('state_invalid')
    expect(auth.token).toBe('')
    expect(localStorage.getItem('trove_auth_token')).toBeNull()
    expect(window.location.hash).toBe('')
  })

  it('没有 token/error 的 fragment 原样不动（正常的 #锚点 不受影响）', () => {
    setHash('#section-2')
    const auth = useAuthStore()

    expect(auth.consumeSsoFragment()).toBe(false)
    expect(window.location.hash).toBe('#section-2')
    expect(auth.token).toBe('')
  })

  it.each([
    ['//evil.example/steal', '协议相对地址'],
    ['https://evil.example/x', '绝对地址'],
    ['/login', '登录页本身'],
    ['/login?next=/x', '带 query 的登录页'],
  ])('不安全的 next（%s，%s）被丢弃', (raw) => {
    setHash(`#token=tok&next=${encodeURIComponent(raw)}`)
    const auth = useAuthStore()

    auth.consumeSsoFragment()
    expect(auth.takeSsoNext()).toBe('')
  })
})

describe('路由守卫：fragment 先于登录判定，next 兑现', () => {
  it('带 token 的回调落在 `/`：恢复会话后去 next，而不是丢掉 fragment 去登录页', async () => {
    const { router } = await import('../src/router')
    const auth = useAuthStore()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) =>
        String(input).includes('/v1/auth/me')
          ? jsonResponse(200, {
              user: { id: 7, username: 'oidc-user', role: 'admin' },
            })
          : jsonResponse(404, {}),
      ),
    )

    setHash('#token=sso-tok&next=%2Fadmin%2Fkb')
    await router.push('/')
    await flushPromises()

    expect(auth.token).toBe('sso-tok')
    expect(auth.user?.username).toBe('oidc-user')
    expect(router.currentRoute.value.fullPath).toBe('/admin/kb')
    expect(window.location.hash).toBe('')
  })

  it('App.vue 先 bootstrap 收下 fragment：守卫仍兑现 next（不能只看本次守卫见没见到 fragment）', async () => {
    const { router } = await import('../src/router')
    const auth = useAuthStore()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) =>
        String(input).includes('/v1/auth/me')
          ? jsonResponse(200, {
              user: { id: 7, username: 'oidc-user', role: 'admin' },
            })
          : jsonResponse(404, {}),
      ),
    )

    setHash('#token=sso-tok&next=%2Fsubscriptions')
    // 真实启动顺序：App.vue 的 setup 先跑 bootstrap，fragment 在这里被收下
    await auth.bootstrap()
    expect(auth.token).toBe('sso-tok')

    await router.push('/')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/subscriptions')
    expect(auth.takeSsoNext()).toBe('')
  })

  it('只有错误码的回调：去登录页，说明留给 LoginView 展示', async () => {
    const { router } = await import('../src/router')
    const auth = useAuthStore()

    setHash('#error=exchange_failed')
    await router.push('/')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('login')
    expect(auth.ssoError).toBe('exchange_failed')
    expect(auth.isAuthed).toBe(false)
  })
})
