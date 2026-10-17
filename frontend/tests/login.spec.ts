/**
 * LoginView — P6 §2.4 登录页状态矩阵（八态逐态可测）。
 *
 * fetch 层 mock，真实的 http.ts / stores/auth.ts 留在链上 —— 因此这里同时
 * 钉住 api/http.ts 的两个新契约：ApiError 带 Retry-After 秒数、以及
 * 401 时的 onUnauthorized 会带 reason=expired + next 跳转。
 *
 * 八态：空闲（autofocus）/ 提交中（aria-busy + readonly + 防重复）/
 * 凭据错误 401 / 被节流 429(Retry-After) / 服务不可用 503 / 网络失败 reject /
 * 会话过期 ?reason=expired / 已登录访问登录页（路由守卫既有行为）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import {
  createMemoryHistory,
  createRouter,
  type RouteRecordRaw,
  type Router,
} from 'vue-router'
import { version } from '../package.json'
import LoginView from '../src/views/LoginView.vue'
import { messages } from '../src/i18n'
import { useAuthStore } from '../src/stores/auth'

const Blank = { render: () => null }
const ROUTES: RouteRecordRaw[] = [
  { path: '/', name: 'chat', component: Blank },
  { path: '/login', name: 'login', component: LoginView },
  { path: '/admin/kb', name: 'admin-kb', component: Blank },
]

type Headers = Record<string, string>

/** 形状与 fetch 的 Response 对齐（http.ts 只用到这几个字段）。 */
function jsonResponse(status: number, body: unknown, headers: Headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: async () => JSON.stringify(body),
    json: async () => body,
  }
}

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountView(path = '/login') {
  await router.push(path)
  await router.isReady()
  wrapper = mount(LoginView, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function field(name: 'username' | 'password'): HTMLInputElement {
  const el = document.querySelector<HTMLInputElement>(`input[name="${name}"]`)
  if (!el) throw new Error(`input[name=${name}] not found`)
  return el
}

async function submitWith(username = 'admin', password = 'secret') {
  await wrapper!.find<HTMLInputElement>('input[name="username"]').setValue(username)
  await wrapper!.find<HTMLInputElement>('input[name="password"]').setValue(password)
  await wrapper!.find('.login-btn').trigger('click')
  await flushPromises()
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

/** 登录页挂载时还会问一次 /v1/auth/oidc/status —— 计数只算登录/其它调用。 */
function nonStatusCalls(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter(
    ([input]) => !String(input).includes('/v1/auth/oidc/status'),
  ).length
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  localStorage.clear()
  document.body.innerHTML = ''
  router = createRouter({ history: createMemoryHistory(), routes: ROUTES })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ───────────────────────── 文案修正 / i18n 对齐 ─────────────────────────

describe('i18n：P6-W1 追加块双语齐备，且全表 zh/en 对称', () => {
  const W1_KEYS = [
    'loginClaim',
    'loginClaimSub',
    'loginPoint1',
    'loginPoint2',
    'loginPoint3',
    'loginFormTitle',
    'loginFormSub',
    'loginExpired',
    'loginErrCredentials',
    'loginErrRateLimited',
    'loginErrRateLimitedGeneric',
    'loginRetryIn',
    'loginErrServer',
    'loginErrNetwork',
    'loginRetry',
    'loginLegal',
    'loginVersionTitle',
  ] as const

  it('新增键 zh / en 都有（单边新增立即红灯）', () => {
    for (const key of W1_KEYS) {
      expect(messages.zh[key], `zh:${key}`).toBeTruthy()
      expect(messages.en[key], `en:${key}`).toBeTruthy()
    }
  })

  it('全表 zh / en 键集合完全一致（P6 §6.4）', () => {
    const zh = Object.keys(messages.zh).sort()
    const en = Object.keys(messages.en).sort()
    expect(en).toEqual(zh)
  })
})

// ───────────────────────── 空闲态 ─────────────────────────

describe('空闲态：品牌区 + 表单卡，用户名自动聚焦', () => {
  it('B 案版式：主张、三条卖点、版本行', async () => {
    await mountView()
    const text = bodyText()
    expect(text).toContain(messages.zh.loginClaim)
    for (const key of ['loginPoint1', 'loginPoint2', 'loginPoint3'] as const) {
      expect(text).toContain(messages.zh[key])
    }
    // 版本行来自 vite define（测试环境无 define → package.json 兜底）
    expect(document.querySelector('.lb-version')?.textContent?.trim()).toBe(`v${version}`)
  })

  it('文案修正：不再说「仅管理员可访问管理台」，副标题不再停在管理端说明书', async () => {
    await mountView()
    const text = bodyText()
    expect(text).not.toContain(messages.zh.adminOnlyHint)
    expect(text).not.toContain(messages.zh.loginSubtitle)
    expect(text).toContain(messages.zh.loginLegal)
    expect(text).toContain(messages.zh.loginFormSub)
  })

  it('用户名自动聚焦；无错误块、无错误面板', async () => {
    await mountView()
    expect(document.activeElement).toBe(field('username'))
    expect(document.querySelector('.login-error')).toBeNull()
    expect(document.querySelector('.login-state')).toBeNull()
  })

  it('autocomplete：username / current-password（密码管理器与浏览器填充）', async () => {
    await mountView()
    expect(field('username').getAttribute('autocomplete')).toBe('username')
    expect(field('password').getAttribute('autocomplete')).toBe('current-password')
  })

  it('语言切换局部生效：文案换语、<html lang> 跟随（D6 / D21）', async () => {
    await mountView()
    await wrapper!.find('.login-lang').trigger('click')
    await flushPromises()
    expect(document.documentElement.lang).toBe('en')
    expect(document.querySelector('.login-title')?.textContent?.trim()).toBe(
      messages.en.loginFormTitle,
    )
    expect(document.body.textContent).toContain(messages.en.loginLegal)

    await wrapper!.find('.login-lang').trigger('click')
    await flushPromises()
    expect(document.documentElement.lang).toBe('zh')
    expect(document.querySelector('.login-title')?.textContent?.trim()).toBe(
      messages.zh.loginFormTitle,
    )
  })
})

// ───────────────────────── 提交中 ─────────────────────────

describe('提交中：按钮 aria-busy + 两输入 readonly + 防重复提交', () => {
  it('在飞期间连点两次只发一次请求；输入只读、按钮可访问性状态正确', async () => {
    let release!: (value: unknown) => void
    const inflight = new Promise((resolve) => (release = resolve))
    const fetchMock = vi.fn().mockReturnValue(inflight)
    vi.stubGlobal('fetch', fetchMock)

    await mountView()
    await submitWith()
    await wrapper!.find('.login-btn').trigger('click') // 第二次点击：应被吞掉

    expect(nonStatusCalls(fetchMock)).toBe(1)
    const button = document.querySelector<HTMLButtonElement>('.login-btn')!
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(button.classList.contains('is-loading')).toBe(true)
    expect(field('username').readOnly).toBe(true)
    expect(field('password').readOnly).toBe(true)

    release(
      jsonResponse(200, {
        token: 'tok',
        user: { id: 1, username: 'admin', role: 'admin' },
      }),
    )
    await flushPromises()
    expect(localStorage.getItem('trove_auth_token')).toBe('tok')
    expect(nonStatusCalls(fetchMock)).toBe(1)
  })
})

// ───────────────────────── 四类失败分流 ─────────────────────────

describe('错误按原因分流：401 / 429 / 503 / 网络 reject', () => {
  it('401 → role="alert"「用户名或密码错误 · 请检查后重试」，焦点回密码框并选中', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'invalid username or password' })),
    )
    await mountView()
    await submitWith('admin', 'wrong-pass')

    const alert = document.querySelector('.login-error')
    expect(alert).not.toBeNull()
    expect(alert!.getAttribute('role')).toBe('alert')
    expect(alert!.textContent).toContain(messages.zh.loginErrCredentials)
    // 焦点回密码框，内容被选中：直接重输
    const pass = field('password')
    expect(document.activeElement).toBe(pass)
    expect(pass.selectionStart).toBe(0)
    expect(pass.selectionEnd).toBe(pass.value.length)
  })

  it('429 + Retry-After → 文案含秒数，按钮倒计时禁用且不再发请求', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(429, { detail: 'too many failed login attempts' }, { 'retry-after': '42' }))
    vi.stubGlobal('fetch', fetchMock)

    await mountView()
    await submitWith()

    const alert = document.querySelector('.login-error')
    expect(alert!.getAttribute('role')).toBe('alert')
    expect(alert!.textContent).toContain('42')
    expect(alert!.textContent).toContain('尝试过于频繁')

    const button = document.querySelector<HTMLButtonElement>('.login-btn')!
    expect(button.disabled).toBe(true)
    expect(button.textContent).toContain('42')
    await wrapper!.find('.login-btn').trigger('click')
    await flushPromises()
    expect(nonStatusCalls(fetchMock)).toBe(1)
  })

  it('429 缺 Retry-After 头 → 不编造秒数（不显示倒计时，消息仍说明原因）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(429, { detail: 'too many failures' })),
    )
    await mountView()
    await submitWith()

    const alert = document.querySelector('.login-error')
    expect(alert!.textContent).toContain(messages.zh.loginErrRateLimitedGeneric)
    expect(alert!.textContent).not.toMatch(/\d/) // 没有秒数就没有倒计时数字
    expect(document.querySelector<HTMLButtonElement>('.login-btn')!.disabled).toBe(false)
  })

  it('429 倒计时走完后错误消除、按钮恢复（等待期不是死锁）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(429, { detail: 'too many failures' }, { 'retry-after': '2' }),
      ),
    )
    await mountView()
    // 挂载用真时钟；从这里开始只有倒计时的 setInterval 被接管。
    vi.useFakeTimers()
    await wrapper!.find<HTMLInputElement>('input[name="username"]').setValue('admin')
    await wrapper!.find<HTMLInputElement>('input[name="password"]').setValue('secret')
    await wrapper!.find('.login-btn').trigger('click')
    await vi.advanceTimersByTimeAsync(0)

    expect(document.querySelector('.login-error')?.textContent).toContain('2')

    await vi.advanceTimersByTimeAsync(2100)
    expect(document.querySelector('.login-error')).toBeNull()
    expect(document.querySelector<HTMLButtonElement>('.login-btn')!.disabled).toBe(false)
    vi.useRealTimers()
  })

  it('503 → StatePanel error 态（role="alert"）+ 重试按钮，重试再发一次请求', async () => {
    let attempt = 0
    const fetchMock = vi.fn(async (input: string) => {
      if (String(input).includes('/v1/auth/oidc/status')) return jsonResponse(404, {})
      attempt += 1
      return attempt === 1
        ? jsonResponse(503, { detail: 'storage unavailable' })
        : jsonResponse(200, {
            token: 'tok',
            user: { id: 1, username: 'admin', role: 'admin' },
          })
    })
    vi.stubGlobal('fetch', fetchMock)

    await mountView()
    await submitWith()

    const panel = document.querySelector('.state-panel.is-error')
    expect(panel).not.toBeNull()
    expect(panel!.getAttribute('role')).toBe('alert')
    expect(panel!.textContent).toContain(messages.zh.loginErrServer)
    expect(panel!.textContent).toContain('HTTP 503')
    expect(document.querySelector('.login-error')).toBeNull()

    await wrapper!.find('.state-panel .sp-btn').trigger('click')
    await flushPromises()
    expect(nonStatusCalls(fetchMock)).toBe(2)
    expect(document.querySelector('.state-panel')).toBeNull()
  })

  it('fetch reject →「无法连接到服务器，请检查网络」+ 重试', async () => {
    let attempt = 0
    const fetchMock = vi.fn(async (input: string) => {
      if (String(input).includes('/v1/auth/oidc/status')) return jsonResponse(404, {})
      attempt += 1
      if (attempt === 1) throw new TypeError('Failed to fetch')
      return jsonResponse(200, {
        token: 'tok',
        user: { id: 1, username: 'admin', role: 'admin' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    await mountView()
    await submitWith()

    const panel = document.querySelector('.state-panel.is-error')
    expect(panel!.textContent).toContain(messages.zh.loginErrNetwork)

    await wrapper!.find('.state-panel .sp-btn').trigger('click')
    await flushPromises()
    expect(nonStatusCalls(fetchMock)).toBe(2)
  })
})

// ───────────────────────── 会话过期 ─────────────────────────

describe('会话过期：?reason=expired 一次性信息条', () => {
  it('展示「登录已过期，请重新登录」，并把 reason 从 URL 抹掉（一次性）', async () => {
    await mountView('/login?reason=expired&next=/admin/kb')

    const notice = document.querySelector('.login-notice')
    expect(notice).not.toBeNull()
    expect(notice!.getAttribute('role')).toBe('status')
    expect(notice!.textContent).toContain(messages.zh.loginExpired)
    // 一次性：展示后 URL 里不再留 reason，但 next 原样带走
    expect(router.currentRoute.value.query.reason).toBeUndefined()
    expect(router.currentRoute.value.query.next).toBe('/admin/kb')
  })

  it('没有 reason 时不出现信息条', async () => {
    await mountView()
    expect(document.querySelector('.login-notice')).toBeNull()
  })
})

// ───────────────────────── 登录成功 / 既有行为保留 ─────────────────────────

describe('登录成功与被踢出的归位', () => {
  it('成功登录后回 ?next 指向的页面（会话过期前的原页）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(200, { token: 'tok', user: { id: 1, username: 'admin', role: 'admin' } }),
      ),
    )
    await mountView('/login?next=/admin/kb')
    await submitWith()

    expect(router.currentRoute.value.fullPath).toBe('/admin/kb')
    expect(useAuthStore().isAuthed).toBe(true)
  })

  it('401 跳转带 reason=expired 与 next（api/http.ts 契约，用户知道是被踢出而非点错）', async () => {
    const { router: appRouter } = await import('../src/router')
    const { apiGet } = await import('../src/api/http')
    const auth = useAuthStore()
    auth.token = 'tok'
    auth.user = { id: 1, username: 'admin', role: 'admin' }
    localStorage.setItem('trove_auth_token', 'tok')
    await appRouter.push('/admin/kb')

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'invalid or expired token' })),
    )
    await expect(apiGet('/v1/admin/kb')).rejects.toMatchObject({ status: 401 })
    await flushPromises()

    expect(appRouter.currentRoute.value.name).toBe('login')
    expect(appRouter.currentRoute.value.query.reason).toBe('expired')
    expect(appRouter.currentRoute.value.query.next).toBe('/admin/kb')
    expect(localStorage.getItem('trove_auth_token')).toBeNull()
  })

  it('已登录访问登录页：路由守卫照旧回对话页（既有行为保留）', async () => {
    const { router: appRouter } = await import('../src/router')
    const auth = useAuthStore()
    auth.token = 'tok'
    auth.user = { id: 1, username: 'admin', role: 'admin' }
    await appRouter.push('/login')
    expect(appRouter.currentRoute.value.name).toBe('chat')
  })
})

