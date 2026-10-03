<template>
  <aside
    class="sidebar"
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
      <button class="new-session-btn" @click="newSession">
        <Pencil :size="14" stroke-width="2" />
        {{ t('newSession', ui.lang) }}
      </button>
      <button class="new-session-btn" @click="openSearch">
        <Search :size="14" stroke-width="2" />
        {{ t('query', ui.lang) }}
      </button>

      <div class="sidebar-sessions">
        <div class="sidebar-section-head">
          <span class="sidebar-section-label">{{ t('recent', ui.lang) }}</span>
        </div>
        <nav class="session-list" @scroll.passive="onScroll">
          <div
            v-for="s in chat.sessions"
            :key="s.session_id"
            class="session-item"
            :class="{ active: s.session_id === chat.sessionId }"
            @click="select(s.session_id)"
            @contextmenu.prevent="openMenu(s, $event)"
          >
            <template v-if="renamingId === s.session_id">
              <input
                ref="renameEl"
                v-model="renameDraft"
                class="session-rename-input"
                @keydown.enter.prevent="commitRename(s.session_id)"
                @keydown.esc="cancelRename()"
                @blur="commitRename(s.session_id)"
              >
            </template>
            <template v-else>
              <span v-if="s.pinned" class="session-pin" :title="t('pinSession', ui.lang)">
                <Pin :size="11" />
              </span>
              <span class="session-title">{{ title(s) }}</span>
            </template>
            <span class="session-more" @click.stop="openMenu(s, $event)"><MoreVertical :size="13" /></span>
            <span class="session-del" @click.stop="remove(s.session_id)"><X :size="12" /></span>
          </div>
          <div v-if="chat.sessionsLoading" class="session-more">
            <span class="session-more-spinner" />
          </div>
          <div
            v-else-if="!chat.sessionsHasMore && chat.sessions.length"
            class="session-more session-more-end"
          >
            {{ t('noMoreSessions', ui.lang) }}
          </div>
        </nav>
      </div>
      <div
        v-if="menu.open"
        ref="menuEl"
        class="session-menu"
        :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
        @pointerdown.stop
        @click.stop
      >
        <button class="session-menu-item" @click="menuPin()">
          <Pin :size="13" />
          {{ menuPinned ? t('unpinSession', ui.lang) : t('pinSession', ui.lang) }}
        </button>
        <button class="session-menu-item" @click="menuExport()">
          <Download :size="13" />
          {{ t('exportMarkdown', ui.lang) }}
        </button>
        <button class="session-menu-item" @click="menuExportReport()">
          <FileDown :size="13" />
          {{ t('exportHtmlReport', ui.lang) }}
        </button>
        <button class="session-menu-item" @click="menuRename()">
          <Pencil :size="13" />
          {{ t('rename', ui.lang) }}
        </button>
        <button class="session-menu-item danger" @click="menuDelete()">
          <Trash2 :size="13" />
          {{ t('delete', ui.lang) }}
        </button>
      </div>

      <div
        v-if="chat.sessionsLoading && !chat.sessions.length"
        class="session-list"
      >
        <div v-for="n in 4" :key="n" class="session-skel">
          <div class="skeleton-line" />
        </div>
      </div>
      <div v-else-if="!chat.sessions.length" class="sidebar-empty">
        <span class="empty-note">{{ t('sessionEmpty', ui.lang) }}</span>
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
      <button
        class="rail-btn"
        :title="t('newSession', ui.lang)"
        @click="newSession"
      >
        <Pencil :size="16" stroke-width="2" />
      </button>
      <button
        class="rail-btn"
        :title="t('query', ui.lang)"
        @click="openSearch"
      >
        <Search :size="16" stroke-width="2" />
      </button>
    </template>

    <div class="sidebar-profile">
      <el-dropdown
        trigger="click"
        popper-class="profile-popper"
        @command="onProfileCmd"
      >
        <button class="profile-btn">
          <span class="profile-avatar">{{ avatarChar }}</span>
          <span
            v-if="ui.sidebarOpen"
            class="profile-name"
          >{{
            auth.user?.display_name || auth.user?.username || ''
          }}</span>
        </button>
        <template #dropdown>
          <div class="profile-head">
            <span class="profile-head-avatar">{{ avatarChar }}</span>
              <div class="profile-head-meta">
              <div class="profile-head-name">{{
                auth.user?.display_name || auth.user?.username || ''
              }}</div>
              <div class="profile-head-sub">{{ auth.user?.username || '' }}</div>
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
            <el-dropdown-item command="subs">
              <Bell :size="15" />
              {{ t('subsMy', ui.lang) }}
            </el-dropdown-item>
            <el-dropdown-item v-if="auth.isAdmin" command="admin">
              <Settings :size="15" />
              {{ t('admin', ui.lang) }}
            </el-dropdown-item>
            <el-dropdown-item divided command="logout">
              <LogOut :size="15" />
              {{ t('logout', ui.lang) }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <teleport to="body">
      <transition name="fade">
        <div
          v-if="searchOpen"
          class="search-overlay"
          @pointerdown.self="closeSearch"
        >
          <div class="search-dialog" role="dialog" aria-modal="true">
            <div class="search-dialog-head">
              <Search :size="16" class="search-dialog-icon" />
              <input
                ref="searchInput"
                v-model="searchText"
                class="search-dialog-input"
                :placeholder="t('searchSessions', ui.lang)"
              >
              <button
                class="search-dialog-close"
                :title="t('close', ui.lang)"
                @click="closeSearch"
              >
                <X :size="15" />
              </button>
            </div>
            <div class="search-dialog-body">
              <div
                v-for="s in filteredSessions"
                :key="s.session_id"
                class="session-item"
                :class="{ active: s.session_id === chat.sessionId }"
                @click="selectFromSearch(s.session_id)"
              >
                <span class="session-title">{{ title(s) }}</span>
              </div>
              <div v-if="!filteredSessions.length" class="search-dialog-empty">
                {{ t('noSessionsMatch', ui.lang) }}
              </div>
            </div>
          </div>
        </div>
      </transition>
    </teleport>
  </aside>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, nextTick } from 'vue'
