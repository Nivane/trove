<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('checkpoints', ui.lang) }}</h2>
        <p class="view-desc">{{ t('checkpointsPageDesc', ui.lang) }}</p>
      </div>
    </header>

    <div class="admin-card">
      <div class="card-toolbar">
        <el-select
          v-model="sessionId"
          :placeholder="t('ckptPickSession', ui.lang)"
          class="toolbar-search ckpt-session-select"
          filterable
          clearable
          :no-data-text="t('ckptNoSessions', ui.lang)"
          @change="load"
        >
          <el-option
            v-for="s in sessions"
            :key="s.session_id"
            :label="sessionLabel(s)"
            :value="s.session_id"
          />
        </el-select>
        <span class="spacer" />
        <span v-if="sessionId" class="view-count">
          {{ checkpoints.length }}
        </span>
        <el-button class="refresh-btn" :loading="loading" @click="loadSessions">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('refresh', ui.lang) }}
        </el-button>
      </div>

      <div v-if="loading && !checkpoints.length" class="table-skeleton">
        <div v-for="n in 6" :key="n" class="skeleton-row">
          <el-skeleton :rows="1" animated />
        </div>
      </div>

      <el-table
        v-else
        v-loading="loading"
        :data="checkpoints"
        class="admin-table"
        max-height="calc(100vh - 300px)"
      >
        <template #empty>
          <TableEmpty />
        </template>
        <el-table-column :label="t('ckptStep', ui.lang)" width="90">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.step ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptNode', ui.lang)" min-width="130">
          <template #default="{ row }">
            <span class="pill pill-neutral">{{ row.node || t('ckptNone', ui.lang) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptSource', ui.lang)" width="100">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.source || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptTime', ui.lang)" width="170">
          <template #default="{ row }">
            <span class="cell-mono">{{ fmtDateTime(row.ts) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptRunId', ui.lang)" min-width="160">
          <template #default="{ row }">
            <span class="cell-mono run-id">{{ row.run_id || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptSummary', ui.lang)" min-width="280">
          <template #default="{ row }">
            <div class="ckpt-summary">
              <div v-if="row.state?.question" class="ckpt-question">{{ row.state.question }}</div>
              <div v-if="row.state?.sql" class="ckpt-sql">{{ row.state.sql }}</div>
              <div class="ckpt-tags">
                <span v-if="row.state?.verdict" class="pill" :class="verdictClass(row.state.verdict)">
                  {{ row.state.verdict }}
                </span>
                <span
                  v-if="row.state?.hitl_status === 'pending'"
                  class="pill pill-warn"
                >
                  {{ t('ckptHitl', ui.lang) }}
                </span>
                <span v-if="row.state?.error" class="pill pill-danger">{{ t('ckptFailed', ui.lang) }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('auditAction', ui.lang)" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row)">
              <Eye :size="14" class="btn-icon" />
              {{ t('ckptViewDetail', ui.lang) }}
            </el-button>
            <el-button size="small" type="primary" :loading="resuming" @click="confirmResume(row)">
              <Play :size="14" class="btn-icon" />
              {{ t('ckptResume', ui.lang) }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="sessionId && !checkpoints.length && !loading" class="ckpt-empty">
        {{ t('ckptEmpty', ui.lang) }}
      </div>
    </div>

    <el-dialog
      v-model="detailOpen"
      :title="t('ckptDetailTitle', ui.lang)"
      width="720px"
      class="ckpt-dialog"
    >
      <template v-if="detail">
        <div class="ckpt-detail-grid">
          <div><span class="dim">{{ t('ckptStep', ui.lang) }}:</span> {{ detail.step ?? '—' }}</div>
          <div><span class="dim">{{ t('ckptNode', ui.lang) }}:</span> {{ detail.node || '—' }}</div>
          <div><span class="dim">checkpoint_id:</span> <span class="cell-mono">{{ detail.checkpoint_id }}</span></div>
          <div><span class="dim">{{ t('ckptTime', ui.lang) }}:</span> {{ fmtDateTime(detail.ts) }}</div>
          <div><span class="dim">{{ t('ckptRunId', ui.lang) }}:</span> <span class="cell-mono">{{ detail.run_id || '—' }}</span></div>
          <div><span class="dim">{{ t('ckptSource', ui.lang) }}:</span> {{ detail.source || '—' }}</div>
        </div>
        <div v-if="summaryPairs.length" class="ckpt-summary-block">
          <div class="ckpt-block-title">{{ t('ckptSummary', ui.lang) }}</div>
          <div v-for="p in summaryPairs" :key="p.k" class="ckpt-kv">
            <span class="dim">{{ p.k }}</span>
            <span class="ckpt-kv-val">{{ p.v }}</span>
          </div>
        </div>
        <div class="ckpt-block-title">
          {{ t('ckptFullState', ui.lang) }}
        </div>
        <pre class="ckpt-json">{{ prettyState }}</pre>
      </template>
    </el-dialog>

    <el-dialog
      v-model="resumeOpen"
      :title="t('ckptResumeDone', ui.lang)"
      width="720px"
      class="ckpt-dialog"
    >
      <template v-if="resumeResult">
        <div v-if="resumeResult.hitl_status === 'pending'" class="ckpt-warn">
          {{ t('ckptResumeHitl', ui.lang) }}
        </div>
        <div v-if="resumeResult.error" class="ckpt-error">{{ resumeResult.error }}</div>
        <div class="ckpt-resume-md">{{ resumeResult.final_response || resumeResult.error || '—' }}</div>
        <div v-if="resumeResult.sql" class="ckpt-block-title">{{ t('ckptSql', ui.lang) }}</div>
        <pre class="ckpt-sql-block">{{ resumeResult.sql }}</pre>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { Eye, Play, RefreshCw } from 'lucide-vue-next'
import { apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError } from '../../utils/notify'
import { fmtDateTime } from '../../utils/format'
import TableEmpty from '../../components/admin/TableEmpty.vue'

interface SessionRow {
  session_id: string
  user_id?: string
  title?: string
  updated_at?: string
  created_at?: string
  [k: string]: unknown
}

interface CheckpointRow {
  checkpoint_id: string
  thread_id?: string
  ts?: string
  step?: number
  source?: string
  run_id?: string
  node?: string
  state?: Record<string, unknown>
  state_full?: Record<string, unknown>
  [k: string]: unknown
}

const ui = useUiStore()
const sessions = ref<SessionRow[]>([])
const sessionId = ref('')
const checkpoints = ref<CheckpointRow[]>([])
const loading = ref(false)
const resuming = ref(false)

const detail = ref<CheckpointRow | null>(null)
const detailOpen = ref(false)
const resumeResult = ref<Record<string, unknown> | null>(null)
const resumeOpen = ref(false)

function sessionLabel(s: SessionRow): string {
  const when = fmtDateTime(s.updated_at)
  return `${s.title || s.session_id} · ${s.user_id || 'local'} · ${when}`
}

function verdictClass(v: string): string {
  if (v === 'OK') return 'pill-ok'
  if (v === 'RETRY') return 'pill-warn'
  return 'pill-neutral'
}

const summaryPairs = computed(() => {
  const st = detail.value?.state || {}
  return Object.entries(st)
    .filter(([, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => ({ k, v: typeof v === 'string' ? v : JSON.stringify(v) }))
})

const prettyState = computed(() => {
  const full = detail.value?.state_full || detail.value?.state || {}
  try {
    return JSON.stringify(full, null, 2)
  } catch {
    return String(full)
  }
})

async function loadSessions() {
  loading.value = true
  try {
    const body = await apiGet('/v1/admin/sessions?limit=100')
    sessions.value = (body.sessions ?? []) as SessionRow[]
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function load() {
  if (!sessionId.value) {
    checkpoints.value = []
    return
  }
  loading.value = true
  try {
    const body = await apiGet(`/v1/admin/sessions/${sessionId.value}/checkpoints?limit=500`)
    checkpoints.value = (body.checkpoints ?? []) as CheckpointRow[]
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function openDetail(row: CheckpointRow) {
  try {
    detail.value = await apiGet(
      `/v1/admin/sessions/${sessionId.value}/checkpoints/${row.checkpoint_id}`,
    )
    detailOpen.value = true
  } catch (e) {
    toastError(e)
  }
}

async function confirmResume(row: CheckpointRow) {
  try {
    await ElMessageBox.confirm(t('ckptResumeConfirm', ui.lang), t('ckptResume', ui.lang), {
      type: 'warning',
      confirmButtonText: t('ckptResume', ui.lang),
      cancelButtonText: t('cancel', ui.lang) || 'Cancel',
    })
  } catch {
    return
  }
  resuming.value = true
  try {
    const body = await apiPost(
      `/v1/admin/sessions/${sessionId.value}/checkpoints/${row.checkpoint_id}/resume`,
      { workflow: 'reflection' },
    )
    resumeResult.value = body.summary ?? {}
    resumeOpen.value = true
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    resuming.value = false
  }
}

onMounted(loadSessions)
</script>