// ───────────────────────── SSO（OIDC）─────────────────────────

describe('SSO（OIDC）：入口只在后端报告 enabled 时出现', () => {
  function statusFetch(body: unknown, status = 200) {
    return vi.fn(async (input: string) =>
      String(input).includes('/v1/auth/oidc/status')
        ? jsonResponse(status, body)
        : jsonResponse(404, { detail: 'unexpected' }),
    )
  }

  it('enabled=true → 显示 SSO 按钮与「或使用本地账号」分隔线', async () => {
    vi.stubGlobal('fetch', statusFetch({ enabled: true }))
    await mountView()
    const btn = document.querySelector<HTMLButtonElement>('.login-sso-btn')
    expect(btn).not.toBeNull()
    expect(btn!.textContent).toContain(messages.zh.loginSsoBtn)
    expect(document.querySelector('.login-or')?.textContent).toContain(
      messages.zh.loginSsoOr,
    )
  })

  it('enabled=false → 不出现按钮（密码登录原样可用）', async () => {
    vi.stubGlobal('fetch', statusFetch({ enabled: false }))
    await mountView()
    expect(document.querySelector('.login-sso-btn')).toBeNull()
    expect(document.querySelector('.login-or')).toBeNull()
    expect(field('username')).not.toBeNull()
  })

  it('状态读不到（老后端 404 / 网络失败）→ 不挂一个点了必然失败的按钮', async () => {
    vi.stubGlobal('fetch', statusFetch({ detail: 'not enabled' }, 404))
    await mountView()
    expect(document.querySelector('.login-sso-btn')).toBeNull()

    wrapper?.unmount()
    pinia = createPinia()
    setActivePinia(pinia)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await mountView()
    expect(document.querySelector('.login-sso-btn')).toBeNull()
    expect(field('username')).not.toBeNull()
  })

  it('点 SSO → 整页跳 /v1/auth/oidc/login，并带上站内 next', async () => {
    vi.stubGlobal('fetch', statusFetch({ enabled: true }))
    const assign = vi.fn()
    // jsdom 的 Location 成员是 unforgeable（实例上不可 spy）——整只换掉
    // window.location；测试结束在 finally 里换回真实对象。
    const original = window.location
    Object.defineProperty(window, 'location', {
      value: { ...original, assign },
      writable: true,
      configurable: true,
    })
    try {
      await mountView('/login?next=/admin/kb')
      await wrapper!.find('.login-sso-btn').trigger('click')

      expect(assign).toHaveBeenCalledWith('/v1/auth/oidc/login?next=%2Fadmin%2Fkb')
    } finally {
      Object.defineProperty(window, 'location', {
        value: original,
        writable: true,
        configurable: true,
      })
    }
  })
})

