import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

export const router = createRouter({
  history: createWebHistory('/ui/'),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/', name: 'chat', component: () => import('../views/ChatView.vue') },
    {
      path: '/admin',
      component: () => import('../views/AdminLayout.vue'),
      children: [
        { path: '', redirect: '/admin/users' },
        { path: 'users', name: 'admin-users', component: () => import('../views/admin/UsersView.vue') },
        { path: 'kb', name: 'admin-kb', component: () => import('../views/admin/KbLessonsView.vue') },
        { path: 'audit', name: 'admin-audit', component: () => import('../views/admin/AuditView.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isAuthed) {
    return { name: 'login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  }
  if (to.name === 'login' && auth.isAuthed) {
    return { name: 'chat' }
  }
  if (to.path.startsWith('/admin') && auth.user?.role !== 'admin') {
    return { name: 'chat' }
  }
  return true
})
