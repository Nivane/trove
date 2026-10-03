import {
  createRouter,
  createWebHistory,
  type RouteLocationNormalized,
} from 'vue-router'
import { useAuthStore, type Role } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { t } from '../i18n'
import { notifyInfo } from '../utils/notify'

// Dev runs at /ui/ (the original mount), prod nginx serves at / — accept both.
const history = createWebHistory(
  window.location.pathname.startsWith('/ui') ? '/ui/' : '/',
)

declare module 'vue-router' {
  interface RouteMeta {
    /**
     * 该路由要求的角色（P6 §4.2）。由最内层声明生效；/admin 子树在父路由
     * 声明 ['admin']，子路由继承 —— 未声明的路由默认拒绝（见 requiredRoles）。
     */
    roles?: Role[]
    /** 顶栏窄屏当前页名的 i18n 键（面包屑唯一来源仍是 PageHeader）。 */
    titleKey?: keyof typeof import('../i18n').messages['zh']
  }
}

/** 管理台路由的角色常量 —— W5 阶段二逐条放开时只改这里/子路由声明。 */
const ADMIN_ONLY: Role[] = ['admin']

export const router = createRouter({
  history,
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
    },
    {
      path: '/',
      name: 'chat',
      component: () => import('../views/ChatView.vue'),
    },
    // 用户面：我的订阅（定时报告）。登录即可，无角色门槛 —— 后端 own-only。
    {
      path: '/subscriptions',
      name: 'subscriptions',
      component: () => import('../views/SubscriptionsView.vue'),
    },
    {
      path: '/admin',
      component: () => import('../views/AdminLayout.vue'),
      // 默认拒绝：子路由未声明自己的 roles 时继承这一条（D9/D11）。
      meta: { roles: ADMIN_ONLY },
      children: [
        {
          path: '',
          name: 'admin-overview',
          component: () => import('../views/admin/OverviewView.vue'),
          meta: { titleKey: 'ovTitle' },
        },
        // Compat: /admin/overview is the same page — keep query/hash so old
        // links (e.g. ?win=7d#todos) land on the state they described.
        {
          path: 'overview',
          redirect: (to) => ({ path: '/admin', query: to.query, hash: to.hash }),
        },
        {
          path: 'users',
          name: 'admin-users',
          component: () => import('../views/admin/UsersView.vue'),
          meta: { titleKey: 'navUsersPermissions' },
        },
        {
          path: 'kb',
          name: 'admin-kb',
          component: () => import('../views/admin/KbView.vue'),
          meta: { titleKey: 'kb' },
        },
        {
          path: 'semantic',
          name: 'admin-semantic',
          component: () => import('../views/admin/SemanticView.vue'),
          meta: { titleKey: 'semanticLayer' },
        },
        {
          path: 'audit',
          name: 'admin-audit',
          component: () => import('../views/admin/AuditView.vue'),
          meta: { titleKey: 'audit' },
        },
        {
          path: 'checkpoints',
          name: 'admin-checkpoints',
          component: () => import('../views/admin/CheckpointsView.vue'),
          meta: { titleKey: 'checkpoints' },
        },
        {
          path: 'datasources',
          name: 'admin-datasources',
          component: () => import('../views/admin/DatasourcesView.vue'),
          meta: { titleKey: 'datasources' },
        },
        {
          path: 'model-config',
          name: 'admin-model-config',
          component: () => import('../views/admin/ModelConfigView.vue'),
          meta: { titleKey: 'modelConfig' },
        },
        {
          path: 'settings',
          name: 'admin-settings',
          component: () => import('../views/admin/SettingsView.vue'),
          meta: { titleKey: 'systemSettings' },
        },
        {
          path: 'jobs',
          name: 'admin-jobs',
          component: () => import('../views/admin/JobsView.vue'),
          meta: { titleKey: 'jobs' },
        },
        {
          path: 'decisions',
          name: 'admin-decisions',
          component: () => import('../views/admin/DecisionsView.vue'),
          meta: { titleKey: 'decisions' },
        },
        {
          path: 'actions',
          name: 'admin-actions',
          component: () => import('../views/admin/ActionsView.vue'),
          meta: { titleKey: 'actionsPage' },
        },
        {
          path: 'skills',
          name: 'admin-skills',
          component: () => import('../views/admin/SkillsView.vue'),
          meta: { titleKey: 'skills' },
        },
        // 治理中心(P5):收件箱 / 覆盖体检 / 漂移与版本 / 血缘地图,四个 Tab 同页。
        {
          path: 'governance',
          name: 'admin-governance',
          component: () => import('../views/admin/GovernanceView.vue'),
          meta: { titleKey: 'govTitle' },
        },
        // 质量与成本运营台(P4):quality / usage 两个 Tab 同页。
        {
          path: 'ops',
          name: 'admin-ops',
          component: () => import('../views/admin/OpsView.vue'),
          meta: { titleKey: 'ops' },
        },
        // Compat: /admin/usage 是同一页的成本 Tab —— 保留 query(窗口/筛选)。
        {
          path: 'usage',
          redirect: (to) => ({
            path: '/admin/ops',
            query: { ...to.query, tab: 'usage' },
          }),
        },
        // 设计系统样式指南（P7-W4）：基础六件 × 状态矩阵的活样板间。
        // 只按 URL 直达（/admin/styleguide），不进侧栏 —— W0 冻结的四组
        // 15 项 IA 保持不变。
        {
          path: 'styleguide',
          name: 'admin-styleguide',
          component: () => import('../views/admin/StyleguideView.vue'),
          meta: { titleKey: 'styleguideTitle' },
        },
        // 壳内 404：/admin/* 的未知路径渲染在壳里（面包屑 + 回总览），
        // 必须放在子路由表最后。
        {
          path: ':pathMatch(.*)*',
          name: 'admin-not-found',
          component: () => import('../components/layout/NotFoundView.vue'),
          meta: { titleKey: 'notFoundTitle' },
        },
      ],
    },
    // 非 /admin 的未知路径：用户端 404（D12），放在路由表最后。
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('../components/layout/NotFoundView.vue'),
    },
  ],
})

