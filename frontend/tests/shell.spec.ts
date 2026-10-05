/**
 * P6 W0 壳地基 — 契约与交互测试。
 *
 * 四层：
 *  1. navModel 契约：四组 16 项、路径唯一、双语 key 齐备、可见性默认拒绝；
 *     每个可见项的 path 在真实路由表里存在，且 routes 的 meta.roles 兜住。
 *  2. 前缀高亮与守卫角色矩阵（纯函数，跑真实路由表）。
 *  3. ⌘K 过滤（中英文命中）。
 *  4. 壳的挂载：总览（既有页，零改动）在壳内渲染；键盘 K1/K3/K5/K6、
 *     角标（有数据显示 / 0 与取不到隐藏）、离线横幅、路由进度条。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import ElementPlus from 'element-plus'
import {
  createMemoryHistory,
  createRouter,
  RouterView,
  type Router,
} from 'vue-router'

// 只替换取数函数：OverviewView 还要用模块里的 OVERVIEW_WINDOWS 等常量。
vi.mock('../src/api/overview', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/overview')>()
  return { ...actual, fetchOverview: vi.fn() }
})

import { fetchOverview, type OverviewPayload } from '../src/api/overview'
import AdminLayout from '../src/views/AdminLayout.vue'
import OverviewView from '../src/views/admin/OverviewView.vue'
import { messages } from '../src/i18n'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import { resetNetworkStatusForTests } from '../src/composables/useNetworkStatus'
import { badgesFromPayload, formatBadge } from '../src/composables/useNavBadges'
import { canAccess, requiredRoles, router as appRouter } from '../src/router'
import {
  BADGE_TITLE_KEYS,
  NAV_GROUPS,
  NAV_ITEMS,
  STYLEGUIDE_READY,
  filterNavItems,
  isPathActive,
  navItemsFor,
} from '../src/components/layout/navModel'

const mockFetch = vi.mocked(fetchOverview)

// ───────────────────────── navModel 契约 ─────────────────────────

describe('navModel — 四组 16 项 IA 单一来源', () => {
  it('四组、16 项、分组顺序固定、路径不重复', () => {
    expect(NAV_GROUPS.map((g) => g.key)).toEqual([
      'ops',
      'modeling',
      'governance',
      'system',
    ])
    expect(NAV_ITEMS).toHaveLength(16)
    const paths = NAV_ITEMS.map((i) => i.path)
    expect(new Set(paths).size).toBe(paths.length)
    const counts = { ops: 4, modeling: 6, governance: 3, system: 3 }
    for (const [group, n] of Object.entries(counts)) {
      expect(NAV_ITEMS.filter((i) => i.group === group)).toHaveLength(n)
    }
  })

  it('每个 labelKey / 角标标题键在 zh 与 en 都齐备（i18n 对齐）', () => {
    for (const item of NAV_ITEMS) {
      expect(messages.zh[item.labelKey], `zh:${item.labelKey}`).toBeTruthy()
      expect(messages.en[item.labelKey], `en:${item.labelKey}`).toBeTruthy()
    }
    for (const key of Object.values(BADGE_TITLE_KEYS)) {
      expect(messages.zh[key], `zh:${key}`).toBeTruthy()
      expect(messages.en[key], `en:${key}`).toBeTruthy()
    }
  })

  it('每个可见项的 path 在真实路由表里存在且要求 admin', () => {
    for (const item of navItemsFor('admin')) {
      const resolved = appRouter.resolve(item.path)
      expect(resolved.matched.length, item.path).toBeGreaterThan(0)
      expect(requiredRoles(resolved), item.path).toContain('admin')
    }
  })

  it('组件规范项保留在 IA 里但显式过滤（URL 直达页，无侧栏死链）', () => {
    expect(STYLEGUIDE_READY).toBe(false)
    expect(NAV_ITEMS.some((i) => i.path === '/admin/styleguide')).toBe(true)
    expect(
      navItemsFor('admin').some((i) => i.path === '/admin/styleguide'),
    ).toBe(false)
    // 不进侧栏的定位（W4 交付）：它不该出现在任何角色的可见列表里。
    expect(filterNavItems('组件规范', 'admin')).toHaveLength(0)
  })

  it('角色可见性：W5 阶段二 analyst 只见只读面 6 项，user 与未登录仍为零', () => {
    expect(navItemsFor('admin')).toHaveLength(15)
    expect(navItemsFor('analyst')).toHaveLength(6)
    expect(navItemsFor('user')).toHaveLength(0)
    expect(navItemsFor(undefined)).toHaveLength(0)
  })
})

// ───────────────────────── 高亮与守卫矩阵 ─────────────────────────

describe('isPathActive — 前缀匹配高亮（K3）', () => {
  it('子路由保父项高亮；/admin 精确匹配防全亮', () => {
    expect(isPathActive('/admin/kb', '/admin/kb')).toBe(true)
    expect(isPathActive('/admin/kb/asset/42', '/admin/kb')).toBe(true)
    expect(isPathActive('/admin', '/admin')).toBe(true)
    expect(isPathActive('/admin/users', '/admin')).toBe(false)
    expect(isPathActive('/adminx', '/admin')).toBe(false)
    expect(isPathActive('/admin/kb', '/admin/users')).toBe(false)
  })
})

describe('守卫角色矩阵（meta.roles 地基）', () => {
  it('admin 子树默认拒绝；W5 只读面按声明放行 analyst；非 admin 路径不设门槛', () => {
    // W5 阶段二：运营 4 页 + 治理中心 / 审计日志声明 ADMIN_ANALYST
    // （逐项清单与三处一致契约由 tests/analyst.spec.ts 钉住）。
    for (const path of ['/admin', '/admin/ops', '/admin/jobs', '/admin/governance']) {
      expect(requiredRoles(appRouter.resolve(path)), path).toEqual([
        'admin',
        'analyst',
      ])
    }
    // 未声明只读的页 + 壳内 404 兜底仍回到默认拒绝（['admin']）。
    for (const path of [
      '/admin/users',
      '/admin/kb?ds=demo',
      '/admin/nonexistent',
    ]) {
      expect(requiredRoles(appRouter.resolve(path)), path).toEqual(['admin'])
    }
    expect(requiredRoles(appRouter.resolve('/'))).toBeNull()
    expect(requiredRoles(appRouter.resolve('/nope'))).toBeNull()
  })

  it('canAccess：越权一律拒绝，未声明角色一律放行', () => {
    expect(canAccess('admin', ['admin'])).toBe(true)
    expect(canAccess('analyst', ['admin'])).toBe(false)
    expect(canAccess('user', ['admin'])).toBe(false)
    expect(canAccess(undefined, ['admin'])).toBe(false)
    expect(canAccess('user', null)).toBe(true)
    expect(canAccess(undefined, null)).toBe(true)
  })

  it('既有路由与兼容重定向逐条保留', () => {
    // resolve 不跟重定向 —— 直接调用 redirect 函数验证兼容契约（query/hash 原样带走）。
    const overview = appRouter
      .getRoutes()
      .find((r) => r.path === '/admin/overview')!
    expect(typeof overview.redirect).toBe('function')
    const toAdmin = (
      overview.redirect as (to: {
        path: string
        query: Record<string, string>
        hash: string
      }) => unknown
    )({ path: '/admin/overview', query: { win: '7d' }, hash: '#todos' })
    expect(toAdmin).toEqual({
      path: '/admin',
      query: { win: '7d' },
      hash: '#todos',
    })

    const usage = appRouter.getRoutes().find((r) => r.path === '/admin/usage')!
    const toOps = (
      usage.redirect as (to: {
        path: string
        query: Record<string, string>
      }) => unknown
    )({ path: '/admin/usage', query: { win: '30d' } })
    expect(toOps).toEqual({
      path: '/admin/ops',
      query: { win: '30d', tab: 'usage' },
    })

    expect(appRouter.resolve('/admin').name).toBe('admin-overview')
    expect(appRouter.resolve('/admin/nonexistent').name).toBe('admin-not-found')
    expect(appRouter.resolve('/nope').name).toBe('not-found')
  })
})

// ───────────────────────── ⌘K 过滤 ─────────────────────────

describe('filterNavItems — ⌘K 过滤', () => {
  it('空查询给出全部可见项；中英文页名均可命中（大小写不敏感）', () => {
    expect(filterNavItems('', 'admin')).toHaveLength(15)
    expect(filterNavItems('知识', 'admin').map((i) => i.path)).toEqual([
      '/admin/kb',
    ])
    expect(filterNavItems('KNOWLEDGE', 'admin').map((i) => i.path)).toEqual([
      '/admin/kb',
    ])
    expect(filterNavItems('overview', 'admin').map((i) => i.path)).toEqual([
      '/admin',
    ])
    // 路径兜底
    expect(filterNavItems('/admin/audit', 'admin').map((i) => i.path)).toEqual([
      '/admin/audit',
    ])
  })

  it('无匹配返回空；分析师过滤=只读面 6 项；普通用户为空', () => {
    expect(filterNavItems('不存在的页面', 'admin')).toHaveLength(0)
    expect(filterNavItems('', 'analyst')).toHaveLength(6)
    expect(filterNavItems('', 'user')).toHaveLength(0)
  })
})

// ───────────────────────── 角标推导（纯函数） ─────────────────────────

function makePayload(
  overrides: Partial<OverviewPayload> = {},
): OverviewPayload {
  return {
    generated_at: '2026-10-03T08:00:00Z',
    elapsed_ms: 12,
    window: '24h',
    health: {
      status: 'ok',
      storage: { ok: true },
      llm: { mock: true, target: 'mock', providers: 1 },
      datasources: {},
    },
    usage: null,
    todos: {
      total: 3,
      count_exact: true,
      items: [
        {
          kind: 'kb_lesson',
          count: 2,
          count_exact: true,
          available: true,
          samples: [],
          href: '/admin/kb?tab=pending',
          note: '',
        },
        {
          kind: 'semantic_draft',
          count: 1,
          count_exact: true,
          available: true,
          samples: [],
          href: null,
          note: '',
        },
        {
          kind: 'job_failed',
          count: null,
          count_exact: false,
          available: false,
          samples: [],
          href: null,
          note: 'degraded',
        },
      ],
    },
    datasources: [],
    wizard: { registered: 1, kb_initialized: 1, users_without_grant: 2 },
    recent_events: [],
    degraded: [],
    ...overrides,
  }
}

describe('badgesFromPayload — 角标只在有数时成立', () => {
  it('从既有字段推导：待办 / 跨源 pending / 未授权用户', () => {
    const badges = badgesFromPayload(makePayload())
    expect(badges.todos).toEqual({ total: 3, exact: true })
    expect(badges.govPending).toEqual({ total: 3, exact: true }) // kb_lesson + semantic_draft
    expect(badges.kbPending).toEqual({ total: 2, exact: true })
    expect(badges.semPending).toEqual({ total: 1, exact: true })
    expect(badges.nogrant).toEqual({ total: 2, exact: true })
    // job_failed 降级（available=false）→ 不成立；0 个问题源 → 0 但不显示
    expect(badges.failedJobs).toBeUndefined()
    expect(badges.dsIssues).toEqual({ total: 0, exact: true })
  })

  it('计数不精确时渲染「≥ N」；0 与取不到都不显示', () => {
    expect(formatBadge({ total: 5, exact: false })).toBe('≥ 5')
    expect(formatBadge({ total: 5, exact: true })).toBe('5')
    expect(formatBadge({ total: 0, exact: true })).toBeNull()
    expect(formatBadge(undefined)).toBeNull()
  })
})

// ───────────────────────── 壳挂载（交互） ─────────────────────────

const StubPage = { render: () => h('div', { class: 'page-stub' }, 'stub') }
const Blank = { render: () => h('div') }
/** 挂载宿主：让路由表自己渲染壳（直接在 RouterView 外挂 AdminLayout 会自递归）。 */
const Host = defineComponent({ render: () => h(RouterView) })