describe('SSO 回调失败：封闭错误码 → 本地化说明，只展示一次', () => {
  const CODE_KEYS = [
    ['state_invalid', 'loginSsoErrStateInvalid'],
    ['exchange_failed', 'loginSsoErrExchangeFailed'],
    ['conflict', 'loginSsoErrConflict'],
    ['disabled', 'loginSsoErrDisabled'],
    ['not_provisioned', 'loginSsoErrNotProvisioned'],
  ] as const

  it.each(CODE_KEYS)('%s → %s', async (code, key) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(404, {})))
    const auth = useAuthStore()
    auth.ssoError = code

    await mountView()

    const alert = document.querySelector('.login-error')
    expect(alert).not.toBeNull()
    expect(alert!.getAttribute('role')).toBe('alert')
    expect(alert!.textContent).toContain(messages.zh[key])
    // 一次性：消费后从 store 清空，重渲染/刷新不再看到
    expect(auth.ssoError).toBe('')
  })

  it('未知错误码 → 通用文案，不把原始码甩给用户', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(404, {})))
    useAuthStore().ssoError = 'some_future_code'
    await mountView()

    const alert = document.querySelector('.login-error')
    expect(alert!.textContent).toContain(messages.zh.loginSsoErrGeneric)
    expect(alert!.textContent).not.toContain('some_future_code')
  })
})
