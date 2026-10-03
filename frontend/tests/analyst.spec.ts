/**
 * W5 阶段二 —— analyst 只读面（设计稿 §2.2 R1/R3）。
 *
 * 钉住「三处一致」的前两处（后端守卫的第三处在 tests/api/test_analyst_readonly.py）：
 *   1. navModel.visibleFor ↔ 路由 meta.roles 对**每一项**都一致；
 *   2. analyst 可见的六项恰好是运营组 4 页 + 治理中心 / 审计日志，
 *      且每一项的端点在 analyst 下不 403（那只在后端测试里验证）；
 *   3. 只读判定（useReadOnly）与站内可达判定（canOpen）用的是同一套
 *      路由契约 —— 不可达就不渲染链接，不留「点了被弹回」的死链。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  ANALYST_READ_ONLY,
  NAV_ITEMS,
  canEnterConsole,
  navItemsFor,
  visibleFor,
} from '../src/components/layout/navModel'
import { useAuthStore, type Role } from '../src/stores/auth'
import { useReadOnly } from '../src/composables/useReadOnly'
import { canAccess, requiredRoles, router as appRouter } from '../src/router'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(async () => ({})),
  apiPost: vi.fn(async () => ({})),
  apiPatch: vi.fn(async () => ({})),
  apiPut: vi.fn(async () => ({})),
  apiDelete: vi.fn(async () => ({})),
}))

function setRole(role: Role | null) {
  const auth = useAuthStore()
  auth.$patch({
    token: role ? 't' : '',
    user: role
      ? { id: 1, username: 'u', role, display_name: 'U' }
      : null,
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
})

describe('navModel ↔ 路由 meta.roles 契约（R1 三处一致之一）', () => {
  it('每一项：路由要求角色 == navModel.visibleFor(item)', () => {
    for (const item of NAV_ITEMS) {
      const resolved = appRouter.resolve(item.path)
      expect(resolved.matched.length, item.path).toBeGreaterThan(0)
      expect(requiredRoles(resolved), item.path).toEqual(
        [...visibleFor(item)].sort(),
      )
    }
  })

  it('analyst 可见的恰好是六项只读页（运营 4 + 治理 2）', () => {
    expect(ANALYST_READ_ONLY).toBe(true)
    expect(navItemsFor('analyst').map((i) => i.path).sort()).toEqual(
      [
        '/admin',
        '/admin/audit',
        '/admin/checkpoints',
        '/admin/governance',
        '/admin/jobs',
        '/admin/ops',
      ].sort(),
    )
  })

  it('analyst 可见项的 route 都放行 analyst；建模/用户/系统组不放行', () => {
    for (const item of navItemsFor('analyst')) {
      expect(
        canAccess('analyst', requiredRoles(appRouter.resolve(item.path))),
        item.path,
      ).toBe(true)
    }
    for (const path of [
      '/admin/datasources',
      '/admin/kb',
      '/admin/semantic',
      '/admin/skills',
      '/admin/decisions',
      '/admin/actions',
      '/admin/users',
      '/admin/model-config',
      '/admin/settings',
      '/admin/styleguide',
    ]) {
      expect(canAccess('analyst', requiredRoles(appRouter.resolve(path))), path).toBe(
        false,
      )
    }
  })

  it('canEnterConsole：admin/analyst 有入口，user/未登录没有', () => {
    expect(canEnterConsole('admin')).toBe(true)
    expect(canEnterConsole('analyst')).toBe(true)
    expect(canEnterConsole('user')).toBe(false)
    expect(canEnterConsole(undefined)).toBe(false)
  })
})

describe('useReadOnly — 只读判定与站内可达判定', () => {
  it('admin 可写；analyst 只读；user 也按只读处理（方向安全）', () => {
    setRole('admin')
    const asAdmin = useReadOnly()
    expect(asAdmin.readOnly.value).toBe(false)

    setRole('analyst')
    const asAnalyst = useReadOnly()
    expect(asAnalyst.readOnly.value).toBe(true)

    setRole('user')
    const asUser = useReadOnly()
    expect(asUser.readOnly.value).toBe(true)
  })

  it('canOpen 与路由守卫同判：analyst 开不了建模页，admin 全开', () => {
    setRole('analyst')
    const { canOpen } = useReadOnly()
    expect(canOpen('/admin/jobs')).toBe(true)
    expect(canOpen('/admin/governance?tab=drift')).toBe(true)
    expect(canOpen('/admin/kb?tab=lessons')).toBe(false)
    expect(canOpen('/admin/datasources')).toBe(false)
    expect(canOpen('/')).toBe(true)

    setRole('admin')
    const asAdmin = useReadOnly()
    expect(asAdmin.canOpen('/admin/kb?tab=lessons')).toBe(true)
    expect(asAdmin.canOpen('/admin/datasources')).toBe(true)
  })
})
