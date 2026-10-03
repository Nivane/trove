/**
 * navModel — 控制台信息架构（IA）的单一来源（设计稿 P6 §4.1）。
 *
 * 四组 16 项：`group` 的声明顺序即渲染顺序；侧栏、⌘K 面板与路由契约
 * 测试读的都是这一份。这是个零渲染、零请求的纯数据 + 纯函数模块：
 * 角标只声明「来源键」（badgeKey），取值在 useNavBadges 里完成，
 * 取不到就不显示 —— 绝不在导航层编造数字。
 *
 * 分组依据是用户的工作视角，不是后端的模块划分（设计稿 §2.2）：
 *   ops        平台现在怎么样、跑得怎么样
 *   modeling   agent 能答什么、依据什么口径
 *   governance 谁改的、谁被允许、有没有问题
 *   system     平台自己怎么配
 */
import type { Component } from 'vue'
import {
  Activity,
  BookOpenCheck,
  Clock,
  Cpu,
  Database,
  Gavel,
  History,
  LayoutDashboard,
  Layers3,
  Library,
  Palette,
  ScrollText,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Users,
} from 'lucide-vue-next'
import { messages } from '../../i18n'

export type Role = 'admin' | 'analyst' | 'user'
export type NavGroupKey = 'ops' | 'modeling' | 'governance' | 'system'
export type MessageKey = keyof typeof messages.zh

/** 角标来源键 —— 每个键都必须在 /v1/admin/overview 的既有字段里有对应计数。 */
export type BadgeKey =
  | 'todos'
  | 'govPending'
  | 'failedJobs'
  | 'dsIssues'
  | 'kbPending'
  | 'semPending'
  | 'skillDrafts'
  | 'nogrant'

export interface NavItem {
  key: string
  path: string
  labelKey: MessageKey
  icon: Component
  group: NavGroupKey
  /** 角标槽位：有数据且 > 0 才渲染（0 与取不到都不显示）。 */
  badgeKey?: BadgeKey
  /** 允许看见该项的角色；缺省 = ['admin']（默认拒绝，与路由守卫同一条规则）。 */
  visibleFor?: Role[]
}

export interface NavGroup {
  key: NavGroupKey
  labelKey: MessageKey
}

/** 「组件规范」页（设计稿 §5 W4：views/admin/StyleguideView.vue）。
 * W4 已建页，但按 W4 交付的定位它只按
 * URL 直达、不进侧栏 —— W0 冻结的 IA 项数保持不变，故此项永远被过滤。
 * 留在这里是为了让路径仍受 navModel 契约测试覆盖（路由存在、角色对齐）。
 */
export const STYLEGUIDE_READY = false

/**
 * W5 阶段二开关（设计稿 §2.2 R3）：批三已置 true —— 后端
 * require_admin_or_analyst 逐条只读面就绪，analyst 可见**声明了
 * visibleFor 的**只读页。此开关是整层 kill-switch：置 false 即让
 * analyst 对管理台整体不可见（visibleFor 声明原样保留，不逐个回改）。
 */
export const ANALYST_READ_ONLY = true

/**
 * W5 阶段二 analyst 只读面（设计稿 §2.2 R3）：运营组 4 页 + 治理中心 /
 * 审计日志。与路由 meta.roles、后端 require_admin_or_analyst 的逐条清单
 * 三处一致 —— 契约测试 tests/analyst.spec.ts 钉住这份对齐。
 * 建模组（数据源/KB/语义/Skills/判定/行动）与用户、系统组保持 admin 专属。
 */
const ADMIN_ANALYST: Role[] = ['admin', 'analyst']

/** 分组顺序即渲染顺序。 */
export const NAV_GROUPS: NavGroup[] = [
  { key: 'ops', labelKey: 'navGroupOps' },
  { key: 'modeling', labelKey: 'navGroupModeling' },
  { key: 'governance', labelKey: 'navGroupGovernance' },
  { key: 'system', labelKey: 'navGroupSystem' },
]