import {
  X,
  Pencil,
  PanelLeftClose,
  PanelLeftOpen,
  Languages,
  Settings,
  LogOut,
  Search,
  MoreVertical,
  Trash2,
  Pin,
  Download,
  FileDown,
  Bell,
} from 'lucide-vue-next'
import BrandMark from '../brand/BrandMark.vue'
import {
  buildSessionMarkdown,
  sessionMarkdownFilename,
  useChatStore,
} from '../../stores/chat'
import type {
  SessionAnalysisLabels,
  SessionExportLabels,
} from '../../stores/chat'
import {
  buildSessionHtml,
  sessionReportFilename,
} from '../../utils/session-report'
import type { SessionReportLabels } from '../../utils/session-report'
import type { SessionInfo } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useUiStore } from '../../stores/ui'
import { useRouter } from 'vue-router'
import { t } from '../../i18n'
import { trunc } from '../../utils/format'
import { notifyError } from '../../utils/notify'

type SessionRow = SessionInfo

const chat = useChatStore()
const ui = useUiStore()
const auth = useAuthStore()
const router = useRouter()
const searchText = ref('')
const searchOpen = ref(false)
const searchInput = ref<HTMLInputElement>()
const menu = ref({ open: false, sid: '', x: 0, y: 0 })
const menuEl = ref<HTMLElement>()
const renamingId = ref('')
const renameDraft = ref('')
const renameEl = ref<HTMLInputElement>()

const filteredSessions = computed(() => {
  const kw = searchText.value.trim().toLowerCase()
  if (!kw) return chat.sessions
  return chat.sessions.filter((s) =>
    (s.title || s.session_id).toLowerCase().includes(kw),
  )
})

function onDocPointerDown(e: PointerEvent) {
  const t = e.target as Node
  if (menu.value.open && !menuEl.value?.contains(t)) closeMenu()
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    closeMenu()
    closeSearch()
  }
}

function openSearch() {
  searchOpen.value = true
  searchText.value = ''
  closeMenu()
  void nextTick(() => searchInput.value?.focus())
}

function closeSearch() {
  searchOpen.value = false
  searchText.value = ''
}

function selectFromSearch(sid: string) {
  closeSearch()
  if (sid !== chat.sessionId) chat.loadSession(sid)
}

function openMenu(s: SessionRow, ev?: PointerEvent | MouseEvent) {
  // anchor at the cursor/three-dot button and clamp into the viewport
  // (menu grew to 5 items — reserve its height so it never opens off-screen)
  let x = ev?.clientX ?? window.innerWidth / 2
  let y = ev?.clientY ?? 80
  x = Math.min(Math.max(0, x), window.innerWidth - 168)
  y = Math.min(Math.max(0, y), window.innerHeight - 208)
  menu.value = { open: true, sid: s.session_id, x, y }
}

function closeMenu() {
  menu.value.open = false
}

function menuTarget(): SessionRow | undefined {
  return chat.sessions.find((x) => x.session_id === menu.value.sid)
}

/** 菜单里显示「取消置顶」还是「置顶」——按目标行当前状态。 */
const menuPinned = computed(() => !!menuTarget()?.pinned)

function menuPin() {
  const s = menuTarget()
  if (!s) return closeMenu()
  closeMenu()
  void chat.pinSession(s.session_id, !s.pinned)
}

function menuExport() {
  const sid = menu.value.sid
  closeMenu()
  void exportSession(sid)
}

function menuExportReport() {
  const sid = menu.value.sid
  closeMenu()
  void exportReport(sid)
}

/** 分析证据节文案(md 导出与 HTML 报告共用;补丁 2)。 */
function analysisLabels(): SessionAnalysisLabels {
  return {
    title: t('exportDocEvidence', ui.lang),
    evidence: t('anaEvidence', ui.lang),
    partial: t('anaPartial', ui.lang),
    partialHint: t('anaPartialHint', ui.lang),
    truncated: t('anaTruncated', ui.lang),
    purposes: {
      overall: t('anaPurposeOverall', ui.lang),
      probe: t('anaPurposeProbe', ui.lang),
      drilldown: t('anaPurposeDrilldown', ui.lang),
      driver_tree: t('anaPurposeTree', ui.lang),
    },
  }
}

