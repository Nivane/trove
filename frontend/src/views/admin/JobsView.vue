<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('jobs', ui.lang) }}</h2>
        <p class="view-desc">{{ t('jobsPageDesc', ui.lang) }}</p>
      </div>
    </header>

    <div class="admin-card">
      <div class="card-toolbar">
        <el-button type="primary" class="add" @click="openCreate">
          <Plus :size="15" class="btn-icon" />
          {{ t('jobCreateTitle', ui.lang) }}
        </el-button>
        <span class="spacer" />
        <span v-if="rows.length" class="view-count">{{ rows.length }}</span>
        <el-button class="refresh-btn" :loading="loading" @click="load">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('refresh', ui.lang) }}
        </el-button>
      </div>

      <div v-if="loading && !rows.length" class="table-skeleton">
        <div v-for="n in 5" :key="n" class="skeleton-row">
          <el-skeleton :rows="1" animated />
        </div>
      </div>

      <el-table
        v-else
        v-loading="loading"
        :data="rows"
        class="admin-table"
        max-height="calc(100vh - 320px)"
      >
        <template #empty>
          <TableEmpty />
        </template>
        <el-table-column :label="t('jobName', ui.lang)" min-width="160">
          <template #default="{ row }">
            <div class="job-name">{{ row.name }}</div>
            <div class="job-question">{{ row.question }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobScheduleType', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="pill" :class="row.schedule_type === 'cron' ? 'pill-neutral' : 'pill-ok'">
              {{ row.schedule_type === 'cron' ? t('jobScheduleCron', ui.lang) : t('jobScheduleInterval', ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobSchedule', ui.lang)" width="140">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.schedule }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobDatasource', ui.lang)" width="120">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.datasource }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobAlertExpr', ui.lang)" min-width="160">
          <template #default="{ row }">
            <span v-if="row.alert_expr" class="cell-mono">{{ row.alert_expr }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobLastRun', ui.lang)" width="150">
          <template #default="{ row }">
            <div v-if="row.recent_run" class="job-last-run">
              <span class="pill" :class="runClass(row.recent_run.status)">
                {{ runLabel(row.recent_run.status) }}
              </span>
              <div v-if="row.recent_run.row_count != null" class="job-row-count">
                {{ row.recent_run.row_count }} rows
              </div>
            </div>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobNextRun', ui.lang)" width="160">
          <template #default="{ row }">
            <span v-if="row.enabled && row.next_run_at" class="cell-mono">
              {{ fmtDateTime(row.next_run_at) }}
            </span>
            <span v-else-if="!row.enabled" class="pill pill-warn">{{ t('disable', ui.lang) }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobStatus', ui.lang)" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="toggling === row.id"
              @change="(v: boolean) => toggle(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column :label="t('auditAction', ui.lang)" width="230" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :loading="running === row.id" @click="runNow(row)">
              <Play :size="14" class="btn-icon" />
              {{ t('jobRunNow', ui.lang) }}
            </el-button>
            <el-button size="small" @click="showRuns(row)">
              <History :size="14" class="btn-icon" />
              {{ t('jobRuns', ui.lang) }}
            </el-button>
            <el-button size="small" @click="openEdit(row)">
              <Pencil :size="14" class="btn-icon" />
            </el-button>
            <el-button size="small" type="danger" @click="remove(row)">
              <Trash2 :size="14" class="btn-icon" />
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogOpen"
      :title="editing ? t('jobEditTitle', ui.lang) : t('jobCreateTitle', ui.lang)"
      width="560px"
      class="job-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('jobName', ui.lang)">
          <el-input v-model="form.name" :placeholder="t('jobName', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('jobQuestion', ui.lang)">
          <el-input
            v-model="form.question"
            type="textarea"
            :rows="2"
            :placeholder="t('jobQuestion', ui.lang)"
          />
        </el-form-item>
        <el-form-item :label="t('jobDatasource', ui.lang)">
          <el-select v-model="form.datasource" filterable class="job-ds-select">
            <el-option
              v-for="d in datasources"
              :key="d.name"
              :label="d.name"
              :value="d.name"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('jobScheduleType', ui.lang)">
          <el-radio-group v-model="form.schedule_type">
            <el-radio value="interval">{{ t('jobScheduleInterval', ui.lang) }}</el-radio>
            <el-radio value="cron">{{ t('jobScheduleCron', ui.lang) }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="form.schedule_type === 'cron' ? t('jobCron', ui.lang) : t('jobInterval', ui.lang)">
          <el-input
            v-model="form.schedule"
            :placeholder="form.schedule_type === 'cron' ? '0 9 * * *' : '30'"
          />
        </el-form-item>
        <el-form-item :label="t('jobAlertExpr', ui.lang)">
          <el-input v-model="form.alert_expr" :placeholder="t('jobAlertHint', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('jobAlertChannel', ui.lang)">
          <el-input v-model="form.alert_channel" placeholder="console" />
          <div class="job-field-hint">{{ t('jobChannelHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item :label="t('jobAlertCooldown', ui.lang)">
          <el-input-number v-model="form.alert_cooldown_min" :min="0" :max="1440" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t('cancel', ui.lang) || 'Cancel' }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ editing ? t('save', ui.lang) : t('jobCreateTitle', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="runsOpen"
      :title="t('jobRuns', ui.lang)"
      width="720px"
      class="job-dialog"
    >
      <el-table v-loading="runsLoading" :data="runs" class="admin-table" max-height="420">
        <template #empty>
          <div class="dim">{{ t('jobRunsEmpty', ui.lang) }}</div>
        </template>
        <el-table-column :label="t('auditTime', ui.lang)" width="170">
          <template #default="{ row }">
            <span class="cell-mono">{{ fmtDateTime(row.started_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobStatus', ui.lang)" width="90">
          <template #default="{ row }">
            <span class="pill" :class="runClass(row.status)">{{ runLabel(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('rowCount', ui.lang)" width="90">
          <template #default="{ row }">
            <span v-if="row.row_count != null">{{ row.row_count }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('jobAlertExpr', ui.lang)" min-width="140">
          <template #default="{ row }">
            <span v-if="row.alert_sent" class="pill pill-warn">alert sent</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('ckptRunId', ui.lang)" min-width="120">
          <template #default="{ row }">
            <span class="cell-mono run-id">{{ row.verdict || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { History, Pencil, Play, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { apiGet, apiPatch, apiPost, apiDelete } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, toastError } from '../../utils/notify'
import { fmtDateTime } from '../../utils/format'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import type { DatasourceInfo } from '../../api/types'

interface JobRow {
  id: string
  name: string
  question: string
  datasource: string
  workflow: string
  schedule_type: string
  schedule: string
  enabled: boolean
  alert_expr: string
  alert_channel: string
  alert_cooldown_min: number
  next_run_at: string
  created_at: string
  updated_at: string
  recent_run?: {
    status: string
    row_count?: number
    alert_sent?: boolean
    started_at?: string
    verdict?: string
  } | null
  [k: string]: unknown
}

interface RunRow {
  id: number
  started_at: string
  finished_at: string
  status: string
  row_count: number | null
  alert_sent: boolean
  verdict: string
  result?: Record<string, unknown>
  [k: string]: unknown
}

const ui = useUiStore()
const rows = ref<JobRow[]>([])
const loading = ref(false)
const saving = ref(false)
const toggling = ref('')
const running = ref('')
const dialogOpen = ref(false)
const editing = ref<JobRow | null>(null)
const runsOpen = ref(false)
const runs = ref<RunRow[]>([])
const runsLoading = ref(false)
const datasources = ref<DatasourceInfo[]>([])

const emptyForm = {
  name: '',
  question: '',
  datasource: '',
  schedule_type: 'interval' as 'interval' | 'cron',
  schedule: '',
  alert_expr: '',
  alert_channel: '',
  alert_cooldown_min: 30,
}
const form = reactive({ ...emptyForm })

function runClass(status: string): string {
  if (status === 'ok') return 'pill-ok'
  if (status === 'alert') return 'pill-warn'
  return 'pill-danger'
}

function runLabel(status: string): string {
  if (status === 'ok') return t('jobStatusOk', ui.lang)
  if (status === 'alert') return t('jobStatusAlert', ui.lang)
  return t('jobStatusError', ui.lang)
}

async function loadDatasources() {
  try {
    const body = await apiGet('/v1/catalog/datasources')
    datasources.value = body.datasources ?? []
    if (!form.datasource && datasources.value.length) {
      const def = datasources.value.find((d) => d.default)
      form.datasource = def?.name || datasources.value[0].name
    }
  } catch {
    datasources.value = []
  }
}

async function load() {
  loading.value = true
  try {
    const body = await apiGet('/v1/admin/jobs')
    rows.value = (body.jobs ?? []) as JobRow[]
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, emptyForm)
  const def = datasources.value.find((d) => d.default)
  form.datasource = def?.name || datasources.value[0]?.name || ''
  dialogOpen.value = true
}

function openEdit(row: JobRow) {
  editing.value = row
  Object.assign(form, {
    name: row.name,
    question: row.question,
    datasource: row.datasource,
    schedule_type: row.schedule_type,
    schedule: row.schedule,
    alert_expr: row.alert_expr,
    alert_channel: row.alert_channel,
    alert_cooldown_min: row.alert_cooldown_min,
  })
  dialogOpen.value = true
}

async function save() {
  if (!form.question.trim()) return
  saving.value = true
  try {
    const payload = {
      name: form.name,
      question: form.question,
      datasource: form.datasource,
      schedule_type: form.schedule_type,
      schedule: form.schedule,
      alert_expr: form.alert_expr,
      alert_channel: form.alert_channel,
      alert_cooldown_min: form.alert_cooldown_min,
    }
    if (editing.value) {
      await apiPatch(`/v1/admin/jobs/${editing.value.id}`, payload)
      notifySuccess(t('jobUpdated', ui.lang))
    } else {
      await apiPost('/v1/admin/jobs', payload)
      notifySuccess(t('jobCreated', ui.lang))
    }
    dialogOpen.value = false
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

async function toggle(row: JobRow, enabled: boolean) {
  toggling.value = row.id
  try {
    await apiPatch(`/v1/admin/jobs/${row.id}`, { enabled })
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    toggling.value = ''
  }
}

async function runNow(row: JobRow) {
  try {
    await ElMessageBox.confirm(t('jobConfirmRun', ui.lang), t('jobRunNow', ui.lang), {
      type: 'warning',
      confirmButtonText: t('jobRunNow', ui.lang),
      cancelButtonText: t('cancel', ui.lang) || 'Cancel',
    })
  } catch {
    return
  }
  running.value = row.id
  try {
    const body = await apiPost(`/v1/admin/jobs/${row.id}/run`)
    ElMessage.success(t('jobRunTriggered', ui.lang))
    if (body?.run?.status) {
      ElMessage.success(
        `${t('jobStatus', ui.lang)}: ${runLabel(body.run.status)} · ${body.run.row_count ?? 0} rows`,
      )
    }
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    running.value = ''
  }
}

async function showRuns(row: JobRow) {
  runsOpen.value = true
  runsLoading.value = true
  try {
    const body = await apiGet(`/v1/admin/jobs/${row.id}/runs`)
    runs.value = (body.runs ?? []) as RunRow[]
  } catch (e) {
    toastError(e)
  } finally {
    runsLoading.value = false
  }
}

async function remove(row: JobRow) {
  try {
    await ElMessageBox.confirm(t('jobConfirmDelete', ui.lang), t('deleteUser', ui.lang), {
      type: 'warning',
      confirmButtonText: t('deleteUser', ui.lang),
      cancelButtonText: t('cancel', ui.lang) || 'Cancel',
    })
  } catch {
    return
  }
  try {
    await apiDelete(`/v1/admin/jobs/${row.id}`)
    notifySuccess(t('jobDeleted', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

onMounted(() => {
  load()
  loadDatasources()
})
</script>