/**
 * 路由要求的角色（P6 §4.2）：取 matched 里最内层的声明；未声明且落在
 * /admin 下 → ['admin']（默认拒绝，防新页忘声明就默认放开）。
 * 返回 null = 该路由不要求角色。
 */
export function requiredRoles(
  to: Pick<RouteLocationNormalized, 'matched' | 'path'>,
): Role[] | null {
  for (let i = to.matched.length - 1; i >= 0; i -= 1) {
    const roles = to.matched[i].meta?.roles
    if (roles && roles.length) return [...roles]
  }
  if (to.path === '/admin' || to.path.startsWith('/admin/')) return [...ADMIN_ONLY]
  return null
}

/** 角色是否放行；required 为 null/空 = 不设门槛。 */
export function canAccess(role: string | undefined, required: Role[] | null): boolean {
  if (!required || required.length === 0) return true
  return !!role && required.includes(role as Role)
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // On first load with a stored token, restore the session before guarding.
  // bootstrap() is cached (bootPromise), so this is safe when App.vue's
  // setup already started the restore — awaiting an in-flight restore is
  // the point; skipping it would decide auth on stale state (login loop
  // after every reload/lang switch).
  if (!auth.user && auth.token) {
    await auth.bootstrap()
  }
  if (to.name !== 'login' && !auth.isAuthed) {
    return {
      name: 'login',
      query: to.fullPath !== '/' ? { next: to.fullPath } : {},
    }
  }
  if (to.name === 'login' && auth.isAuthed) {
    return { name: 'chat' }
  }
  const required = requiredRoles(to)
  if (!canAccess(auth.user?.role, required)) {
    // 越权重定向不再静默（P6 §2.2 R2 / §6.3）：给一次提示再回对话页。
    // 前端只是体验层；每个 /v1/admin/* 端点后端仍按同一规则 403。
    notifyInfo(t('adminDeniedHint', useUiStore().lang))
    return { name: 'chat' }
  }
  return true
})
