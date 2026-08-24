<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('kb', ui.lang) }}</h2>
        <p class="view-desc">{{ t('kbPageDesc', ui.lang) }}</p>
      </div>
    </header>

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon accent"><Library :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('kbTerms', ui.lang) }}</span>
          <span class="stat-value">{{ terms.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><FileCode2 :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('kbExamples', ui.lang) }}</span>
          <span class="stat-value">{{ examples.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><ListChecks :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('kbRules', ui.lang) }}</span>
          <span class="stat-value">{{ rules.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon" :class="pending.length ? 'warn' : 'ok'">
          <Inbox :size="18" />
        </span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('pendingLessons', ui.lang) }}</span>
          <span class="stat-value">{{ pending.length }}</span>
          <span class="stat-sub">{{ t('confirmedLessons', ui.lang) }} · {{ confirmed.length }}</span>
        </div>
      </div>
    </div>

    <div class="admin-card kb-summary">
      <div class="card-header">
        <div class="card-title">{{ t('kb', ui.lang) }}</div>
        <div class="card-actions">
          <el-select
            v-model="ds"
            class="ds-select"
            :placeholder="t('kbSelectDs', ui.lang)"
            @change="loadDetail"
          >
            <el-option
              v-for="d in connected"
              :key="d.name"
              :value="d.name"
              :label="d.name"
            >
              <span>{{ d.name }}</span>
              <span v-if="d.default" class="cell-muted"> · default</span>
            </el-option>
          </el-select>
          <el-button class="refresh-btn" :loading="loading" @click="loadAll">
            <RefreshCw :size="15" class="btn-icon" />
            {{ t('refresh', ui.lang) }}
          </el-button>
        </div>
      </div>
      <div class="kb-stat-row">
        <span class="pill" :class="initialized ? 'pill-ok' : 'pill-warn'">
          <span class="pill-dot" :class="initialized ? 'pill-dot-ok' : ''" />
          {{
            initialized ? t('dsKbReady', ui.lang) : t('dsKbNotReady', ui.lang)
          }}
        </span>
        <span v-if="status.files?.length" class="cell-muted">
          {{ status.files.join(' · ') }}
        </span>
        <span
          v-for="(n, kind) in status.items || {}"
          :key="kind"
          class="kb-chip"
        >
          {{ kind }}: {{ n }}
        </span>
      </div>
      <div class="row-actions kb-actions">
        <el-button
          v-if="!initialized"
          type="primary"
          :loading="busy('init')"
          :disabled="!ds"
          @click="initKb"
        >
          <Sparkles :size="15" class="btn-icon" />
          {{ t('dsInit', ui.lang) }}
        </el-button>
        <el-button
          v-if="initialized"
          :loading="busy('reload')"
          :disabled="!ds"
          @click="reloadKb"
        >
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('dsReload', ui.lang) }}
        </el-button>
        <el-button
          v-if="initialized"
          type="danger"
          plain
          :loading="busy('delete')"
          :disabled="!ds"
          @click="deleteKb"
        >
          <Trash2 :size="15" class="btn-icon" />
          {{ t('kbDelete', ui.lang) }}
        </el-button>
      </div>
      <div v-if="!initialized" class="empty-note">
        {{ t('kbNotFound', ui.lang) }}
      </div>
      <div v-if="busy('init') && initProgress" class="kb-init-progress">
        <el-progress
          :percentage="initProgress.progress || 0"
          :stroke-width="8"
        />
        <div class="kb-init-stage">
          {{ initStageLabel(initProgress.stage) }}
          <span v-if="initProgress.detail" class="cell-muted">
            {{ initProgress.detail }}
          </span>
        </div>
      </div>
    </div>

    <div class="admin-tabs">
      <el-tabs v-model="tab">
        <el-tab-pane :name="'terms'">
          <template #label>
            <span class="tab-label">
              {{ t('kbTerms', ui.lang) }}
              <span v-if="terms.length" class="tab-badge">{{
                terms.length
              }}</span>
            </span>
          </template>
          <div class="admin-card">
            <div class="card-toolbar">
              <span class="view-count">{{ terms.length }} · {{ t('kbTerms', ui.lang) }}</span>
              <span class="spacer" />
              <el-button type="primary" @click="openTerm">
                <Plus :size="15" class="btn-icon" />
                {{ t('kbAddTerm', ui.lang) }}
              </el-button>
            </div>
            <el-table
              v-loading="loading"
              :data="terms"
              class="admin-table"
              max-height="calc(100vh - 380px)"
            >
              <template #empty>
                <TableEmpty>{{ t('kbTermsEmpty', ui.lang) }}</TableEmpty>
              </template>
              <el-table-column
                :label="t('kbTermField', ui.lang)"
                min-width="160"
              >
                <template #default="{ row }">
                  <div class="lesson-cell">
                    <span class="lesson-title">{{ row.term }}</span>
                    <span v-if="row.aliases?.length" class="cell-muted">
                      {{ row.aliases.join(' / ') }}
                    </span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="mapping" min-width="220">
                <template #default="{ row }">
                  <code class="mapping-code">{{ row.mapping || '—' }}</code>
                </template>
              </el-table-column>
              <el-table-column :label="t('kbTables', ui.lang)" min-width="160">
                <template #default="{ row }">
                  <span class="cell-muted">
                    <span
                      v-for="tbl in row.tables || []"
                      :key="tbl"
                      class="kb-chip"
                      >{{ tbl }}</span>
                    <span v-if="!(row.tables || []).length">—</span>
                  </span>
                </template>
              </el-table-column>
              <el-table-column
                :label="t('kbDefinition', ui.lang)"
                min-width="220"
              >
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.definition || '—' }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane :name="'examples'">
          <template #label>
            <span class="tab-label">
              {{ t('kbExamples', ui.lang) }}
              <span v-if="examples.length" class="tab-badge">{{
                examples.length
              }}</span>
            </span>
          </template>
          <div class="admin-card">
            <div class="card-toolbar">
              <span class="view-count">{{ examples.length }} · {{ t('kbExamples', ui.lang) }}</span>
              <span class="spacer" />
              <el-button type="primary" @click="openExample">
                <Plus :size="15" class="btn-icon" />
                {{ t('kbAddExample', ui.lang) }}
              </el-button>
            </div>
            <el-table
              v-loading="loading"
              :data="examples"
              class="admin-table"
              max-height="calc(100vh - 380px)"
            >
              <template #empty>
                <TableEmpty>{{ t('kbExamplesEmpty', ui.lang) }}</TableEmpty>
              </template>
              <el-table-column
                :label="t('kbQuestion', ui.lang)"
                min-width="240"
              >
                <template #default="{ row }">
                  <span class="lesson-title">{{ row.question }}</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('sql', ui.lang)" min-width="280">
                <template #default="{ row }">
                  <button
                    v-if="row.sql"
                    class="sql-snippet"
                    title="copy"
                    @click="copySnippet(row.sql)"
                  >
                    <code>{{ row.sql }}</code>
                  </button>
                  <span v-else class="cell-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('kbTags', ui.lang)" min-width="150">
                <template #default="{ row }">
                  <span
                    v-for="tag in row.tags || []"
                    :key="tag"
                    class="kb-chip"
                    >{{ tag }}</span>
                  <span v-if="!(row.tags || []).length" class="cell-muted">—</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane :name="'rules'">
          <template #label>
            <span class="tab-label">
              {{ t('kbRules', ui.lang) }}
              <span v-if="rules.length" class="tab-badge">{{
                rules.length
              }}</span>
            </span>
          </template>
          <div class="admin-card">
            <el-table
              v-loading="loading"
              :data="rules"
              class="admin-table"
              max-height="calc(100vh - 380px)"
            >
              <template #empty>
                <TableEmpty>{{ t('kbRulesEmpty', ui.lang) }}</TableEmpty>
              </template>
              <el-table-column :label="t('kbRules', ui.lang)" min-width="100">
                <template #default="{ row }">
                  <span class="lesson-title">{{ row }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane :name="'lessons'">
          <template #label>
            <span class="tab-label">
              {{ t('kb', ui.lang) }}
              <span v-if="pending.length" class="tab-badge">{{
                pending.length
              }}</span>
            </span>
          </template>
          <div class="admin-card">
            <div class="card-toolbar">
              <span class="view-count">
                {{ pending.length }} {{ t('pendingLessons', ui.lang) }} ·
                {{ confirmed.length }}
                {{ t('confirmedLessons', ui.lang) }}
              </span>
              <span class="spacer" />
              <el-button
                v-if="pending.length"
                :loading="acting"
                @click="confirmAll"
              >
                {{ t('kbConfirmAll', ui.lang) }}
              </el-button>
            </div>
            <el-table
              v-loading="loading"
              :data="pending"
              class="admin-table"
              max-height="calc(100vh - 380px)"
            >
              <template #empty>
                <TableEmpty />
              </template>
              <el-table-column :label="t('kb', ui.lang)" min-width="210">
                <template #default="{ row }">
                  <div class="lesson-cell">
                    <span class="lesson-title">{{ label(row) }}</span>
                    <span
                      v-if="row.pattern && row.question"
                      class="cell-muted"
                      >{{ '@' + row.pattern }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="note" min-width="240">
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.note || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="sql" min-width="240">
                <template #default="{ row }">
                  <button
                    v-if="row.sql_snippet"
                    class="sql-snippet"
                    title="copy"
                    @click="copySnippet(row.sql_snippet)"
                  >
                    <code>{{ row.sql_snippet }}</code>
                  </button>
                  <span v-else class="cell-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('upvotes', ui.lang)" width="70">
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.upvotes ?? 0 }}</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('downvotes', ui.lang)" width="70">
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.downvotes ?? 0 }}</span>
                </template>
              </el-table-column>
              <el-table-column
                :label="t('actions', ui.lang)"
                width="90"
                fixed="right"
              >
                <template #default="{ row }">
                  <div class="row-actions">
                    <button
                      class="mini-btn icon primary"
                      :title="t('confirmLesson', ui.lang)"
                      :disabled="acting"
                      @click="confirmLesson(row)"
                    >
                      <Check :size="13" />
                    </button>
                    <button
                      class="mini-btn icon is-danger"
                      :title="t('rejectLesson', ui.lang)"
                      :disabled="acting"
                      @click="rejectLesson(row)"
                    >
                      <X :size="13" />
                    </button>
                  </div>
                </template>
              </el-table-column>
            </el-table>
            <el-table
              v-loading="loading"
              :data="confirmed"
              class="admin-table kb-confirmed-table"
              max-height="calc(100vh - 380px)"
            >
              <template #empty>
                <TableEmpty />
              </template>
              <el-table-column :label="t('kb', ui.lang)" min-width="210">
                <template #default="{ row }">
                  <div class="lesson-cell">
                    <span class="lesson-title">{{ label(row) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="note" min-width="260">
                <template #default="{ row }">
                  <span class="cell-muted">{{ row.note || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="sql" min-width="240">
                <template #default="{ row }">
                  <button
                    v-if="row.sql_snippet"
                    class="sql-snippet"
                    title="copy"
                    @click="copySnippet(row.sql_snippet)"
                  >
                    <code>{{ row.sql_snippet }}</code>
                  </button>
                  <span v-else class="cell-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('status', ui.lang)" width="110">
                <template #default>
                  <span class="pill pill-ok">
                    <span class="pill-dot pill-dot-ok" />
                    {{ t('confirmed', ui.lang) }}
                  </span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog
      v-model="termOpen"
      :title="t('kbAddTerm', ui.lang)"
      width="520"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="addTerm">
        <el-form-item :label="t('kbTermField', ui.lang)">
          <el-input
            v-model="termForm.term"
            :placeholder="t('kbTermField', ui.lang)"
          />
        </el-form-item>
        <el-form-item
          :label="`${t('kbAliases', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="termForm.aliases" />
        </el-form-item>
        <el-form-item :label="t('kbMapping', ui.lang)">
          <el-input
            v-model="termForm.mapping"
            :placeholder="t('kbMappingHint', ui.lang)"
            class="mono-input"
          />
          <div class="form-hint">{{ t('kbMappingHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item
          :label="`${t('kbTables', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="termForm.tables" />
        </el-form-item>
        <el-form-item
          :label="`${t('kbDefinition', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="termForm.definition" type="textarea" :rows="2" />
        </el-form-item>
        <div v-if="termError" class="form-error">
          <span>{{ termError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="termOpen = false">
{{
          t('cancel', ui.lang)
        }}
</el-button>
        <el-button type="primary" :loading="acting" @click="addTerm">
          {{ t('confirm', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="exampleOpen"
      :title="t('kbAddExample', ui.lang)"
      width="560"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="addExample">
        <el-form-item :label="t('kbQuestion', ui.lang)">
          <el-input v-model="exForm.question" />
        </el-form-item>
        <el-form-item :label="t('sql', ui.lang)">
          <el-input
            v-model="exForm.sql"
            type="textarea"
            :rows="4"
            spellcheck="false"
            class="mono-input"
          />
        </el-form-item>
        <el-form-item
          :label="`${t('kbTags', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="exForm.tags" />
        </el-form-item>
        <div v-if="exError" class="form-error">
          <span>{{ exError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="exampleOpen = false">
{{
          t('cancel', ui.lang)
        }}
</el-button>
        <el-button type="primary" :loading="acting" @click="addExample">
          {{ t('confirm', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Plus,
  RefreshCw,
  Library,
  FileCode2,
  ListChecks,
  Inbox,
  Sparkles,
  Trash2,
  Check,
  X,
} from 'lucide-vue-next'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiDelete, apiGet, apiPost } from '../../api/http'
import type { DatasourceInfo } from '../../api/types'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import { copyText } from '../../utils/format'
import TableEmpty from '../../components/admin/TableEmpty.vue'

interface Term {
  term?: string
  aliases?: string[]
  mapping?: string
  tables?: string[]
  definition?: string
  question?: string
}
interface Example {
  question?: string
  sql?: string
  tags?: string[]
  aggregate?: boolean
  date_range?: boolean
}
interface Lesson {
  pattern?: string
  question?: string
  note?: string
  sql_snippet?: string
  confirmed?: boolean
  upvotes?: number
  downvotes?: number
}
interface KbDetail {
  status: {
    initialized?: boolean
    files?: string[]
    items?: Record<string, number>
  }
  terms: Term[]
  examples: Example[]
  rules: string[]
  lessons: Lesson[]
}

function label(l: Lesson): string {
  return l.question || l.pattern || '-'
}
function splitCsv(v: string): string[] {
  return v
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

const ui = useUiStore()
const tab = ref('terms')
const ds = ref('')
const datasources = ref<DatasourceInfo[]>([])
const detail = ref<KbDetail | null>(null)
const loading = ref(false)
const acting = ref(false)
const busyMap: Record<string, boolean> = {}

const connected = computed(() =>
  datasources.value.filter((d) => d.status === 'connected'),
)
const initialized = computed(() => !!detail.value?.status.initialized)
const status = computed(() => detail.value?.status || {})
const terms = computed(() => detail.value?.terms || [])
const examples = computed(() => detail.value?.examples || [])
const rules = computed(() => detail.value?.rules || [])
const lessons = computed(() => detail.value?.lessons || [])
const pending = computed(() => lessons.value.filter((l) => !l.confirmed))
const confirmed = computed(() => lessons.value.filter((l) => l.confirmed))

function busy(name: string): boolean {
  return !!busyMap[name]
}
function setBusy(name: string, v: boolean) {
  if (v) busyMap[name] = true
  else delete busyMap[name]
}

async function loadDatasources() {
  const body = await apiGet('/v1/admin/datasources')
  datasources.value = body.datasources ?? []
  if (!ds.value && connected.value.length) {
    const dflt = connected.value.find((d) => d.default)
    ds.value = dflt ? dflt.name : connected.value[0].name
  }
}

async function loadDetail() {
  if (!ds.value) return
  loading.value = true
  try {
    const body = await apiGet(
      `/v1/admin/datasources/${encodeURIComponent(ds.value)}/kb`,
    )
    detail.value = body.kb
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  loading.value = true
  try {
    await loadDatasources()
    await loadDetail()
  } finally {
    loading.value = false
  }
}

async function copySnippet(sql: string) {
  const ok = await copyText(sql)
  notifySuccess(ok ? t('copied', ui.lang) : t('copyFailed', ui.lang))
}

const initProgress = ref<{
  stage?: string
  progress?: number
  detail?: string
} | null>(null)

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function initStageLabel(stage?: string): string {
  const map: Record<string, string> = {
    queued: t('kbInitStageQueued', ui.lang),
    schema: t('kbInitStageSchema', ui.lang),
    probe: t('kbInitStageProbe', ui.lang),
    notes: t('kbInitStageNotes', ui.lang),
    examples: t('kbInitStageExamples', ui.lang),
    semantic: t('kbInitStageSemantic', ui.lang),
    write: t('kbInitStageWrite', ui.lang),
    done: t('kbInitStageDone', ui.lang),
    error: t('kbInitStageError', ui.lang),
  }
  return (stage && map[stage]) || stage || ''
}

async function pollInitStatus(): Promise<void> {
  for (;;) {
    await sleep(2000)
    const st = await apiGet<{
      status: string
      stage?: string
      progress?: number
      detail?: string
      summary?: string
      error?: string
    }>(`/v1/admin/datasources/${encodeURIComponent(ds.value)}/kb/init/status`)
    initProgress.value = {
      stage: st.stage,
      progress: st.progress ?? 0,
      detail: st.detail,
    }
    if (st.status === 'done') {
      initProgress.value = { stage: 'done', progress: 100 }
      return
    }
    if (st.status === 'error') {
      throw new Error(st.error || t('dsInitFail', ui.lang))
    }
    if (st.status === 'idle') {
      throw new Error(t('dsInitLost', ui.lang))
    }
  }
}

async function initKb() {
  try {
    await ElMessageBox.confirm(t('dsInitConfirm', ui.lang), 'Confirm')
  } catch {
    return
  }
  setBusy('init', true)
  initProgress.value = { stage: 'queued', progress: 0 }
  const notice = ElMessage({
    type: 'info',
    message: t('dsInitStarted', ui.lang),
    duration: 0,
  })
  try {
    // 异步 init:202 + task_id → 轮询状态(后台跑,不阻塞请求)
    await apiPost(
      `/v1/admin/datasources/${encodeURIComponent(ds.value)}/kb/init`,
      {},
    )
    await pollInitStatus()
    notifySuccess(t('dsInitDone', ui.lang))
    await loadDetail()
  } catch (e) {
    toastError(e, t('dsInitFail', ui.lang))
  } finally {
    notice.close()
    setBusy('init', false)
    initProgress.value = null
  }
}

async function pollReloadStatus(): Promise<void> {
  for (;;) {
    await sleep(2000)
    const st = await apiGet<{
      status: string
      error?: string
    }>(`/v1/admin/datasources/${encodeURIComponent(ds.value)}/kb/reload/status`)
    if (st.status === 'done') return
    if (st.status === 'error') {
      throw new Error(st.error || t('dsReloadFail', ui.lang))
    }
    if (st.status === 'idle') {
      throw new Error(t('dsReloadLost', ui.lang))
    }
  }
}

async function reloadKb() {
  setBusy('reload', true)
  const notice = ElMessage({
    type: 'info',
    message: t('dsReloadStarted', ui.lang),
    duration: 0,
  })
  try {
    // 异步 reload:202 + task_id → 轮询状态(后台同步,不阻塞请求)
    await apiPost(
      `/v1/admin/datasources/${encodeURIComponent(ds.value)}/kb/reload`,
    )
    await pollReloadStatus()
    notifySuccess(t('dsReloadDone', ui.lang))
    await loadDetail()
  } catch (e) {
    toastError(e, t('dsReloadFail', ui.lang))
  } finally {
    notice.close()
    setBusy('reload', false)
  }
}

async function deleteKb() {
  try {
    await ElMessageBox.confirm(t('kbDeleteConfirm', ui.lang), 'Confirm', {
      type: 'warning',
    })
  } catch {
    return
  }
  setBusy('delete', true)
  try {
    const body = await apiDelete(
      `/v1/admin/datasources/${encodeURIComponent(ds.value)}/kb`,
    )
    detail.value = body.kb
    notifySuccess(t('kbDeleteDone', ui.lang))
  } catch (e) {
    toastError(e)
  } finally {
    setBusy('delete', false)
  }
}

// ── 术语新增（学习：写入 semantics.yml，经 OSSIE 转 metric）──
const termOpen = ref(false)
const termError = ref('')
const termForm = ref({
  term: '',
  aliases: '',
  mapping: '',
  tables: '',
  definition: '',
})
function openTerm() {
  termError.value = ''
  Object.assign(termForm.value, {
    term: '',
    aliases: '',
    mapping: '',
    tables: '',
    definition: '',
  })
  termOpen.value = true
}
async function addTerm() {
  const f = termForm.value
  if (!f.term.trim()) return
  if (!f.mapping.trim()) {
    termError.value = t('kbMappingRequired', ui.lang)
    return
  }
  acting.value = true
  termError.value = ''
  try {
    await apiPost(`/v1/kb/terms?datasource=${encodeURIComponent(ds.value)}`, {
      term: f.term.trim(),
      aliases: splitCsv(f.aliases),
      mapping: f.mapping.trim(),
      tables: splitCsv(f.tables),
      definition: f.definition.trim(),
    })
    termOpen.value = false
    notifySuccess(t('kbTermAdded', ui.lang))
    await loadDetail()
  } catch (e) {
    termError.value =
      e && typeof e === 'object' && 'message' in e
        ? String((e as { message: unknown }).message)
        : t('kbAddFail', ui.lang)
  } finally {
    acting.value = false
  }
}

// ── 示例新增（学习：写入 examples.yml）──
const exampleOpen = ref(false)
const exError = ref('')
const exForm = ref({ question: '', sql: '', tags: '' })
function openExample() {
  exError.value = ''
  Object.assign(exForm.value, { question: '', sql: '', tags: '' })
  exampleOpen.value = true
}
async function addExample() {
  const f = exForm.value
  if (!f.question.trim() || !f.sql.trim()) return
  acting.value = true
  exError.value = ''
  try {
    await apiPost(
      `/v1/kb/examples?datasource=${encodeURIComponent(ds.value)}`,
      {
        question: f.question.trim(),
        sql: f.sql.trim(),
        tags: splitCsv(f.tags),
      },
    )
    exampleOpen.value = false
    notifySuccess(t('kbExampleAdded', ui.lang))
    await loadDetail()
  } catch (e) {
    exError.value =
      e && typeof e === 'object' && 'message' in e
        ? String((e as { message: unknown }).message)
        : t('kbAddFail', ui.lang)
  } finally {
    acting.value = false
  }
}

// ── Lessons 审批 ──
async function confirmLesson(row: Lesson) {
  const key = row.pattern || row.question || ''
  if (!key) return
  acting.value = true
  try {
    await apiPost(`/v1/admin/kb/lessons/${encodeURIComponent(key)}/confirm`)
    notifySuccess(t('lessonConfirmedOk', ui.lang))
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}
async function rejectLesson(row: Lesson) {
  const key = row.pattern || row.question || ''
  if (!key) return
  acting.value = true
  try {
    await apiPost(`/v1/admin/kb/lessons/${encodeURIComponent(key)}/reject`)
    notifySuccess(t('lessonRejectedOk', ui.lang))
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}
async function confirmAll() {
  acting.value = true
  try {
    const body = await apiPost(
      `/v1/kb/lessons/confirm?datasource=${encodeURIComponent(ds.value)}`,
    )
    notifySuccess(t('kbConfirmAllDone', ui.lang, body.confirmed))
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}

onMounted(loadAll)
</script>
