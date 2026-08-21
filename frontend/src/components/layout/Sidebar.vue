<template>
  <aside
    class="sidebar"
    :class="{ rail: !ui.sidebarOpen }"
    :style="ui.sidebarOpen ? { width: ui.sidebarWidth + 'px' } : undefined"
  >
    <template v-if="ui.sidebarOpen">
      <div class="brand">
        <span class="brand-mark"><el-icon :size="16"><DataAnalysis /></el-icon></span>
        <span class="brand-name">Trove</span>
      </div>
      <button class="new-session-btn" @click="newSession">
        <el-icon :size="14"><Plus /></el-icon>
        {{ t('newSession', ui.lang) }}
      </button>

      <template v-for="group in groups" :key="group.label">
        <div class="sidebar-section-label">{{ group.label }}</div>
        <nav class="session-list">
          <div
            v-for="s in group.items"
            :key="s.session_id"
            class="session-item"
            :class="{ active: s.session_id === chat.sessionId }"
            @click="select(s.session_id)"
          >
            <span class="session-title">{{ title(s) }}</span>
            <span class="session-del" @click.stop="remove(s.session_id)"><el-icon :size="12"><Close /></el-icon></span>
          </div>
        </nav>
      </template>

      <div v-if="chat.sessionsLoading && !chat.sessions.length" class="session-list">
        <div v-for="n in 4" :key="n" class="session-skel"><div class="skeleton-line" /></div>
      </div>
      <div v-else-if="!chat.sessions.length" class="sidebar-empty">
        <span class="empty-note">{{ t('sessionEmpty', ui.lang) }}</span>
      </div>

      <div class="sidebar-resizer" @pointerdown="startResize" />
    </template>

    <template v-else>
      <div class="rail-btns">
        <button class="rail-btn" :title="t('newSession', ui.lang)" @click="newSession">
          <el-icon :size="16"><Plus /></el-icon>
        </button>
        <button class="rail-btn" :title="t('historyTitle', ui.lang)" @click="historyOpen = !historyOpen">
          <el-icon :size="16"><Clock /></el-icon>
        </button>
      </div>
      <div v-if="historyOpen" class="history-popover" @click.stop>
        <div class="history-popover-head">
          <span class="history-title">{{ t('historyTitle', ui.lang) }}</span>
          <button class="topbar-btn" @click="newSession"><el-icon :size="14"><Plus /></el-icon></button>
        </div>
        <div
          v-for="s in chat.sessions"
          :key="s.session_id"
          class="session-item"
          :class="{ active: s.session_id === chat.sessionId }"
          @click="select(s.session_id)"
        >
          <span class="session-title">{{ title(s) }}</span>
        </div>
      </div>
    </template>
  </aside>
</template>

<script setup lang="ts">
import { onMounted, computed, ref } from 'vue'
import { Plus, Close, Clock, DataAnalysis } from '@element-plus/icons-vue'
import { useChatStore } from '../../stores/chat'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { trunc } from '../../utils/format'

interface SessionRow {
  session_id: string
  created_at?: string
  updated_at?: string
  message_count?: number
  title?: string
}

const chat = useChatStore()
const ui = useUiStore()
const historyOpen = ref(false)

onMounted(() => chat.listSessions())

const groups = computed(() => {
  const items = chat.sessions as SessionRow[]
  const buckets: { label: string; items: SessionRow[] }[] = [
    { label: t('today', ui.lang), items: [] },
    { label: t('yesterday', ui.lang), items: [] },
    { label: t('older', ui.lang), items: [] },
  ]
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = startOfToday - 86400000
  for (const s of items) {
    const ts = new Date(s.updated_at || s.created_at || 0).getTime()
    if (!ts) { buckets[2].items.push(s); continue }
    if (ts >= startOfToday) buckets[0].items.push(s)
    else if (ts >= startOfYesterday) buckets[1].items.push(s)
    else buckets[2].items.push(s)
  }
  return buckets.filter((b) => b.items.length)
})

function title(s: SessionRow): string {
  if (s.title && s.title.trim()) return trunc(s.title.trim(), 24)
  return trunc(s.session_id.slice(0, 8), 16)
}

async function newSession() {
  historyOpen.value = false
  await chat.createSession()
}

function select(sid: string) {
  historyOpen.value = false
  if (sid !== chat.sessionId) chat.loadSession(sid)
}

async function remove(sid: string) {
  await chat.deleteSession(sid)
}

function startResize(e: PointerEvent) {
  e.preventDefault()
  const startX = e.clientX
  const startW = ui.sidebarWidth
  const onMove = (ev: PointerEvent) => ui.setSidebarWidth(startW + ev.clientX - startX)
  const onUp = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}
</script>
