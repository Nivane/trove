import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// Dev runs at /ui/ (the original mount), prod nginx serves at / — accept both.
const history = createWebHistory(
  window.location.pathname.startsWith('/ui') ? '/ui/' : '/',
)

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
    {
      path: '/admin',
      component: () => import('../views/AdminLayout.vue'),
      meta: { requiresAdmin: true },
      children: [
        { path: '', redirect: '/admin/users' },
        {
          path: 'users',
          name: 'admin-users',
          component: () => import('../views/admin/UsersView.vue'),
        },
        {
          path: 'kb',
          name: 'admin-kb',
          component: () => import('../views/admin/KbLessonsView.vue'),
        },
        {
          path: 'audit',
          name: 'admin-audit',
          component: () => import('../views/admin/AuditView.vue'),
        },
        {
          path: 'datasources',
          name: 'admin-datasources',
          component: () => import('../views/admin/DatasourcesView.vue'),
        },
      ],
    },
  ],
})

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
  if (to.path.startsWith('/admin') && auth.user?.role !== 'admin') {
    // Regular users are kept out of the console — frontend guard + every
    // /v1/admin/* route enforces the same rule server-side (403).
    return { name: 'chat' }
  }
  return true
})