export const NAV_ITEMS: NavItem[] = [
  // ── 运营 ──
  {
    key: 'overview',
    path: '/admin',
    labelKey: 'ovTitle',
    icon: LayoutDashboard,
    group: 'ops',
    badgeKey: 'todos',
    visibleFor: ADMIN_ANALYST,
  },
  {
    key: 'ops',
    path: '/admin/ops',
    labelKey: 'ops',
    icon: Activity,
    group: 'ops',
    badgeKey: 'failedJobs',
    visibleFor: ADMIN_ANALYST,
  },
  {
    key: 'jobs',
    path: '/admin/jobs',
    labelKey: 'jobs',
    icon: Clock,
    group: 'ops',
    badgeKey: 'failedJobs',
    visibleFor: ADMIN_ANALYST,
  },
  {
    key: 'checkpoints',
    path: '/admin/checkpoints',
    labelKey: 'checkpoints',
    icon: History,
    group: 'ops',
    visibleFor: ADMIN_ANALYST,
  },
  // ── 建模 ──
  {
    key: 'datasources',
    path: '/admin/datasources',
    labelKey: 'datasources',
    icon: Database,
    group: 'modeling',
    badgeKey: 'dsIssues',
  },
  {
    key: 'kb',
    path: '/admin/kb',
    labelKey: 'kb',
    icon: Library,
    group: 'modeling',
    badgeKey: 'kbPending',
  },
  {
    key: 'semantic',
    path: '/admin/semantic',
    labelKey: 'semanticLayer',
    icon: Layers3,
    group: 'modeling',
    badgeKey: 'semPending',
  },
  {
    key: 'skills',
    path: '/admin/skills',
    labelKey: 'skills',
    icon: BookOpenCheck,
    group: 'modeling',
    badgeKey: 'skillDrafts',
  },
  {
    key: 'decisions',
    path: '/admin/decisions',
    labelKey: 'decisions',
    icon: Gavel,
    group: 'modeling',
  },
  {
    key: 'actions',
    path: '/admin/actions',
    labelKey: 'actionsPage',
    icon: Send,
    group: 'modeling',
  },
  // ── 治理 ──
  {
    key: 'governance',
    path: '/admin/governance',
    labelKey: 'govTitle',
    icon: ShieldCheck,
    group: 'governance',
    badgeKey: 'govPending',
    visibleFor: ADMIN_ANALYST,
  },
  {
    key: 'audit',
    path: '/admin/audit',
    labelKey: 'audit',
    icon: ScrollText,
    group: 'governance',
    visibleFor: ADMIN_ANALYST,
  },
  {
    key: 'users',
    path: '/admin/users',
    labelKey: 'navUsersPermissions',
    icon: Users,
    group: 'governance',
    badgeKey: 'nogrant',
  },
  // ── 系统 ──
  {
    key: 'model-config',
    path: '/admin/model-config',
    labelKey: 'modelConfig',
    icon: Cpu,
    group: 'system',
  },
  {
    key: 'settings',
    path: '/admin/settings',
    labelKey: 'systemSettings',
    icon: SlidersHorizontal,
    group: 'system',
  },
  {
    key: 'styleguide',
    path: '/admin/styleguide',
    labelKey: 'navStyleGuide',
    icon: Palette,
    group: 'system',
  },
]

/** 角标槽位的可访问名（title / aria-label 用）。 */
export const BADGE_TITLE_KEYS: Record<BadgeKey, MessageKey> = {
  todos: 'badgeTodos',
  govPending: 'badgeGovPending',
  failedJobs: 'badgeFailedJobs',
  dsIssues: 'badgeDsIssues',
  kbPending: 'badgeKbPending',
  semPending: 'badgeSemPending',
  skillDrafts: 'badgeSkillDrafts',
  nogrant: 'badgeNogrant',
}

/** 声明可视角色；未声明 = 仅 admin（默认拒绝）。 */
export function visibleFor(item: NavItem): Role[] {
  return item.visibleFor ?? ['admin']
}

/** 角色可见性：与路由 meta.roles、后端守卫三处一致（设计稿 §2.2 R1）。 */
export function canSee(item: NavItem, role: string | undefined): boolean {
  return !!role && visibleFor(item).includes(role as Role)
}

/** 当前角色可见的项（保持声明顺序，含 W4 前的显式过滤）。 */
export function navItemsFor(role: string | undefined): NavItem[] {
  return NAV_ITEMS.filter(
    (item) =>
      canSee(item, role) &&
      (item.key !== 'styleguide' || STYLEGUIDE_READY) &&
      // W5 阶段二 kill-switch：ANALYST_READ_ONLY 关闭时，analyst 连
      // 声明过 visibleFor 的只读面也不可见（后端对应守卫另在 deps.py）。
      (role === 'admin' || ANALYST_READ_ONLY),
  )
}

/**
 * 该角色是否有任一管理台入口（侧栏「管理台」下拉项用）。
 * 单一判定来源 = navItemsFor —— 入口可见 ⟺ 至少一个导航项可见。
 */
export function canEnterConsole(role: string | undefined): boolean {
  return navItemsFor(role).length > 0
}

/** 按分组归拢（供侧栏与 ⌘K 渲染）；空组不返回。 */
export function navGroupsFor(
  role: string | undefined,
): { group: NavGroup; items: NavItem[] }[] {
  const items = navItemsFor(role)
  return NAV_GROUPS.map((group) => ({
    group,
    items: items.filter((item) => item.group === group.key),
  })).filter((entry) => entry.items.length > 0)
}

/**
 * 前缀匹配高亮（K3）：子路由保父项高亮；/admin 精确匹配，
 * 否则「总览」会在每个管理页上全亮。
 */
export function isPathActive(current: string, target: string): boolean {
  if (target === '/admin') return current === '/admin'
  return current === target || current.startsWith(target + '/')
}

/**
 * ⌘K 过滤：中英文 label 均可匹配（大小写不敏感），路径与键名兜底。
 * 空查询返回当前角色可见的全部项。
 */
export function filterNavItems(
  query: string,
  role: string | undefined,
): NavItem[] {
  const items = navItemsFor(role)
  const q = query.trim().toLowerCase()
  if (!q) return items
  return items.filter((item) => {
    const zh = String(messages.zh[item.labelKey] ?? '').toLowerCase()
    const en = String(messages.en[item.labelKey] ?? '').toLowerCase()
    return (
      zh.includes(q) ||
      en.includes(q) ||
      item.path.toLowerCase().includes(q) ||
      item.key.toLowerCase().includes(q)
    )
  })
}
