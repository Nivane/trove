<template>
  <aside class="sidebar" :class="{ open: ui.sidebarOpen }">
    <div class="brand">
      <span class="brand-mark">◆</span>
      <span class="brand-name">Trove</span>
    </div>
    <button class="new-session-btn" @click="newSession">{{ t('newSession', ui.lang) }}</button>
    <div class="sidebar-section-label">{{ t('tasks', ui.lang) }}</div>
    <nav class="session-list">
      <div
        v-for="s in chat.sessions"
        :key="s.session_id"
        class="session-item"
        :class="{ active: s.session_id === chat.sessionId }"
        @click="select(s.session_id)"
      >
        <span class="session-title">{{ title(s) }}</span>
        <span class="session-del" @click.stop="remove(s.session_id)">×</span>
      </div>
      <div v-if="!chat.sessions.length" class="session-empty">—</div>
    </nav>
    <div class="sidebar-footer">
      <router-link v-if="auth.isAdmin" class="admin-link" to="/admin">
        {{ t('admin', ui.lang) }}
      </router-link>
      <button class="logout-btn" @click="logout">{{ t('logout', ui.lang) }}</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { useAuthStore } from '../../stores/auth'
import { t } from '../../i18n'
import { trunc } from '../../utils/format'

const chat = useChatStore()
const ui = useUiStore()
const auth = useAuthStore()

onMounted(() => chat.listSessions())

async function newSession() {
  await chat.createSession()
}

function select(sid: string) {
  if (sid !== chat.sessionId) chat.loadSession(sid)
}

async function remove(sid: string) {
  await chat.deleteSession(sid)
}

function title(s: { session_id: string }): string {
  return trunc(s.session_id.slice(0, 8), 16)
}

async function logout() {
  await auth.logout()
}
</script>
