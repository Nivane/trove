<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('datasources', ui.lang) }}</h2>
        <p class="view-desc">{{ t('dsPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-header-right">
        <span class="view-count">{{ rows.length }} · {{ t('datasources', ui.lang) }}</span>
        <el-button type="primary" class="add" @click="openDialog">
          <Plus :size="15" class="btn-icon" />
          {{ t('dsCreateTitle', ui.lang) }}
        </el-button>
      </div>
    </header>

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon accent"><Database :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('datasources', ui.lang) }}</span>
          <span class="stat-value">{{ rows.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon ok"><PlugZap :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('dsConnected', ui.lang) }}</span>
          <span class="stat-value">{{ connectedCount }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><Library :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('dsKbReady', ui.lang) }}</span>
          <span class="stat-value">{{ kbReadyCount }}</span>
        </div>
      </div>
    </div>

    <div v-if="!loading && !rows.length" class="ds-empty">
      <span class="ds-empty-icon"><Database :size="22" /></span>
      <div class="ds-empty-title">{{ t('dsEmpty', ui.lang) }}</div>
      <div class="ds-empty-sub">{{ t('dsEmptySub', ui.lang) }}</div>
      <el-button type="primary" class="add" @click="openDialog">
        <Plus :size="15" class="btn-icon" />
        {{ t('dsCreateTitle', ui.lang) }}
      </el-button>
    </div>

    <div v-else class="admin-card">
      <el-table
        v-loading="loading"
        :data="rows"
        class="admin-table"
        empty-text="—"
      >
        <el-table-column :label="t('datasources', ui.lang)" min-width="220">
          <template #default="{ row }">
            <div class="ds-name-cell">
              <span class="ds-icon"><Database :size="16" /></span>
              <div class="ds-name-meta">
                <div class="ds-name-row">
                  <span class="ds-name">{{ row.name }}</span>
                  <span v-if="row.default" class="pill pill-accent">default</span>
                </div>
                <span class="ds-type">{{ row.type }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('dsStatus', ui.lang)" width="140">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.status === 'connected' ? 'pill-ok' : 'pill-neutral'"
            >
              <span
                class="pill-dot"
                :class="row.status === 'connected' ? 'pill-dot-ok' : ''"
              />
              {{
                row.status === 'connected'
                  ? t('dsConnected', ui.lang)
                  : t('dsDisconnected', ui.lang)
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('dsKb', ui.lang)" width="150">
          <template #default="{ row }">
            <span
              class="pill"
              :class="row.kb_initialized ? 'pill-ok' : 'pill-warn'"
            >
              <span
                class="pill-dot"
                :class="row.kb_initialized ? 'pill-dot-ok' : ''"
              />
              {{
                row.kb_initialized
                  ? t('dsKbReady', ui.lang)
                  : t('dsKbNotReady', ui.lang)
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column
          :label="t('actions', ui.lang)"
          width="320"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="row-actions">
              <button
                v-if="!row.kb_initialized"
                class="mini-btn primary init"
                :disabled="busy(row.name, 'init')"
                @click="initKb(row)"
              >
                <Sparkles :size="13" />
                {{ t('dsInit', ui.lang) }}
              </button>
              <button
                v-if="row.kb_initialized"
                class="mini-btn"
                :disabled="busy(row.name, 'reload')"
                @click="reloadKb(row)"
              >
                <RefreshCw :size="13" />
                {{ t('dsReload', ui.lang) }}
              </button>
              <button
                class="mini-btn"
                :disabled="busy(row.name, 'reconnect')"
                @click="reconnect(row)"
              >
                <PlugZap :size="13" />
                {{ t('dsReconnect', ui.lang) }}
              </button>
              <button class="mini-btn is-danger" @click="remove(row)">
                <Trash2 :size="13" />
                {{ t('dsRemove', ui.lang) }}
              </button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dlgOpen"
      :title="t('dsCreateTitle', ui.lang)"
      width="480"
      class="admin-dialog ds-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="add">
        <el-form-item :label="t('dsTypeLabel', ui.lang)">
          <el-select v-model="form.type" class="ds-type-select">
            <el-option
              v-for="opt in typeOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <div class="form-hint">{{ t('dsTypeHint', ui.lang) }}</div>
        </el-form-item>

        <el-form-item v-if="form.type !== 'demo'" :label="t('dsUrl', ui.lang)">
          <el-input
            v-model="form.url"
            class="ds-url-input"
            :placeholder="exampleFor(form.type)"
            spellcheck="false"
            autocomplete="off"
            @input="detectType"
          />
          <div class="form-hint">
            {{ t('dsExample', ui.lang) }}
            <button class="link-btn" type="button" @click="insertExample()">
              {{ exampleFor(form.type) }}
            </button>
          </div>
        </el-form-item>

        <el-form-item :label="t('dsName', ui.lang)">
          <el-input
            v-model="form.name"
            class="ds-name-field"
            :placeholder="t('dsNamePlaceholder', ui.lang)"
            autocomplete="off"
          />
          <div class="form-hint">{{ t('dsNameHint', ui.lang) }}</div>
        </el-form-item>

        <div v-if="formError" class="form-error">
          <AlertCircle :size="15" />
          <span>{{ formError }}</span>
        </div>
      </el-form>

      <template #footer>
        <span class="dialog-hint">
          <Info :size="13" />
          {{ t('dsProbeHint', ui.lang) }}
        </span>
        <span class="dialog-footer-spacer" />
        <el-button @click="dlgOpen = false">
{{
          t('cancel', ui.lang)
        }}
</el-button>
        <el-button
          type="primary"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="add"
        >
          {{ t('dsRegister', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  Plus,
  Database,
  AlertCircle,
  Info,
  PlugZap,
  Library,
  RefreshCw,
  Sparkles,
  Trash2,
} from 'lucide-vue-next'
import { apiDelete, apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import type { DatasourceInfo } from '../../api/types'

const ui = useUiStore()
const rows = ref<DatasourceInfo[]>([])
const loading = ref(false)
const dlgOpen = ref(false)
const submitting = ref(false)
const formError = ref('')
const busyMap = reactive<Record<string, boolean>>({})

const connectedCount = computed(
  () => rows.value.filter((r) => r.status === 'connected').length,
)
const kbReadyCount = computed(
  () => rows.value.filter((r) => r.kb_initialized).length,
)

const form = reactive({
  type: 'demo',
  name: '',
  url: '',
})

interface TypeMeta {
  value: string
  label: string
  example: string
}

// Connection-string examples per scheme — demo needs no URL.
const TYPES: Record<string, TypeMeta> = {
  demo: { value: 'demo', label: 'Demo · 内置示例', example: '' },
  mysql: {
    value: 'mysql',
    label: 'MySQL',
    example: 'mysql://user:pass@localhost:3306/financial',
  },
  clickhouse: {
    value: 'clickhouse',
    label: 'ClickHouse',
    example: 'clickhouse://user@localhost:8123/default',
  },
  sqlite: {
    value: 'sqlite',
    label: 'SQLite',
    example: 'sqlite:///path/to/data.db',
  },
  duckdb: {
    value: 'duckdb',
    label: 'DuckDB',
    example: 'duckdb:///path/to/data.duckdb',
  },
}

const typeOptions = Object.values(TYPES) as TypeMeta[]

function exampleFor(type: string): string {
  return TYPES[type]?.example ?? ''
}

const canSubmit = computed(() => {
  if (form.type === 'demo') return true
  return !!form.url.trim()
})

function busyKey(row: DatasourceInfo, action: string) {
  return `${row.name}:${action}`
}
function busy(row: DatasourceInfo, action: string): boolean {
  return !!busyMap[busyKey(row, action)]
}
function setBusy(row: DatasourceInfo, action: string, v: boolean) {
  if (v) busyMap[busyKey(row, action)] = true
  else delete busyMap[busyKey(row, action)]
}

async function load() {
  loading.value = true
  try {
    rows.value = (await apiGet('/v1/admin/datasources')).datasources ?? []
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function openDialog() {
  formError.value = ''
  Object.assign(form, { type: 'demo', name: '', url: '' })
  dlgOpen.value = true
}

function insertExample() {
  form.url = exampleFor(form.type)
}

/** When a ``scheme://`` URL is pasted in, switch the type selector to match. */
function detectType() {
  const scheme = form.url.trim().split('://')[0].toLowerCase()
  if (scheme && scheme !== 'demo' && TYPES[scheme]) {
    form.type = scheme
  }
}

async function add() {
  if (!canSubmit.value || submitting.value) return
  formError.value = ''
  // demo carries no connection string — the bare marker tells the backend.
  const url = form.type === 'demo' ? 'demo' : form.url.trim()
  submitting.value = true
  try {
    await apiPost('/v1/admin/datasources', { name: form.name.trim(), url })
    dlgOpen.value = false
    notifySuccess(t('dsAddedOk', ui.lang))
    await load()
  } catch (e) {
    formError.value =
      e && typeof e === 'object' && 'message' in e
        ? String((e as { message: unknown }).message)
        : t('dsAddFail', ui.lang)
  } finally {
    submitting.value = false
  }
}

async function initKb(row: DatasourceInfo) {
  try {
    await ElMessageBox.confirm(t('dsInitConfirm', ui.lang), 'Confirm')
  } catch {
    return
  }
  setBusy(row, 'init', true)
  try {
    await apiPost(`/v1/admin/datasources/${row.name}/kb/init`, {})
    notifySuccess(t('dsInitDone', ui.lang))
    await load()
  } catch (e) {
    toastError(e, t('dsInitFail', ui.lang))
  } finally {
    setBusy(row, 'init', false)
  }
}

async function runAction(
  row: DatasourceInfo,
  action: 'reload' | 'reconnect',
  pathSuffix: string,
  okKey: 'dsReloadDone' | 'dsReconnectDone',
) {
  setBusy(row, action, true)
  try {
    await apiPost(`/v1/admin/datasources/${row.name}${pathSuffix}`)
    notifySuccess(t(okKey, ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, action, false)
  }
}

async function reloadKb(row: DatasourceInfo) {
  await runAction(row, 'reload', '/kb/reload', 'dsReloadDone')
}

async function reconnect(row: DatasourceInfo) {
  await runAction(row, 'reconnect', '/reconnect', 'dsReconnectDone')
}

async function remove(row: DatasourceInfo) {
  try {
    await ElMessageBox.confirm(t('dsRemoveConfirm', ui.lang), 'Confirm')
  } catch {
    return
  }
  setBusy(row, 'remove', true)
  try {
    await apiDelete(`/v1/admin/datasources/${row.name}`)
    notifySuccess(t('dsRemovedOk', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    setBusy(row, 'remove', false)
  }
}

onMounted(load)
</script>