let pinia: Pinia
let router: Router
let wrapper: VueWrapper | null = null

async function mountShell(path = '/admin') {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'chat', component: Blank },
      {
        path: '/admin',
        component: AdminLayout,
        meta: { roles: ['admin'] },
        children: [
          {
            path: '',
            name: 'admin-overview',
            component: OverviewView,
            meta: { titleKey: 'ovTitle' },
          },
          {
            path: 'kb',
            name: 'admin-kb',
            component: StubPage,
            meta: { titleKey: 'kb' },
          },
          {
            path: 'ops',
            name: 'admin-ops',
            component: StubPage,
            meta: { titleKey: 'ops' },
          },
        ],
      },
    ],
  })
  await router.push(path)
  await router.isReady()
  wrapper = mount(Host, {
    global: { plugins: [pinia, router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

function navLink(href: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`a.admin-nav-item[href="${href}"]`)
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  localStorage.clear()
  resetNetworkStatusForTests()
  mockFetch.mockReset()
  mockFetch.mockResolvedValue(makePayload())
  const auth = useAuthStore()
  auth.token = 'test-token'
  auth.user = { id: 1, username: 'admin', display_name: 'Admin', role: 'admin' }
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('既有管理页模块契约（壳改造后 11 页零改动仍可加载）', () => {
  it('每个 views/admin/*.vue 都能解析出组件；主区容器契约一致', async () => {
    const modules = import.meta.glob('../src/views/admin/*.vue')
    const names = Object.keys(modules)
    expect(names.length).toBeGreaterThanOrEqual(11)
    for (const [path, load] of Object.entries(modules)) {
      const mod = (await load()) as { default?: unknown }
      expect(mod.default, path).toBeTruthy()
      expect(typeof mod.default, path).toBe('object')
    }
  })
})

describe('ConsoleShell — 既有页零改动地落在壳里', () => {
  it('四组标签 + 15 个真链接；总览页（既有页）照常渲染在主区', async () => {
    const view = await mountShell('/admin')
    const text = document.body.textContent ?? ''
    for (const label of ['运营', '建模', '治理', '系统']) {
      expect(text).toContain(label)
    }
    const links = document.querySelectorAll('a.admin-nav-item')
    expect(links).toHaveLength(15)
    for (const link of Array.from(links)) {
      expect(link.getAttribute('href')).toMatch(/^\/admin/)
    }
    // 主区容器类名保持不变：11 个既有页靠它布局。
    expect(document.querySelector('main.admin-main')).not.toBeNull()
    // 真页面渲染在壳里（OverviewView 的页头由 PageHeader 提供）。
    expect(document.querySelector('.admin-main .overview-page')).not.toBeNull()
    expect(view.find('.page-stub').exists()).toBe(false)
  })

  it('品牌标是回控制台首页的链接（/admin）', async () => {
    await mountShell('/admin/kb')
    const brand = document.querySelector('a.brand-mark')
    expect(brand).not.toBeNull()
    expect(brand?.getAttribute('href')).toBe('/admin')
  })

  it('K3：当前项 aria-current="page"，父项不做全亮', async () => {
    await mountShell('/admin')
    expect(navLink('/admin')?.getAttribute('aria-current')).toBe('page')
    expect(navLink('/admin/kb')?.getAttribute('aria-current')).toBeNull()

    await router.push('/admin/kb')
    await flushPromises()
    expect(navLink('/admin/kb')?.getAttribute('aria-current')).toBe('page')
    expect(navLink('/admin')?.getAttribute('aria-current')).toBeNull()
  })

  it('K1：跳过链接是首个可聚焦元素，主区可被它聚焦', async () => {
    const view = await mountShell('/admin')
    const focusables = view.element.querySelectorAll<HTMLElement>(
      'a[href], button, [tabindex]:not([tabindex="-1"])',
    )
    expect(focusables[0].classList.contains('skip-link')).toBe(true)
    expect(focusables[0].getAttribute('href')).toBe('#main-content')
    expect(document.querySelector('main.admin-main')?.id).toBe('main-content')
  })

  it('K6：折叠态图标项有可访问名；折叠按钮与搜索按钮有 aria-label', async () => {
    await mountShell('/admin')
    const ui = useUiStore()
    ui.sidebarOpen = false
    await flushPromises()
    expect(
      document.querySelector('.admin-sidebar')?.classList.contains('rail'),
    ).toBe(true)
    // 无角标项：可访问名 = 页名；图标是装饰（aria-hidden），没有可见文本。
    const audit = NAV_ITEMS.find((i) => i.path === '/admin/audit')!
    const link = navLink('/admin/audit')
    expect(link?.getAttribute('aria-label')).toBe(messages.zh[audit.labelKey])
    expect(link?.textContent?.trim()).toBe('')
    // 有角标项：角标并入可访问名（折叠态看不见数字，读屏要读得到）。
    expect(navLink('/admin/kb')?.getAttribute('aria-label')).toBe('知识库 (2)')
    expect(
      document.querySelector('.console-topbar button[aria-label]'),
    ).not.toBeNull()
  })
})

describe('ConsoleShell — 角标与健康点（有数据才显示）', () => {
  it('有数：待办 3 / 知识库 2 / 语义层 1 / 未授权 2；0 与降级的来源不显示', async () => {
    await mountShell('/admin')
    const badgeText = (href: string) =>
      navLink(href)?.querySelector('.admin-nav-badge')?.textContent?.trim()
    expect(badgeText('/admin')).toBe('3')
    expect(badgeText('/admin/kb')).toBe('2')
    expect(badgeText('/admin/semantic')).toBe('1')
    expect(badgeText('/admin/users')).toBe('2')
    expect(badgeText('/admin/governance')).toBe('3') // 跨源 pending 聚合
    // 降级来源（job_failed）不显示；0 个问题的数据源也不显示
    expect(badgeText('/admin/jobs')).toBeUndefined()
    expect(badgeText('/admin/datasources')).toBeUndefined()
    // 健康点来自同一 payload
    const health = document.querySelector('.topbar-health')
    expect(health?.classList.contains('is-ok')).toBe(true)
    expect(health?.textContent).toContain('正常')
  })

  it('取不到：角标与健康点全部隐藏（不把「没取到」画成 0）', async () => {
    mockFetch.mockRejectedValue(new Error('offline'))
    await mountShell('/admin')
    expect(document.querySelectorAll('.admin-nav-badge')).toHaveLength(0)
    expect(document.querySelector('.topbar-health')).toBeNull()
  })

  it('顶栏版本来自 package.json', async () => {
    await mountShell('/admin')
    expect(document.querySelector('.topbar-version')?.textContent?.trim()).toBe(
      `v${(await import('../package.json')).version}`,
    )
  })
})

describe('ConsoleShell — ⌘K（K5）', () => {
  async function openPalette() {
    await mountShell('/admin')
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'k', metaKey: true, bubbles: true }),
    )
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('.cp-input')
    expect(input).not.toBeNull()
    return input as HTMLInputElement
  }

  it('⌘K 打开、过滤、↑↓ 选择、Enter 跳转', async () => {
    const input = await openPalette()
    expect(document.activeElement).toBe(input)

    input.value = '知识'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    const options = document.querySelectorAll('[role="option"]')
    expect(options).toHaveLength(1)
    expect(options[0].textContent).toContain('知识库')

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    )
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/admin/kb')
  })

  it('↑↓ 在结果间移动；无匹配时给可读空态', async () => {
    const input = await openPalette()
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }),
    )
    await flushPromises()
    const options = document.querySelectorAll('[role="option"]')
    expect(options).toHaveLength(15)
    expect(options[1].getAttribute('aria-selected')).toBe('true')
    expect(input.getAttribute('aria-activedescendant')).toBe(options[1].id)

    input.value = '没有这个页面'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(0)
    expect(document.querySelector('.cp-empty')?.textContent).toContain('无匹配')
  })

  it('Esc 关闭并把焦点还给触发按钮', async () => {
    await mountShell('/admin')
    const trigger = document.querySelector<HTMLElement>('.topbar-search')!
    trigger.focus()
    trigger.click()
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('.cp-input')!
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    )
    await flushPromises()
    expect(document.querySelector('.cp-panel')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
})

