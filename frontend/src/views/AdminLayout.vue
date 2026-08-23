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
              <span>{{ t(item.label, ui.lang) }}</span>
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
                <el-dropdown-item command="theme">
                  <component :is="ui.theme === 'dark' ? Sun : Moon" :size="15" />
                  {{ themeLabel }}
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
                <el-dropdown-item command="theme">
                  <component :is="ui.theme === 'dark' ? Sun : Moon" :size="15" />
                  {{ themeLabel }}
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
import { computed } from 'vue'
import type { Component } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { useRouter, useRoute } from 'vue-router'
import {
  Users,
  Database,
  Library,
  ScrollText,
  SlidersHorizontal,
  Cpu,
  PanelLeftClose,
  PanelLeftOpen,
  Languages,
  Sun,
  Moon,
  MessageSquare,
  LogOut,
} from 'lucide-vue-next'
import BrandMark from '../components/brand/BrandMark.vue'
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
  { path: '/admin/users', label: 'users', icon: Users },
  { path: '/admin/datasources', label: 'datasources', icon: Database },
  { path: '/admin/kb', label: 'kb', icon: Library },
]

const systemItems: {
  path: string
  label: keyof typeof import('../i18n').messages['zh']
  icon: Component
}[] = [
  { path: '/admin/model-config', label: 'modelConfig', icon: Cpu },
  { path: '/admin/audit', label: 'audit', icon: ScrollText },
  { path: '/admin/settings', label: 'systemSettings', icon: SlidersHorizontal },
]

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

const themeLabel = computed(() =>
  ui.theme === 'dark' ? t('themeLight', ui.lang) : t('themeDark', ui.lang),
)

async function onProfileCmd(cmd: string) {
  if (cmd === 'lang') {
    ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
    window.location.reload()
  } else if (cmd === 'theme') {
    ui.cycleTheme()
  } else if (cmd === 'chat') {
    await router.push('/')
  } else if (cmd === 'logout') {
    await auth.logout()
    await router.push({ name: 'login' })
  }
}
</script>