function reportLabels(): SessionReportLabels {
  return {
    generatedAt: t('exportDocGeneratedAt', ui.lang),
    rounds: t('exportDocRounds', ui.lang),
    results: t('exportDocResult', ui.lang),
    rows: t('exportDocRows', ui.lang),
    cols: t('exportDocCols', ui.lang),
    chart: t('exportDocChart', ui.lang),
    analysis: analysisLabels(),
  }
}

/** 导出整段会话:取轮次 → 渲染 Markdown → blob 下载(抄 DataTable 的下载先例)。 */
async function exportSession(sid: string) {
  try {
    const turns = await chat.fetchSessionTurns(sid)
    if (!turns.length) {
      notifyError(t('exportEmpty', ui.lang))
      return
    }
    const labels: SessionExportLabels = {
      results: t('exportDocResult', ui.lang),
      rows: t('exportDocRows', ui.lang),
      cols: t('exportDocCols', ui.lang),
      generatedAt: t('exportDocGeneratedAt', ui.lang),
      rounds: t('exportDocRounds', ui.lang),
      analysis: analysisLabels(),
    }
    const row = chat.sessions.find((s) => s.session_id === sid)
    const markdown = buildSessionMarkdown(turns, {
      title: row?.title ?? '',
      sessionId: sid,
      labels,
    })
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = sessionMarkdownFilename(row?.title ?? '', sid)
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    notifyError(t('exportFailed', ui.lang))
  }
}

/** 导出 HTML 报告(补丁 2):同一取轮次口径 → 自包含单文件 HTML
 *  (图表内联 SVG、证据查询折叠,离线可开,零外部依赖)。 */
async function exportReport(sid: string) {
  try {
    const turns = await chat.fetchSessionTurns(sid)
    if (!turns.length) {
      notifyError(t('exportEmpty', ui.lang))
      return
    }
    const row = chat.sessions.find((s) => s.session_id === sid)
    const html = buildSessionHtml(turns, {
      title: row?.title ?? '',
      sessionId: sid,
      labels: reportLabels(),
      lang: ui.lang,
    })
    const blob = new Blob([html], { type: 'text/html;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = sessionReportFilename(row?.title ?? '', sid)
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    notifyError(t('exportFailed', ui.lang))
  }
}

function menuRename() {
  const s = chat.sessions.find((x) => x.session_id === menu.value.sid)
  if (!s) return closeMenu()
  menu.value.open = false
  renamingId.value = s.session_id
  renameDraft.value = s.title ?? ''
  void nextTick(() => {
    const el = renameEl.value
    if (el) {
      el.focus()
      el.select()
    }
  })
}

function menuDelete() {
  const sid = menu.value.sid
  closeMenu()
  void chat.deleteSession(sid)
}

function cancelRename() {
  renamingId.value = ''
}

async function commitRename(sid: string) {
  if (renamingId.value !== sid) return
  renamingId.value = ''
  const title = renameDraft.value.trim()
  if (title) await chat.renameSession(sid, title)
}

onMounted(() => {
  chat.listSessions()
  window.addEventListener('pointerdown', onDocPointerDown)
  window.addEventListener('keydown', onKey)
})

onUnmounted(() => {
  window.removeEventListener('pointerdown', onDocPointerDown)
  window.removeEventListener('keydown', onKey)
})

const avatarChar = computed(() => {
  const name = auth.user?.display_name || auth.user?.username || ''
  return (name.trim()[0] || '?').toUpperCase()
})

async function onProfileCmd(cmd: string) {
  if (cmd === 'lang') {
    ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
    window.location.reload()
  } else if (cmd === 'subs') {
    await router.push('/subscriptions')
  } else if (cmd === 'admin') {
    await router.push('/admin')
  } else if (cmd === 'logout') {
    await auth.logout()
    await router.push({ name: 'login' })
  }
}

function title(s: SessionRow): string {
  if (s.title && s.title.trim()) return trunc(s.title.trim(), 24)
  return trunc(s.session_id.slice(0, 8), 16)
}

/** Scroll near the bottom → load the next page (下拉加载). */
function onScroll(e: Event) {
  const el = e.target as HTMLElement
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) {
    chat.loadMoreSessions()
  }
}

function newSession() {
  closeMenu()
  chat.clearSession()
}

function select(sid: string) {
  closeMenu()
  if (sid !== chat.sessionId) chat.loadSession(sid)
}

async function remove(sid: string) {
  closeMenu()
  await chat.deleteSession(sid)
}
</script>

<style scoped>
/* 置顶标记:排在标题前的固定小图标(不参与标题省略) */
.session-pin {
  display: inline-flex;
  align-items: center;
  flex: none;
  color: var(--indigo-600);
}
</style>