describe('ConsoleShell — 全局态', () => {
  it('离线黄条 / 恢复绿条 / 可关闭', async () => {
    await mountShell('/admin')
    expect(document.querySelector('.global-banner')).toBeNull()

    window.dispatchEvent(new Event('offline'))
    await flushPromises()
    expect(
      document.querySelector('.global-banner.is-offline')?.textContent,
    ).toContain('网络已断开')

    document.querySelector<HTMLElement>('.gb-close')!.click()
    await flushPromises()
    expect(document.querySelector('.global-banner')).toBeNull()

    window.dispatchEvent(new Event('online'))
    await flushPromises()
    expect(
      document.querySelector('.global-banner.is-online')?.textContent,
    ).toContain('网络已恢复')
  })

  it('路由切换点亮顶栏下沿进度条（K8：reduced-motion 也有静态兜底）', async () => {
    await mountShell('/admin')
    const nav = router.push('/admin/ops')
    await flushPromises()
    expect(
      document
        .querySelector('.route-progress')
        ?.classList.contains('is-active'),
    ).toBe(true)
    await nav
    await flushPromises()
  })

  it('语言切换局部生效：无 reload，html lang 同步（D6/D21）', async () => {
    await mountShell('/admin')
    const ui = useUiStore()
    ui.setLang('en')
    await flushPromises()
    expect(document.documentElement.lang).toBe('en')
    expect(
      document.querySelector('a.admin-nav-item[href="/admin/kb"]')?.textContent,
    ).toContain('Knowledge base')
    ui.setLang('zh')
    await flushPromises()
    expect(document.documentElement.lang).toBe('zh')
  })
})
