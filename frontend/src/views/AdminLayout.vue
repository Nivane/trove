<template>
  <div class="admin-shell">
    <aside
      class="sidebar admin-sidebar"
      :class="{ rail: !ui.sidebarOpen }"
    >
      <template v-if="ui.sidebarOpen">
        <div class="brand">
          <span class="brand-mark"><BrandMark :size="24" /></span>
          <button
            class="topbar-btn sidebar-toggle-btn"
            :title="t('toggleSidebar', ui.lang)"
            @click="ui.toggleSidebar()"
          >
            <PanelLeftClose :size="16" />
          </button>
        </div>

        <nav class="admin-nav">
          <div class="admin-nav-section">
            <div class="admin-nav-label">
              {{ t('adminNavManage', ui.lang) }}
            </div>
            <button
              v-for="item in manageItems"
              :key="item.path"
              class="admin-nav-item"
              :class="{ active: isActive(item.path) }"
              @click="go(item.path)"
            >
              <component :is="item.icon" :size="17" class="admin-nav-icon" />
              <span>{{ t(item.label, ui.lang) }}</span>
            </button>
          </div>
          <div class="admin-nav-section">
            <div class="admin-nav-label">
              {{ t('adminNavSystem', ui.lang) }}
            </div>
            <button
              v-for="item in systemItems"
              :key="item.path"
              class="admin-nav-item"
              :class="{ active: isActive(item.path) }"
              @click="go(item.path)"
            >
              <component :is="item.icon" :size="17" class="admin-nav-icon" />
              <span class="admin-nav-text">{{ t(item.label, ui.lang) }}</span>
              <span
                v-if="item.path === '/admin/governance' && govBadge"
                class="admin-nav-badge"
                :title="t('govKpiTotal', ui.lang)"
              >{{ govBadge }}</span>
            </button>
          </div>
        </nav>

        <div class="sidebar-profile">
          <el-dropdown
            trigger="click"
            popper-class="profile-popper"
            @command="onProfileCmd"
          >
            <button class="profile-btn">
              <span class="profile-avatar">{{ avatarChar }}</span>
              <span class="profile-name">{{
                auth.user?.display_name || auth.user?.username || ''
              }}</span>
            </button>
            <template #dropdown>
              <div class="profile-head">
                <span class="profile-head-avatar">{{ avatarChar }}</span>
                <div class="profile-head-meta">
                  <div class="profile-head-name">
                    {{ auth.user?.display_name || auth.user?.username || '' }}
                  </div>
                  <div class="profile-head-sub">
                    {{ auth.user?.username || '' }}
                  </div>
                </div>
              </div>
              <el-dropdown-menu class="profile-menu">
                <el-dropdown-item command="lang">
                  <Languages :size="15" />
                  {{ t('langToggle', ui.lang) }}
                  <span class="profile-value">{{
                    ui.lang === 'zh' ? '中文' : 'English'
                  }}</span>
                </el-dropdown-item>
                <el-dropdown-item divided command="chat">
                  <MessageSquare :size="15" />
                  {{ t('goToChat', ui.lang) }}
                </el-dropdown-item>
                <el-dropdown-item command="logout">
                  <LogOut :size="15" />
                  {{ t('logout', ui.lang) }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </template>

      <template v-else>
        <button
          class="rail-btn"
          :title="t('toggleSidebar', ui.lang)"
          @click="ui.toggleSidebar()"
        >
          <PanelLeftOpen :size="16" />
        </button>
        <div class="rail-btns">
          <button
            v-for="item in allItems"
            :key="item.path"
            class="rail-btn"
            :class="{ active: isActive(item.path) }"
            :title="t(item.label, ui.lang)"
            @click="go(item.path)"
          >
            <component :is="item.icon" :size="16" />
          </button>
        </div>
        <div class="sidebar-profile">
          <el-dropdown
            trigger="click"
            popper-class="profile-popper"
            @command="onProfileCmd"
          >
            <button class="profile-btn profile-rail-btn" :title="auth.user?.username">
              <span class="profile-avatar">{{ avatarChar }}</span>
            </button>
            <template #dropdown>
              <div class="profile-head">
                <span class="profile-head-avatar">{{ avatarChar }}</span>
                <div class="profile-head-meta">
                  <div class="profile-head-name">
                    {{ auth.user?.display_name || auth.user?.username || '' }}
                  </div>
                  <div class="profile-head-sub">
                    {{ auth.user?.username || '' }}
                  </div>
                </div>
              </div>
              <el-dropdown-menu class="profile-menu">
                <el-dropdown-item command="lang">
                  <Languages :size="15" />
                  {{ t('langToggle', ui.lang) }}
                  <span class="profile-value">{{
                    ui.lang === 'zh' ? '中文' : 'English'
                  }}</span>
                </el-dropdown-item>
                <el-dropdown-item divided command="chat">
                  <MessageSquare :size="15" />
                  {{ t('goToChat', ui.lang) }}
                </el-dropdown-item>
                <el-dropdown-item command="logout">
                  <LogOut :size="15" />
                  {{ t('logout', ui.lang) }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </template>
    </aside>
    <main class="admin-main">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { Component } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { useRouter, useRoute } from 'vue-router'
import {
  LayoutDashboard,
  Users,
  Database,
  Library,
  Layers3,
  ScrollText,
  SlidersHorizontal,
  Cpu,
  History,
  Clock,
  Gavel,
  BookOpenCheck,
  Activity,
  ShieldCheck,
  PanelLeftClose,
  PanelLeftOpen,
  Languages,
  MessageSquare,
  LogOut,
} from 'lucide-vue-next'
import BrandMark from '../components/brand/BrandMark.vue'
import { fetchOverview } from '../api/overview'
import { t } from '../i18n'

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()
const route = useRoute()

const manageItems: {
  path: string
  label: keyof typeof import('../i18n').messages['zh']
  icon: Component
}[] = [
  { path: '/admin', label: 'ovTitle', icon: LayoutDashboard },
  { path: '/admin/users', label: 'users', icon: Users },
  { path: '/admin/datasources', label: 'datasources', icon: Database },
  { path: '/admin/kb', label: 'kb', icon: Library },
  { path: '/admin/semantic', label: 'semanticLayer', icon: Layers3 },
  { path: '/admin/jobs', label: 'jobs', icon: Clock },
  { path: '/admin/decisions', label: 'decisions', icon: Gavel },
  { path: '/admin/skills', label: 'skills', icon: BookOpenCheck },
]

const systemItems: {
  path: string
  label: keyof typeof import('../i18n').messages['zh']
  icon: Component
}[] = [
  { path: '/admin/governance', label: 'govTitle', icon: ShieldCheck },
  { path: '/admin/ops', label: 'ops', icon: Activity },
  { path: '/admin/model-config', label: 'modelConfig', icon: Cpu },
  { path: '/admin/audit', label: 'audit', icon: ScrollText },
  { path: '/admin/checkpoints', label: 'checkpoints', icon: History },
  { path: '/admin/settings', label: 'systemSettings', icon: SlidersHorizontal },
]

/* 治理待办徽标:合计 > 0 才出现(0 与取不到都不显示 —— 空徽标没有信息,
   而把「没取到」画成 0 是这一屏最不该犯的错,§6-A/B)。不精确时 ≥ N。 */
const govBadge = ref<string | null>(null)

async function loadGovBadge() {
  try {
    const payload = await fetchOverview('24h')
    const todos = payload.todos
    if (!todos || todos.total <= 0) {
      govBadge.value = null
      return
    }
    govBadge.value = todos.count_exact ? String(todos.total) : `≥ ${todos.total}`
  } catch {
    govBadge.value = null
  }
}

onMounted(loadGovBadge)

const allItems = computed(() => [...manageItems, ...systemItems])

function isActive(path: string): boolean {
  return route.path === path
}

function go(path: string) {
  if (route.path !== path) router.push(path)
}

const avatarChar = computed(() => {
  const name = auth.user?.display_name || auth.user?.username || ''
  return (name.trim()[0] || '?').toUpperCase()
})

async function onProfileCmd(cmd: string) {
  if (cmd === 'lang') {
    ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
    window.location.reload()
  } else if (cmd === 'chat') {
    await router.push('/')
  } else if (cmd === 'logout') {
    await auth.logout()
    await router.push({ name: 'login' })
  }
}
</script>

<style scoped>
/* 治理待办徽标(仅治理中心一行渲染;0 / 取不到时不出现)。 */
.admin-nav-badge {
  margin-left: auto;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  font-variant-numeric: tabular-nums;
  line-height: 16px;
}
.admin-nav-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>